"""离线 Chromium 仅渲染通过鉴权提交的 UTF-8 HTML。"""

import asyncio
import hmac
import http.client
import json
import os
import re
import signal
import socket
import stat
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import uvicorn
from playwright.async_api import Browser, Error, async_playwright
from playwright.async_api import TimeoutError as BrowserTimeout

SOCKET = Path("/run/multisearch-renderer/render.sock")
ORIGIN = "https://multisearch-render.invalid/"
MAX_HTML = 1024 * 1024
MAX_WIRE = 6 * MAX_HTML
MAX_BUNDLE_WIRE = 10 * 1024 * 1024
MAX_ASSETS = 8
MAX_ASSET = 256 * 1024
MAX_ASSET_TOTAL = 512 * 1024
RENDER_SECONDS = 5
SETTLE_SECONDS = 0.2
CHROMIUM = "/usr/bin/chromium"
MIN_CHROMIUM_VERSION = (157, 0, 8103, 38)
CSP = (
    "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-src 'none'; "
    "connect-src 'none'; worker-src 'none'; object-src 'none'"
)
BUNDLE_CSP = (
    "sandbox allow-scripts allow-same-origin; default-src 'none'; script-src 'self' 'unsafe-inline'; "
    "style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-src 'none'; "
    "connect-src 'self'; worker-src 'none'; object-src 'none'"
)
ASSET_TYPES = {
    "script": (frozenset({"text/javascript", "application/javascript"}), frozenset({"script"})),
    "json": (frozenset({"application/json"}), frozenset({"fetch", "xhr"})),
}
DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
BLOCKED_REQUEST_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie", "range"})


@dataclass(frozen=True)
class Asset:
    content_type: str
    body: str
    request_types: frozenset[str]


@dataclass(frozen=True)
class Snapshot:
    url: str
    html: str
    source_csp: str | None
    assets: dict[str, Asset]


def canonical_host(url: str) -> str:
    if not isinstance(url, str) or not re.fullmatch(r"[!-~]{1,4096}", url) or "\\" in url:
        raise ValueError("快照 URL 无效")
    parts = urlsplit(url)
    host = parts.hostname
    if (
        parts.scheme != "https"
        or not host
        or parts.netloc != host
        or len(host) > 253
        or not parts.path.startswith("/")
        or url != urlunsplit(("https", host, parts.path, parts.query, ""))
    ):
        raise ValueError("快照 URL 无效")
    labels = host.split(".")
    if (
        len(labels) < 2
        or not labels[-1][:1].isalpha()
        or not all(DNS_LABEL.fullmatch(label) for label in labels)
    ):
        raise ValueError("快照域名无效")
    return host


def distinct_keys(pairs: list[tuple]) -> dict:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("快照包含重复字段")
    return value


def parse_bundle(raw: bytes) -> Snapshot:
    data = json.loads(raw.decode("utf-8"), object_pairs_hook=distinct_keys)
    if not isinstance(data, dict) or data.keys() != {"url", "html", "source_csp", "resources"}:
        raise ValueError("快照字段无效")
    host = canonical_host(data["url"])
    body, policy, resources = data["html"], data["source_csp"], data["resources"]
    if not isinstance(body, str) or not body or not isinstance(resources, list) or not resources:
        raise ValueError("快照正文或资源无效")
    if len(body.encode()) > MAX_HTML or len(resources) > MAX_ASSETS:
        raise OverflowError
    if policy is not None and (
        not isinstance(policy, str) or not re.fullmatch(r"[ -~]{1,4096}", policy)
    ):
        raise ValueError("快照 CSP 无效")
    assets: dict[str, Asset] = {}
    total_size = 0
    for resource in resources:
        if not isinstance(resource, dict) or resource.keys() != {
            "url",
            "kind",
            "content_type",
            "body",
        }:
            raise ValueError("资源字段无效")
        url, kind, content_type, text = (
            resource["url"],
            resource["kind"],
            resource["content_type"],
            resource["body"],
        )
        if (
            canonical_host(url) != host
            or url == data["url"]
            or url in assets
            or not isinstance(kind, str)
            or kind not in ASSET_TYPES
            or not isinstance(content_type, str)
            or content_type not in ASSET_TYPES[kind][0]
            or not isinstance(text, str)
        ):
            raise ValueError("资源地址或类型无效")
        size = len(text.encode())
        total_size += size
        if size > MAX_ASSET or total_size > MAX_ASSET_TOTAL:
            raise OverflowError
        assets[url] = Asset(content_type, text, ASSET_TYPES[kind][1])
    return Snapshot(data["url"], body, policy, assets)


