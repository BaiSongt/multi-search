"""把已经获准获取的 HTML 交给断网渲染服务。"""

import asyncio
import json
import re
from collections.abc import Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx

from .render_resources import (
    MAX_RESOURCE_BUNDLE_BYTES,
    MAX_RESOURCE_BYTES,
    MAX_RESOURCES_PER_PAGE,
    FetchedResource,
)
from .safe_fetch import FetchError

_SOCKET = "/run/multisearch-renderer/render.sock"
_BASE = "http://renderer"
_MAX_HTML_BYTES = 1024 * 1024
_MAX_BUNDLE_WIRE_BYTES = 10 * _MAX_HTML_BYTES
_MAX_RESPONSE_BYTES = 6 * _MAX_HTML_BYTES
_MAX_RENDER_SECONDS = 8.0
_READY_SECONDS = 0.5
_VERSION = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.+-]{0,63}")
_CSP = re.compile(r"[\x20-\x7e]{1,4096}")
_COEP = {"unsafe-none", "require-corp", "credentialless"}
HEARTBEAT_KEY = "multisearch:worker:fetch_dynamic"


@dataclass(frozen=True)
class RenderedHTML:
    html: str
    version: str


class DynamicRenderer:
    def __init__(self, client: httpx.AsyncClient, token: str):
        if re.fullmatch(r"[\x21-\x7e]{32,256}", token) is None:
            raise ValueError("渲染令牌需要 32–256 个可见 ASCII 字符")
        self._client = client
        self._headers = {"Authorization": f"Bearer {token}", "Accept-Encoding": "identity"}

    @classmethod
    @asynccontextmanager
    async def connect(cls, token: str | None):
        if token is None:
            yield None
            return
        transport = httpx.AsyncHTTPTransport(
            uds=_SOCKET,
            trust_env=False,
            retries=0,
            limits=httpx.Limits(max_connections=3, max_keepalive_connections=0),
        )
        async with httpx.AsyncClient(
            transport=transport, trust_env=False, follow_redirects=False
        ) as client:
            yield cls(client, token)

    async def ready(self) -> bool:
        try:
            async with (
                asyncio.timeout(_READY_SECONDS),
                self._client.stream(
                    "GET", _BASE + "/health/ready", headers=self._headers, follow_redirects=False
                ) as response,
            ):
                return response.status_code == 200
        except (TimeoutError, httpx.HTTPError):
            return False

    async def render(
        self, html: str, *, timeout_s: float, source_csp: str | None = None
    ) -> RenderedHTML | None:
        headers = {"Content-Type": "text/html; charset=utf-8"}
        if source_csp is not None:
            if _CSP.fullmatch(source_csp) is None:
                return None
            headers["X-MultiSearch-Source-CSP"] = source_csp
        try:
            data = html.encode("utf-8")
        except UnicodeError:
            return None
        if not data or len(data) > _MAX_HTML_BYTES:
            return None
        return await self._submit("/render", data, headers, timeout_s)

    async def render_bundle(
        self,
        url: str,
        html: str,
        resources: Sequence[FetchedResource],
        *,
        timeout_s: float,
        source_csp: str | None = None,
        source_coep: str | None = None,
    ) -> RenderedHTML | None:
        if not 1 <= len(resources) <= MAX_RESOURCES_PER_PAGE:
            return None
        if source_csp is not None and _CSP.fullmatch(source_csp) is None:
            return None
        if source_coep is not None and source_coep not in _COEP:
            return None
        try:
            sizes = [len(resource.body.encode("utf-8")) for resource in resources]
            if (
                not 0 < len(html.encode("utf-8")) <= _MAX_HTML_BYTES
                or max(sizes) > MAX_RESOURCE_BYTES
                or sum(sizes) > MAX_RESOURCE_BUNDLE_BYTES
            ):
                return None
            payload = {
                "url": url,
                "html": html,
                "source_csp": source_csp,
                "resources": [
                    {
                        "url": resource.url,
                        "kind": resource.kind,
                        "content_type": resource.content_type,
                        "body": resource.body,
                        **(
                            {
                                "allow_origin": resource.allow_origin,
                                "resource_policy": resource.resource_policy,
                            }
                            if resource.allow_origin is not None
                            or resource.resource_policy is not None
                            else {}
                        ),
                    }
                    for resource in resources
                ],
            }
            if source_coep is not None:
                payload["source_coep"] = source_coep
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except UnicodeError:
            return None
        if len(data) > _MAX_BUNDLE_WIRE_BYTES:
            return None
        return await self._submit(
            "/render-bundle", data, {"Content-Type": "application/json"}, timeout_s
        )

    async def _submit(
        self, endpoint: str, data: bytes, headers: dict[str, str], timeout_s: float
    ) -> RenderedHTML | None:
        if not timeout_s > 0:
            raise FetchError(504, "TIME_BUDGET_EXCEEDED", "动态正文提取时限已耗尽", retryable=True)
        try:
            async with (
                asyncio.timeout(min(timeout_s, _MAX_RENDER_SECONDS)),
                self._client.stream(
                    "POST",
                    _BASE + endpoint,
                    content=data,
                    headers={**self._headers, **headers},
                    follow_redirects=False,
                ) as response,
            ):
                if response.status_code == 504:
                    raise TimeoutError
                if (
                    response.status_code != 200
                    or response.headers.get("content-encoding", "identity").lower() != "identity"
                    or response.headers.get("content-type", "").partition(";")[0].strip().lower()
                    != "application/json"
                ):
                    return None
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                        return None
                    body.extend(chunk)
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict) or payload.keys() != {"html", "version"}:
                return None
            rendered, version = payload["html"], payload["version"]
            if (
                not isinstance(rendered, str)
                or not 0 < len(rendered.encode("utf-8")) <= _MAX_HTML_BYTES
                or not isinstance(version, str)
                or _VERSION.fullmatch(version) is None
            ):
                return None
            return RenderedHTML(rendered, version)
        except (TimeoutError, httpx.TimeoutException):
            raise FetchError(
                504, "TIME_BUDGET_EXCEEDED", "动态正文提取时限已耗尽", retryable=True
            ) from None
        except (httpx.HTTPError, ValueError, RecursionError):
            return None
