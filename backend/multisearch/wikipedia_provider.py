import asyncio
import json
import re
from time import monotonic
from typing import NoReturn
from urllib.parse import urlsplit

import httpx

from .contracts import Modality, ProviderReason, Scope, SearchRequest, Source
from .errors import retry_after_seconds
from .pipeline import _matches_domain, _plain_excerpt, normalize
from .providers import (
    _MAX_UPSTREAM_BYTES,
    FailureStatus,
    ProviderCapabilities,
    ProviderFailure,
    ProviderFilters,
    ProviderResult,
    _locale,
)
from .vertical_providers import academic_identifier

_EDITIONS = {"en": "https://en.wikipedia.org", "zh": "https://zh.wikipedia.org"}
_SITE_FILTER = re.compile(r"(?<!\S)site:([a-z0-9.-]+)(?=\s|$)", re.I | re.ASCII)
_HEADERS = {
    "Accept": "application/json",
    "Accept-Encoding": "identity",
    "User-Agent": "MultiSearch-Wikipedia/1.0 (https://github.com/BaiSongt/multi-search/issues)",
}
_MAX_RESULTS = 3
_DEFAULT_COOLDOWN = 5.0
_UNAVAILABLE = ProviderReason.UPSTREAM_UNAVAILABLE
_RATE_LIMITED = ProviderReason.UPSTREAM_RATE_LIMITED