def token() -> bytes:
    value = Path(os.environ["MULTISEARCH_RENDERER_TOKEN_FILE"]).read_bytes().strip()
    if not 32 <= len(value) <= 256 or any(byte < 33 or byte > 126 for byte in value):
        raise RuntimeError("渲染器 Token 需为 32 至 256 个可见 ASCII 字节")
    return value


async def reply(send, status: int, document: dict) -> None:
    data = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()
    if len(data) > MAX_WIRE:
        status, data = 413, b'{"code":"RENDER_OUTPUT_TOO_LARGE"}'
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": data})


async def read_body(receive, limit: int) -> bytes | None:
    body = bytearray()
    while True:
        event = await receive()
        if event["type"] == "http.disconnect":
            return None
        chunk = event.get("body", b"")
        if len(body) + len(chunk) > limit:
            raise OverflowError
        body.extend(chunk)
        if not event.get("more_body", False):
            return bytes(body)


@asynccontextmanager
async def browser_context(browser: Browser):
    context = await browser.new_context(
        accept_downloads=False,
        bypass_csp=False,
        service_workers="block",
        viewport={"width": 1024, "height": 768},
    )
    try:
        yield context
    finally:
        async with asyncio.timeout(1):
            await context.close()


async def sandbox_status(browser: Browser) -> None:
    async with asyncio.timeout(8), browser_context(browser) as context:
        page = await context.new_page()
        await page.goto("chrome://sandbox/")
        rows = page.locator("#sandbox-status tr")
        await rows.first.wait_for(state="attached")
        statuses = dict(
            await rows.evaluate_all(
                "rows => rows.map(row => Array.from(row.cells, cell => cell.textContent.trim()))"
            )
        )
    expected = {
        "Layer 1 Sandbox": "Namespace",
        "PID namespaces": "Yes",
        "Network namespaces": "Yes",
        "Seccomp-BPF sandbox": "Yes",
    }
    if any(statuses.get(label) != value for label, value in expected.items()):
        raise RuntimeError("Chromium 命名空间或 Seccomp-BPF 沙箱未启用")


