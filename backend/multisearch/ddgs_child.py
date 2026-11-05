"""单次 DDGS 查询进程。由 API 终止超时查询，避免 SDK 线程继续执行。"""

import json
import sys
from urllib.parse import urlsplit

from ddgs.ddgs import DDGS
from ddgs.exceptions import DDGSException, TimeoutException

_RESULT_FIELDS = {"title", "href", "url", "body", "date", "image", "thumbnail", "width", "height"}
_SEARCH_HOSTS = {"html.duckduckgo.com", "www.bing.com"}


class _FixedEndpoint:
    """为锁定版本的 DDGS 引擎禁用跳转，并识别上游 HTTP 状态。"""

    def __init__(self, client):
        self.client = client
        self.status: int | None = None
        self.retry_after: str | None = None

    def request(self, method, url, *args, **kwargs):
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.netloc not in _SEARCH_HOSTS:
            raise DDGSException("Unexpected search endpoint")
        response = self.client.request(method, url, *args, **kwargs, follow_redirects=False)
        self.status = response.status_code
        self.retry_after = response.headers.get("retry-after")
        if self.status != 200:
            raise DDGSException("Upstream rejected the request")
        return response


class _Search(DDGS):
    endpoint: _FixedEndpoint | None = None

    def _get_engines(self, category, backend):
        engines = super()._get_engines(category, backend)
        for engine in engines:
            self.endpoint = _FixedEndpoint(engine.http_client.client)
            engine.http_client.client = self.endpoint
        return engines


def _search(args: dict) -> dict:
    ddgs = _Search(timeout=args.pop("timeout"))
    method = {"text": ddgs.text, "images": ddgs.images, "news": ddgs.news}[args.pop("method")]
    try:
        rows = method(**args)
    except TimeoutException:
        return {"status": "timeout"}
    except DDGSException as error:
        endpoint = ddgs.endpoint
        if endpoint and endpoint.status == 429:
            return {"status": "rate_limited", "retry_after": endpoint.retry_after}
        if endpoint and endpoint.status in {408, 504}:
            return {"status": "timeout"}
        if endpoint and endpoint.status == 200 and str(error) == "No results found.":
            return {"status": "success", "results": []}
        return {"status": "failed"}
    return {
        "status": "success",
        "results": [
            {
                key: value
                for key, value in row.items()
                if key in _RESULT_FIELDS
                and (type(value) is int or (isinstance(value, str) and len(value) <= 4096))
            }
            for row in rows[: args["max_results"]]
        ],
    }


if __name__ == "__main__":
    try:
        result = _search(json.load(sys.stdin.buffer))
    except Exception:
        result = {"status": "failed"}
    data = json.dumps(result, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(data if len(data) <= 1024 * 1024 else b'{"status":"failed"}')
