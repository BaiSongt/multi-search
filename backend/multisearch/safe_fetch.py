import asyncio
import math
import re
import socket
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import Message
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from time import monotonic
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from protego import Protego

from .contracts import Scope
from .errors import ApiError
from .pdf_child import PDF_MAX_BYTES, PDF_SIGNATURE

_USER_AGENT = "MultiSearchBot/0.1"
_PORTS = {"http": 80, "https": 443}
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_BLOCKED_TLDS = {"local", "internal", "localhost", "test", "invalid", "onion", "lan"}
_TRANSITION_NETWORKS = tuple(
    ip_network(value) for value in ("2002::/16", "2001::/32", "64:ff9b::/96", "64:ff9b:1::/48")
)
_SPECIAL_IPV4 = tuple(
    ip_network(value) for value in ("168.63.129.16/32", "192.0.0.0/24", "192.88.99.0/24")
)
_PAGE_TYPES = frozenset({"text/html", "application/xhtml+xml", "text/plain", "application/pdf"})
_ROBOTS_TYPES = frozenset({"text/plain"})
_REDIRECTS = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 5
_ROBOTS_BODY_BYTES = 512 * 1024
_ROBOTS_TTL_SECONDS = 600
_ROBOTS_CACHE_ENTRIES = 256
_MAX_BODY_BYTES = 1024 * 1024
_MAX_TIMEOUT_S = 60
_MIN_HOST_DELAY = 0.2

type IP = IPv4Address | IPv6Address


class FetchError(ApiError):
    """安全抓取的可公开错误。"""


@dataclass(frozen=True)
class FetchedPage:
    origin_url: str
    url: str
    content_type: str
    body: bytes
    retrieved_at: datetime
    charset: str | None = None
    max_snippet: int | None = None


@dataclass(frozen=True)
class _Target:
    url: str
    host: str
    authority: str
    scheme: str
    port: int

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.authority}"

    def pin(self, address: IP) -> httpx.URL:
        return httpx.URL(self.url).copy_with(host=str(address))


@dataclass(frozen=True)
class _Response:
    status: int
    location: str | None = None
    content_type: str = ""
    charset: str | None = None
    body: bytes = b""
    max_snippet: int | None = None


@dataclass
class _RobotsPolicy:
    rules: Protego
    expires: float
    delay: float
    ready_at: float


def _blocked() -> FetchError:
    return FetchError(403, "FETCH_TARGET_BLOCKED", "抓取地址不符合公网访问策略")


def _budget_exhausted() -> FetchError:
    return FetchError(504, "TIME_BUDGET_EXCEEDED", "抓取时限已耗尽", retryable=True)


def _robots_unavailable() -> FetchError:
    return FetchError(403, "FETCH_ROBOTS_UNAVAILABLE", "无法确认站点抓取规则")


def _public_ip(value: str) -> IP:
    try:
        address = ip_address(value)
    except ValueError:
        raise _blocked() from None
    if isinstance(address, IPv6Address):
        if address.scope_id or any(address in network for network in _TRANSITION_NETWORKS):
            raise _blocked()
        if address.ipv4_mapped is not None:
            address = address.ipv4_mapped
    if (
        not address.is_global
        or address.is_multicast
        or address.is_reserved
        or any(address in network for network in _SPECIAL_IPV4)
    ):
        raise _blocked()
    return address


def _clean_url_input(url: str) -> bool:
    return (
        bool(url)
        and len(url) <= 4096
        and not any(
            char == "\\" or char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url
        )
    )