async def render(browser: Browser, snapshot: Snapshot) -> str:
    async with browser_context(browser) as context:
        page = await context.new_page()
        delivered = False
        policy = BUNDLE_CSP if snapshot.assets else CSP
        if snapshot.source_csp:
            policy += f", {snapshot.source_csp}"

        async def intercept(route):
            nonlocal delivered
            request = route.request
            if (
                request.method != "GET"
                or request.redirected_from is not None
                or request.post_data_buffer
            ):
                await route.abort("blockedbyclient")
                return
            main_document = (
                not delivered and request.url == snapshot.url and request.is_navigation_request()
            )
            asset = snapshot.assets.get(request.url)
            resource_allowed = (
                delivered
                and asset is not None
                and not request.is_navigation_request()
                and request.resource_type in asset.request_types
            )
            if not (main_document or resource_allowed):
                await route.abort("blockedbyclient")
                return
            try:
                from_main_frame = request.frame == page.main_frame
            except Error:
                # 部分导航请求在创建对应 frame 前即到达。
                from_main_frame = False
            if not from_main_frame or BLOCKED_REQUEST_HEADERS.intersection(
                await request.all_headers()
            ):
                await route.abort("blockedbyclient")
                return
            if main_document:
                if delivered:
                    await route.abort("blockedbyclient")
                    return
                delivered = True
                await route.fulfill(
                    status=200,
                    headers={
                        "content-type": "text/html; charset=utf-8",
                        "content-security-policy": policy,
                        "cache-control": "no-store",
                        "x-content-type-options": "nosniff",
                    },
                    body=snapshot.html,
                )
                return
            assert asset is not None
            await route.fulfill(
                status=200,
                headers={
                    "content-type": f"{asset.content_type}; charset=utf-8",
                    "cache-control": "no-store",
                    "x-content-type-options": "nosniff",
                },
                body=asset.body,
            )

        async def block_websocket(route):
            await route.close(code=1008)

        await context.route("**/*", intercept)
        await context.route_web_socket("**/*", block_websocket)
        await page.goto(
            snapshot.url,
            wait_until="networkidle" if snapshot.assets else "domcontentloaded",
            timeout=3500,
        )
        if not snapshot.assets:
            await asyncio.sleep(SETTLE_SECONDS)
        if not delivered or page.url != snapshot.url:
            raise ValueError("页面离开已验证的快照")
        # 在独立 JavaScript 环境中读取 DOM 并限制发送到 IPC 的大小。
        session = await context.new_cdp_session(page)
        tree = await session.send("Page.getFrameTree")
        world = await session.send(
            "Page.createIsolatedWorld",
            {
                "frameId": tree["frameTree"]["frame"]["id"],
                "worldName": "multisearch-extract",
            },
        )
        result = await session.send(
            "Runtime.evaluate",
            {
                "expression": (
                    "(() => {const html = document.documentElement?.outerHTML ?? '';"
                    f"return html.length <= {MAX_HTML} ? html : null;}})()"
                ),
                "contextId": world["executionContextId"],
                "returnByValue": True,
            },
        )
        if "exceptionDetails" in result or page.url != snapshot.url:
            raise ValueError("无法读取原页面的渲染结果")
        html = result["result"].get("value")
        if html is None or len(html.encode()) > MAX_HTML:
            raise OverflowError
        return html


