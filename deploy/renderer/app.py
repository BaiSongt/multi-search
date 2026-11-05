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
from pathlib import Path

import uvicorn
from playwright.async_api import Browser, Error, async_playwright
from playwright.async_api import TimeoutError as BrowserTimeout

SOCKET = Path("/run/multisearch-renderer/render.sock")
ORIGIN = "https://multisearch-render.invalid/"
MAX_HTML = 1024 * 1024
MAX_WIRE = 6 * MAX_HTML
RENDER_SECONDS = 5
SETTLE_SECONDS = 0.2
CHROMIUM = "/usr/bin/chromium"
MIN_CHROMIUM_VERSION = (157, 0, 8103, 38)
CSP = (
    "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-src 'none'; "
    "connect-src 'none'; worker-src 'none'; object-src 'none'"
)


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


async def read_body(receive) -> bytes | None:
    body = bytearray()
    while True:
        event = await receive()
        if event["type"] == "http.disconnect":
            return None
        chunk = event.get("body", b"")
        if len(body) + len(chunk) > MAX_HTML:
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


async def render(browser: Browser, body: str, source_csp: str | None) -> str:
    async with browser_context(browser) as context:
        page = await context.new_page()
        delivered = False

        async def intercept(route):
            nonlocal delivered
            request = route.request
            if (
                not delivered
                and request.url == ORIGIN
                and request.method == "GET"
                and request.is_navigation_request()
                and request.frame == page.main_frame
            ):
                delivered = True
                await route.fulfill(
                    status=200,
                    headers={
                        "content-type": "text/html; charset=utf-8",
                        "content-security-policy": f"{CSP}, {source_csp}" if source_csp else CSP,
                    },
                    body=body,
                )
            else:
                await route.abort("blockedbyclient")

        async def block_websocket(route):
            await route.close(code=1008)

        await context.route("**/*", intercept)
        await context.route_web_socket("**/*", block_websocket)
        await page.goto(ORIGIN, wait_until="domcontentloaded", timeout=3500)
        await asyncio.sleep(SETTLE_SECONDS)
        if not delivered or page.url != ORIGIN:
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
        if "exceptionDetails" in result or page.url != ORIGIN:
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
                self.version = f"chromium.{self.browser.version}.renderer.1"
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
        if endpoint not in {("GET", "/health/ready"), ("POST", "/render")}:
            return await reply(send, 404, {"code": "NOT_FOUND"})
        if not self.browser or not self.browser.is_connected():
            return await reply(send, 503, {"code": "RENDERER_UNAVAILABLE"})
        if endpoint == ("GET", "/health/ready"):
            return await reply(send, 200, {"status": "ready"})
        headers = dict(scope["headers"])
        content_type = headers.get(b"content-type", b"").lower().replace(b" ", b"")
        if content_type not in (b"text/html", b"text/html;charset=utf-8") or (
            headers.get(b"content-encoding", b"identity").lower() != b"identity"
        ):
            return await reply(send, 415, {"code": "UNSUPPORTED_MEDIA_TYPE"})
        policies = [value for key, value in scope["headers"] if key == b"x-multisearch-source-csp"]
        if len(policies) > 1 or (
            policies
            and (
                not 1 <= len(policies[0]) <= 4096
                or any(byte < 32 or byte > 126 for byte in policies[0])
            )
        ):
            return await reply(send, 400, {"code": "INVALID_SOURCE_CSP"})
        try:
            async with asyncio.timeout(RENDER_SECONDS):
                raw = await read_body(receive)
                if raw is None:
                    return
                body = raw.decode("utf-8")
                if not body:
                    return await reply(send, 422, {"code": "EMPTY_HTML"})
                async with self.slot:
                    html = await render(
                        self.browser,
                        body,
                        policies[0].decode("ascii") if policies else None,
                    )
        except OverflowError:
            return await reply(send, 413, {"code": "BODY_TOO_LARGE"})
        except (BrowserTimeout, TimeoutError):
            return await reply(send, 504, {"code": "RENDER_TIMEOUT"})
        except (Error, UnicodeError, ValueError):
            return await reply(send, 422, {"code": "RENDER_FAILED"})
        return await reply(send, 200, {"html": html, "version": self.version})


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