def _target(url: str) -> _Target:
    if not _clean_url_input(url):
        raise _blocked()
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise _blocked() from None
    if (
        parts.scheme not in _PORTS
        or not host
        or parts.username is not None
        or "%" in host
        or (port is not None and port != _PORTS[parts.scheme])
    ):
        raise _blocked()
    try:
        literal = ip_address(host)
    except ValueError:
        try:
            host = httpx.URL(f"https://{host.removesuffix('.')}/").raw_host.decode("ascii")
        except (httpx.InvalidURL, UnicodeError):
            raise _blocked() from None
        labels = host.split(".")
        if (
            len(host) > 253
            or len(labels) < 2
            or not all(_HOST_LABEL.fullmatch(label) for label in labels)
            or not labels[-1][0].isalpha()
            or labels[-1] in _BLOCKED_TLDS
            or host.endswith(".home.arpa")
            or host == "home.arpa"
        ):
            raise _blocked() from None
        authority = host
    else:
        _public_ip(host)
        host = str(literal)
        authority = f"[{host}]" if isinstance(literal, IPv6Address) else host
    try:
        canonical = str(
            httpx.URL(urlunsplit((parts.scheme, authority, parts.path or "/", parts.query, "")))
        )
    except (httpx.InvalidURL, UnicodeError):
        raise _blocked() from None
    return _Target(canonical, host, authority, parts.scheme, _PORTS[parts.scheme])


def _domain_matches(host: str, values: Sequence[str]) -> bool:
    return any(host == value.lower() or host.endswith(f".{value.lower()}") for value in values)


def _check_domains(target: _Target, allowlist: Sequence[str], denylist: Sequence[str]) -> None:
    if _domain_matches(target.host, denylist) or (
        allowlist and not _domain_matches(target.host, allowlist)
    ):
        raise _blocked()


def validate_fetch_url(
    url: str,
    domain_allowlist: Sequence[str] = (),
    domain_denylist: Sequence[str] = (),
) -> None:
    """在任务入队前拒绝明显不合规的地址；实际连接仍逐跳检查 DNS。"""
    _check_domains(_target(url), domain_allowlist, domain_denylist)