class Renderer:
    def __init__(self):
        self.browser: Browser | None = None
        self.version = ""
        self.authorization = b"Bearer " + token()
        self.slot = asyncio.Semaphore(1)
        self.stopping = False

    def browser_disconnected(self, _browser: Browser) -> None:
        if not self.stopping:
            os.kill(os.getpid(), signal.SIGTERM)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await self.lifespan(receive, send)
        elif scope["type"] == "http":
            await self.request(scope, receive, send)

    async def lifespan(self, receive, send):
        await receive()
        if os.geteuid() == 0 or [name for _, name in socket.if_nameindex()] != ["lo"]:
            await send(
                {
                    "type": "lifespan.startup.failed",
                    "message": "需要非 root 且仅回环网络",
                }
            )
            return
        phase = "startup"
        try:
            async with async_playwright() as playwright:
                self.browser = await playwright.chromium.launch(
                    executable_path=CHROMIUM,
                    chromium_sandbox=True,
                    timeout=8000,
                    args=["--disable-setuid-sandbox", "--disable-gpu", "--disable-webgl"],
                    ignore_default_args=[
                        "--disable-ipc-flooding-protection",
                        "--enable-unsafe-swiftshader",
                    ],
                )
                await sandbox_status(self.browser)
                if (
                    not re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", self.browser.version)
                    or tuple(map(int, self.browser.version.split("."))) < MIN_CHROMIUM_VERSION
                ):
                    raise RuntimeError("Chromium 版本未达到动态渲染安全基线")
                self.version = f"chromium.{self.browser.version}"
                self.browser.on("disconnected", self.browser_disconnected)
                await send({"type": "lifespan.startup.complete"})
                phase = "shutdown"
                await receive()
                self.stopping = True
                async with asyncio.timeout(3):
                    await self.browser.close()
                await send({"type": "lifespan.shutdown.complete"})
        except (Error, OSError, RuntimeError, TimeoutError) as error:
            message = str(error) or type(error).__name__
            await send({"type": f"lifespan.{phase}.failed", "message": message})

    async def request(self, scope, receive, send):
        endpoint = (scope["method"], scope["path"])
        values = [value for key, value in scope["headers"] if key == b"authorization"]
        if len(values) != 1 or not hmac.compare_digest(values[0], self.authorization):
            return await reply(send, 401, {"code": "AUTH_REQUIRED"})
        if endpoint not in {
            ("GET", "/health/ready"),
            ("POST", "/render"),
            ("POST", "/render-bundle"),
        }:
            return await reply(send, 404, {"code": "NOT_FOUND"})
        if not self.browser or not self.browser.is_connected():
            return await reply(send, 503, {"code": "RENDERER_UNAVAILABLE"})
        if endpoint == ("GET", "/health/ready"):
            return await reply(send, 200, {"status": "ready"})
        headers = dict(scope["headers"])
        bundled = endpoint[1] == "/render-bundle"
        media_type = b"application/json" if bundled else b"text/html"
        content_type = headers.get(b"content-type", b"").lower().replace(b" ", b"")
        if content_type not in (media_type, media_type + b";charset=utf-8") or (
            headers.get(b"content-encoding", b"identity").lower() != b"identity"
        ):
            return await reply(send, 415, {"code": "UNSUPPORTED_MEDIA_TYPE"})
        policies = [value for key, value in scope["headers"] if key == b"x-multisearch-source-csp"]
        if len(policies) > 1 or (
            policies
            and (
                bundled
                or not 1 <= len(policies[0]) <= 4096
                or any(byte < 32 or byte > 126 for byte in policies[0])
            )
        ):
            return await reply(send, 400, {"code": "INVALID_SOURCE_CSP"})
        try:
            async with asyncio.timeout(RENDER_SECONDS), self.slot:
                raw = await read_body(receive, MAX_BUNDLE_WIRE if bundled else MAX_HTML)
                if raw is None:
                    return
                if not raw:
                    return await reply(send, 422, {"code": "EMPTY_HTML"})
                snapshot = (
                    parse_bundle(raw)
                    if bundled
                    else Snapshot(
                        ORIGIN,
                        raw.decode("utf-8"),
                        policies[0].decode("ascii") if policies else None,
                        {},
                    )
                )
                html = await render(self.browser, snapshot)
        except OverflowError:
            return await reply(send, 413, {"code": "BODY_TOO_LARGE"})
        except (BrowserTimeout, TimeoutError):
            return await reply(send, 504, {"code": "RENDER_TIMEOUT"})
        except (Error, UnicodeError, ValueError, RecursionError):
            return await reply(send, 422, {"code": "RENDER_FAILED"})
        version = f"{self.version}.renderer.{2 if bundled else 1}"
        return await reply(send, 200, {"html": html, "version": version})


class UnixConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(SOCKET.as_posix())


def ready() -> bool:
    connection = UnixConnection("renderer", timeout=2)
    try:
        connection.request(
            "GET",
            "/health/ready",
            headers={"Authorization": "Bearer " + token().decode("ascii")},
        )
        return connection.getresponse().status == 200
    except OSError:
        return False
    finally:
        connection.close()


def serve() -> None:
    os.umask(0o077)
    if SOCKET.exists():
        existing = SOCKET.lstat()
        if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.getuid():
            raise RuntimeError("渲染器 socket 路径已被占用")
        SOCKET.unlink()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(SOCKET.as_posix())
        try:
            server = uvicorn.Server(
                uvicorn.Config(
                    Renderer(),
                    lifespan="on",
                    ws="none",
                    access_log=False,
                    limit_concurrency=8,
                    timeout_keep_alive=1,
                    timeout_graceful_shutdown=5,
                )
            )
            server.run(sockets=[listener])
            if not server.started:
                raise SystemExit(1)
        finally:
            SOCKET.unlink(missing_ok=True)


if __name__ == "__main__":
    if "--healthcheck" in sys.argv[1:]:
        raise SystemExit(0 if ready() else 1)
    serve()
