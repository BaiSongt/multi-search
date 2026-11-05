"""只接收已安全获取的 UTF-8 HTML，并运行固定的本地解析器。"""

import asyncio
import hmac
import json
import os
import sys
from pathlib import Path

_MAX_BODY = 1024 * 1024
_MAX_OUTPUT = 65_536
_TIMEOUT = 5
_SLOT = asyncio.Semaphore(1)
_SCRIPT = Path(__file__).with_name("parse_html.py")
_TOKEN = Path(os.environ["CRAWL4AI_API_TOKEN_FILE"]).read_bytes().strip()
if len(_TOKEN) < 32 or not _TOKEN.isascii():
    raise RuntimeError("解析器 Token 需要至少 32 个 ASCII 字节")


async def _respond(send, status, payload):
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store")],
        }
    )
    await send({"type": "http.response.body", "body": data})


async def _parse(body: bytes) -> dict | None:
    env = {
        name: os.environ[name]
        for name in ("PATH", "HOME", "SYSTEMROOT", "TEMP", "TMP")
        if name in os.environ
    }
    env.update(OPENBLAS_NUM_THREADS="1", PYTHONDONTWRITEBYTECODE="1")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(_SCRIPT),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
    )
    try:
        output, _ = await process.communicate(body)
        if process.returncode or len(output) > _MAX_OUTPUT:
            return None
        result = json.loads(output)
        return result if isinstance(result, dict) else None
    except (ValueError, OSError):
        return None
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.communicate()


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        await receive()
        await send({"type": "lifespan.startup.complete"})
        await receive()
        await send({"type": "lifespan.shutdown.complete"})
        return
    if scope["type"] != "http":
        return
    method, path = scope["method"], scope["path"]
    if method == "GET" and path == "/health/ready":
        await _respond(send, 200, {"status": "ready"})
        return
    if method != "POST" or path != "/parse":
        await _respond(send, 404, {"code": "NOT_FOUND"})
        return
    values = [value for key, value in scope["headers"] if key == b"authorization"]
    if len(values) != 1 or not hmac.compare_digest(values[0], b"Bearer " + _TOKEN):
        await _respond(send, 401, {"code": "AUTH_REQUIRED"})
        return
    headers = dict(scope["headers"])
    content_type = headers.get(b"content-type", b"").lower().replace(b" ", b"")
    if content_type not in (b"text/html", b"text/html;charset=utf-8") or (
        headers.get(b"content-encoding", b"identity").lower() != b"identity"
    ):
        await _respond(send, 415, {"code": "UNSUPPORTED_MEDIA_TYPE"})
        return
    try:
        async with asyncio.timeout(_TIMEOUT), _SLOT:
            body = bytearray()
            while True:
                event = await receive()
                if event["type"] == "http.disconnect":
                    return
                chunk = event.get("body", b"")
                if len(body) + len(chunk) > _MAX_BODY:
                    await _respond(send, 413, {"code": "BODY_TOO_LARGE"})
                    return
                body.extend(chunk)
                if not event.get("more_body", False):
                    break
            if not body:
                await _respond(send, 422, {"code": "EMPTY_HTML"})
                return
            parsed = await _parse(bytes(body))
    except TimeoutError:
        await _respond(send, 504, {"code": "PARSE_TIMEOUT"})
        return
    if parsed is None:
        await _respond(send, 422, {"code": "PARSE_FAILED"})
        return
    await _respond(send, 200, parsed)
