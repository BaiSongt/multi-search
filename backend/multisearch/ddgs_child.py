"""单次 DDGS 查询进程。由 API 终止超时查询，避免 SDK 线程继续执行。"""

import json
import sys
from typing import Literal
from urllib.parse import urlsplit

from ddgs.ddgs import DDGS
from ddgs.exceptions import DDGSException, TimeoutException
from primp import ConnectError, DNSError
from primp import TimeoutError as RequestTimeout

_RESULT_FIELDS = {"title", "href", "url", "body", "date", "image", "thumbnail", "width", "height"}
_SEARCH_HOSTS = {"html.duckduckgo.com", "www.bing.com"}


class _FixedEndpoint:
    """为锁定版本的 DDGS 引擎禁用跳转，并识别上游 HTTP 状态。"""

    def __init__(self, client):
        self.client = client
        self.status: int | None = None
        self.retry_after: str | None = None
        self.transport_failure: Literal["upstream_timeout", "upstream_unavailable"] | None = None

    def request(self, method, url, *args, **kwargs):
        self.status = None
        self.retry_after = None
        self.transport_failure = None
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.netloc not in _SEARCH_HOSTS:
            raise DDGSException("Unexpected search endpoint")
        try:
            response = self.client.request(method, url, *args, **kwargs, follow_redirects=False)
        except RequestTimeout:
            self.transport_failure = "upstream_timeout"
            raise
        except (ConnectError, DNSError):
            self.transport_failure = "upstream_unavailable"
            raise
        self.status = response.status_code
        if self.status == 429:
            retry_after = response.headers.get("retry-after")
            if (
                isinstance(retry_after, str)
                and 0 < len(retry_after) <= 128
                and retry_after.isascii()
                and retry_after.isprintable()
            ):
                self.retry_after = retry_after
        if self.status != 200:
            raise DDGSException("Upstream rejected the request")
        return response


class _Search(DDGS):
    endpoint: _FixedEndpoint | None = None

    def _get_engines(self, category, backend):
        engines = super()._get_engines(category, backend)
        if len(engines) != 1:
            raise DDGSException("Expected exactly one configured search engine")
        (engine,) = engines
        self.endpoint = _FixedEndpoint(engine.http_client.client)
        engine.http_client.client = self.endpoint
        return engines


def _search(args: dict) -> dict:
    ddgs = _Search(timeout=args.pop("timeout"))
    method = {"text": ddgs.text, "images": ddgs.images, "news": ddgs.news}[args.pop("method")]
    try:
        rows = method(**args)
    except DDGSException as error:
        endpoint = ddgs.endpoint
        if endpoint is not None:
            if endpoint.transport_failure:
                return {"status": endpoint.transport_failure}
            if endpoint.status == 429:
                return {"status": "rate_limited", "retry_after": endpoint.retry_after}
            if endpoint.status in {408, 504}:
                return {"status": "upstream_timeout"}
            if endpoint.status not in {None, 200}:
                return {"status": "upstream_unavailable"}
            if endpoint.status == 200 and str(error) == "No results found.":
                return {"status": "success", "results": []}
        if isinstance(error, TimeoutException):
            return {"status": "timeout"}
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
