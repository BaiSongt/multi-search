import asyncio
import json

import httpx

CRAWL4AI_VERSION = "0.9.5"
_MAX_HTML_BYTES = 1024 * 1024
_MAX_RESULT_BYTES = 65_536


class CrawlParser:
    """向部署方配置的隔离服务发送已验证的 HTML，不发送来源 URL。"""

    def __init__(self, client: httpx.AsyncClient, base_url: str, token: str):
        self._client = client
        self._url = f"{base_url.rstrip('/')}/parse"
        self._token = token

    async def select(self, raw_html: str, *, timeout_s: float) -> tuple[str, ...] | None:
        data = raw_html.encode("utf-8")
        if not data or len(data) > _MAX_HTML_BYTES or timeout_s <= 0:
            return None
        try:
            async with (
                asyncio.timeout(timeout_s),
                self._client.stream(
                    "POST",
                    self._url,
                    content=data,
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Content-Type": "text/html; charset=utf-8",
                        "Accept-Encoding": "identity",
                    },
                    follow_redirects=False,
                    timeout=None,
                ) as response,
            ):
                if (
                    response.status_code != 200
                    or response.headers.get("content-encoding", "identity").lower() != "identity"
                    or response.headers.get("content-type", "").partition(";")[0].strip().lower()
                    != "application/json"
                ):
                    return None
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > _MAX_RESULT_BYTES:
                        return None
                    body.extend(chunk)
            payload = json.loads(body)
        except (TimeoutError, httpx.HTTPError, ValueError, RecursionError):
            return None
        if not isinstance(payload, dict) or payload.get("version") != CRAWL4AI_VERSION:
            return None
        blocks = payload.get("blocks")
        if (
            not isinstance(blocks, list)
            or len(blocks) > 24
            or any(not isinstance(block, str) or not 0 < len(block) <= 512 for block in blocks)
        ):
            return None
        return tuple(blocks)