async def _resolve(host: str, port: int) -> list[str]:
    records = await asyncio.get_running_loop().getaddrinfo(
        f"{host}.", port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    return [record[4][0] for record in records]


def _transport() -> httpx.AsyncBaseTransport:
    return httpx.AsyncHTTPTransport(
        verify=True, trust_env=False, retries=0, limits=httpx.Limits(max_keepalive_connections=0)
    )


def snippet_limit(directives: Sequence[str]) -> int | None:
    limit: int | None = None
    for entry in directives:
        lowered = entry.casefold()
        if re.search(r"(?:^|[\s,:;])(?:noindex|none|nosnippet|noarchive)(?:$|[\s,;])", lowered):
            raise FetchError(403, "FETCH_SITE_RESTRICTED", "站点禁止保存或摘录此内容")
        for match in re.finditer(r"(?:^|[\s,:;])max-snippet\s*:\s*(-?\d{1,9})(?!\d)", lowered):
            value = int(match[1])
            if value == 0:
                raise FetchError(403, "FETCH_SITE_RESTRICTED", "站点禁止摘录此内容")
            if value > 0:
                limit = min(limit, value) if limit is not None else value
    return limit


def _redirect(target: _Target, location: str | None) -> _Target:
    if not location:
        raise FetchError(502, "FETCH_REDIRECT_INVALID", "来源重定向缺少目标地址")
    if not _clean_url_input(location):
        raise _blocked()
    updated = _target(urljoin(target.url, location))
    if target.scheme == "https" and updated.scheme == "http":
        raise _blocked()
    return updated


class SafeFetcher:
    def __init__(self, *, max_parallel: int = 8, max_body_bytes: int = _MAX_BODY_BYTES):
        if max_parallel < 1 or max_body_bytes < 1:
            raise ValueError("抓取并发与响应上限需要为正数")
        self._slots = asyncio.Semaphore(max_parallel)
        self._host_locks = tuple(asyncio.Lock() for _ in range(64))
        self._max_body_bytes = max_body_bytes
        self._robots_cache: OrderedDict[str, _RobotsPolicy] = OrderedDict()

    async def fetch(
        self,
        url: str,
        *,
        timeout_s: float,
        network_scope: Scope | str = Scope.PUBLIC,
        domain_allowlist: Sequence[str] = (),
        domain_denylist: Sequence[str] = (),
    ) -> FetchedPage:
        if network_scope != Scope.PUBLIC:
            raise _blocked()
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise _budget_exhausted()
        timeout_s = min(timeout_s, _MAX_TIMEOUT_S)
        target = _target(url)
        origin_url = target.url
        deadline = monotonic() + timeout_s
        seen: set[str] = set()
        try:
            async with asyncio.timeout(timeout_s), self._slots:
                for _ in range(_MAX_REDIRECTS + 1):
                    if target.url in seen:
                        break
                    seen.add(target.url)
                    _check_domains(target, domain_allowlist, domain_denylist)
                    lock = self._host_locks[hash(target.host) % len(self._host_locks)]
                    async with lock:
                        rules = await self._robots_policy(target)
                        if not rules.rules.can_fetch(target.url, _USER_AGENT):
                            raise FetchError(
                                403, "FETCH_ROBOTS_DENIED", "站点 robots.txt 禁止抓取此地址"
                            )
                        wait = rules.ready_at - monotonic()
                        if wait >= deadline - monotonic():
                            raise _budget_exhausted()
                        if wait > 0:
                            await asyncio.sleep(wait)
                        rules.ready_at = monotonic() + rules.delay
                        response = await self._request(target, _PAGE_TYPES, self._max_body_bytes)
                    if response.status not in _REDIRECTS:
                        return self._page(origin_url, target, response)
                    target = _redirect(target, response.location)
        except (TimeoutError, httpx.TimeoutException):
            raise _budget_exhausted() from None
        except (OSError, httpx.HTTPError):
            raise FetchError(502, "FETCH_UNAVAILABLE", "来源暂时不可达", retryable=True) from None
        raise FetchError(502, "FETCH_REDIRECT_LIMIT", "来源重定向超限或形成循环")

    async def _robots_policy(self, target: _Target) -> _RobotsPolicy:
        cached = self._robots_cache.get(target.origin)
        if cached is not None and cached.expires > monotonic():
            self._robots_cache.move_to_end(target.origin)
            return cached
        robots = _target(f"{target.origin}/robots.txt")
        try:
            for _ in range(_MAX_REDIRECTS + 1):
                response = await self._request(robots, _ROBOTS_TYPES, _ROBOTS_BODY_BYTES)
                if response.status not in _REDIRECTS:
                    break
                robots = _redirect(robots, response.location)
                if robots.host != target.host:
                    raise _robots_unavailable()
            else:
                raise _robots_unavailable()
            if response.status == 200:
                rules = Protego.parse(response.body.decode("utf-8-sig"))
            elif response.status in (404, 410):
                rules = Protego.parse("")
            else:
                raise _robots_unavailable()
        except FetchError as error:
            if error.code in {"FETCH_TARGET_BLOCKED", "FETCH_ROBOTS_UNAVAILABLE"}:
                raise
            raise _robots_unavailable() from None
        except UnicodeError:
            raise _robots_unavailable() from None
        crawl_delay = rules.crawl_delay(_USER_AGENT) or 0
        request_rate = rules.request_rate(_USER_AGENT)
        interval = (
            request_rate.seconds / request_rate.requests
            if request_rate and request_rate.requests
            else 0
        )
        delay = max(crawl_delay, interval, _MIN_HOST_DELAY)
        if not math.isfinite(delay):
            raise FetchError(403, "FETCH_ROBOTS_UNAVAILABLE", "站点抓取间隔无法执行")
        now = monotonic()
        result = _RobotsPolicy(rules, now + _ROBOTS_TTL_SECONDS, delay, now + delay)
        self._robots_cache[target.origin] = result
        self._robots_cache.move_to_end(target.origin)
        if len(self._robots_cache) > _ROBOTS_CACHE_ENTRIES:
            self._robots_cache.popitem(last=False)
        return result

    async def _request(
        self, target: _Target, content_types: frozenset[str], max_bytes: int
    ) -> _Response:
        try:
            literal = ip_address(target.host)
        except ValueError:
            addresses = [_public_ip(value) for value in await _resolve(target.host, target.port)]
        else:
            addresses = [_public_ip(str(literal))]
        if not addresses:
            raise FetchError(502, "FETCH_UNAVAILABLE", "来源没有可用的公网地址", retryable=True)
        # 连接池只在当前跳使用。IP 固定到经过检查的 DNS 答案，TLS 仍验证原始主机名。
        address = next(
            (value for value in addresses if isinstance(value, IPv4Address)), addresses[0]
        )
        async with (
            httpx.AsyncClient(
                transport=_transport(), trust_env=False, follow_redirects=False
            ) as client,
            client.stream(
                "GET",
                target.pin(address),
                headers={
                    "Host": target.authority,
                    "User-Agent": _USER_AGENT,
                    "Accept": ", ".join(sorted(content_types)),
                    "Accept-Encoding": "identity",
                },
                extensions={"sni_hostname": target.host},
                timeout=None,
            ) as response,
        ):
            status = response.status_code
            if status != 200:
                return _Response(status, response.headers.get("location"))
            if response.headers.get("cf-mitigated", "").casefold() == "challenge":
                raise FetchError(403, "FETCH_SITE_RESTRICTED", "站点要求通过访问验证")
            if response.headers.get("content-encoding", "identity").casefold() != "identity":
                raise FetchError(415, "FETCH_CONTENT_UNSUPPORTED", "响应使用了不受支持的编码")
            mime_header = response.headers.get("content-type", "")
            kind = mime_header.partition(";")[0].strip().lower()
            if kind not in content_types:
                raise FetchError(415, "FETCH_CONTENT_UNSUPPORTED", "来源响应类型不受支持")
            is_pdf = kind == "application/pdf"
            if is_pdf:
                max_bytes = min(max_bytes, PDF_MAX_BYTES)
            mime = Message()
            mime["Content-Type"] = mime_header
            maximum = snippet_limit(response.headers.get_list("x-robots-tag"))
            length = response.headers.get("content-length")
            if (
                length is not None
                and length.isascii()
                and length.isdecimal()
                and (len(length) > 12 or int(length) > max_bytes)
            ):
                raise FetchError(413, "FETCH_BODY_TOO_LARGE", "来源响应超过抓取上限")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > max_bytes:
                    raise FetchError(413, "FETCH_BODY_TOO_LARGE", "来源响应超过抓取上限")
                body.extend(chunk)
            if body.startswith(PDF_SIGNATURE) != is_pdf:
                raise FetchError(415, "FETCH_CONTENT_UNSUPPORTED", "PDF 响应类型与文件签名不符")
            return _Response(
                status,
                content_type=kind,
                charset=mime.get_content_charset(),
                body=bytes(body),
                max_snippet=maximum,
            )

    @staticmethod
    def _page(origin_url: str, target: _Target, response: _Response) -> FetchedPage:
        if response.status in (401, 403, 407, 451):
            raise FetchError(403, "FETCH_SITE_RESTRICTED", "站点限制访问此内容")
        if response.status in (404, 410):
            raise FetchError(404, "FETCH_NOT_FOUND", "来源不存在或已移除")
        if response.status == 429:
            raise FetchError(429, "FETCH_RATE_LIMITED", "站点已限制请求速率", retryable=True)
        if response.status != 200:
            raise FetchError(502, "FETCH_UNAVAILABLE", "来源未返回可提取的正文", retryable=True)
        return FetchedPage(
            origin_url,
            target.url,
            response.content_type,
            response.body,
            datetime.now(UTC),
            response.charset,
            response.max_snippet,
        )