class WikipediaProvider:
    provider_id = "wikipedia"
    cache_version = "action-api-v3"
    fallback_only = True
    pairs = frozenset({(Source.WEB, Modality.TEXT)})
    capabilities = ProviderCapabilities(
        {Modality.TEXT: ProviderFilters(language="upstream_best_effort", date_range="unsupported")},
        upstream_concurrency=1,
    )

    def __init__(self, client: httpx.AsyncClient):
        self._client = client
        self._connection = asyncio.Lock()
        self._cooldown: tuple[float, FailureStatus, ProviderReason] = (0.0, "failed", _UNAVAILABLE)

    @staticmethod
    def _edition(request: SearchRequest) -> str | None:
        if (
            request.network_scope != Scope.PUBLIC
            or request.time_range
            or request.region
            or academic_identifier(request.query) is not None
        ):
            return None
        if not request.query.strip():
            return None
        language, _ = _locale(request)
        sites = _SITE_FILTER.findall(request.query.lower())
        domains = request.domain_allowlist or sites
        if not language and len(domains) == 1 and domains[0] == "zh.wikipedia.org":
            language = "zh"
        base_url = _EDITIONS.get(language or "en")
        if not base_url or _matches_domain(base_url, request.domain_denylist):
            return None
        if domains and not _matches_domain(base_url, domains):
            return None
        if sites and (
            not _matches_domain(base_url, sites) or not _SITE_FILTER.sub(" ", request.query).strip()
        ):
            return None
        return base_url

    def supports_request(self, request: SearchRequest) -> bool:
        return self._edition(request) is not None

    def explicitly_requested(self, request: SearchRequest) -> bool:
        domains = request.domain_allowlist or _SITE_FILTER.findall(request.query.lower())
        base_url = self._edition(request)
        return bool(
            base_url
            and len(domains) == 1
            and domains[0] in {"wikipedia.org", urlsplit(base_url).hostname}
        )

    def _defer(
        self, retry_after: float | None, status: FailureStatus, reason: ProviderReason
    ) -> NoReturn:
        delay = retry_after or _DEFAULT_COOLDOWN
        self._cooldown = (monotonic() + delay, status, reason)
        raise ProviderFailure(status, delay, reason_codes=(reason,))

    async def _download(
        self, base_url: str, query: str, limit: int, timeout_s: float
    ) -> list[object]:
        params = {
            "action": "query",
            "list": "search",
            "format": "json",
            "formatversion": 2,
            "maxlag": 5,
            "srsearch": query,
            "srwhat": "text",
            "srnamespace": 0,
            "srlimit": limit,
            "srprop": "snippet",
            "srinfo": "",
        }
        async with self._client.stream(
            "GET",
            f"{base_url}/w/api.php",
            params=params,
            headers=_HEADERS,
            follow_redirects=False,
            timeout=timeout_s,
        ) as response:
            retry_after = retry_after_seconds(response.headers.get("retry-after"))
            if response.status_code == 429:
                self._defer(retry_after, "rate_limited", _RATE_LIMITED)
            if response.status_code == 503:
                self._defer(retry_after, "failed", _UNAVAILABLE)
            if response.status_code in {408, 504}:
                raise ProviderFailure("timeout", reason_codes=(ProviderReason.UPSTREAM_TIMEOUT,))
            if (
                response.status_code != 200
                or response.headers.get("content-encoding", "identity").lower() != "identity"
            ):
                raise ProviderFailure("failed", reason_codes=(_UNAVAILABLE,))
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > _MAX_UPSTREAM_BYTES:
                    raise ProviderFailure("failed", reason_codes=(_UNAVAILABLE,))
                body.extend(chunk)

        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ProviderFailure("failed", reason_codes=(_UNAVAILABLE,))
        error = payload.get("error")
        if isinstance(error, dict):
            if error.get("code") == "ratelimited":
                self._defer(retry_after, "rate_limited", _RATE_LIMITED)
            if error.get("code") == "maxlag":
                self._defer(retry_after, "failed", _UNAVAILABLE)
        query_result = payload.get("query")
        if (
            "error" in payload
            or "errors" in payload
            or not isinstance(query_result, dict)
            or not isinstance(rows := query_result.get("search"), list)
        ):
            raise ProviderFailure("failed", reason_codes=(_UNAVAILABLE,))
        return rows[:limit]

    async def search(
        self,
        request: SearchRequest,
        source: Source,
        modality: Modality,
        limit: int,
        timeout_s: float = 1.0,
    ) -> ProviderResult:
        if (source, modality) not in self.pairs or not (base_url := self._edition(request)):
            raise ProviderFailure("failed")
        attempted = False
        limit = max(1, min(limit, _MAX_RESULTS))
        try:
            async with asyncio.timeout(timeout_s), self._connection:
                retry_until, status, reason = self._cooldown
                delay = retry_until - monotonic()
                if delay > 0:
                    raise ProviderFailure(status, delay, reason_codes=(reason,))
                if timeout_s <= 0:
                    raise TimeoutError
                attempted = True
                query = _SITE_FILTER.sub(" ", request.query).strip()
                rows = await self._download(base_url, query, limit, timeout_s)
        except (TimeoutError, httpx.TimeoutException):
            reason = (
                ProviderReason.UPSTREAM_TIMEOUT
                if attempted
                else ProviderReason.LOCAL_PROVIDER_QUEUE_TIMEOUT
            )
            raise ProviderFailure("timeout", reason_codes=(reason,)) from None
        except (httpx.HTTPError, RecursionError, ValueError):
            raise ProviderFailure("failed", reason_codes=(_UNAVAILABLE,)) from None

        candidates = []
        for row in rows:
            if (
                isinstance(row, dict)
                and row.get("ns", 0) == 0
                and type(page_id := row.get("pageid")) is int
                and 0 < page_id < 2**63
                and isinstance(title := row.get("title"), str)
                and title.strip()
                and "\0" not in title
            ):
                candidates.append(
                    {
                        "title": title,
                        "url": f"{base_url}/?curid={page_id}",
                        "content": _plain_excerpt(row.get("snippet"), 360),
                    }
                )
        results, warnings = normalize(
            candidates, request, self.provider_id, source, modality, limit
        )
        if rows and not results:
            raise ProviderFailure("failed", reason_codes=(_UNAVAILABLE,))
        partial = len(candidates) != len(rows)
        if partial:
            warnings.append("wikipedia:invalid_results")
        return ProviderResult(
            results, warnings, partial=partial, reason_codes=(_UNAVAILABLE,) if partial else ()
        )
