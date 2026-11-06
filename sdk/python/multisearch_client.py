"""MultiSearch REST API 的 Python 3.12 标准库客户端。"""

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from ipaddress import ip_address
from time import monotonic
from typing import Literal, NotRequired, TypedDict, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID

Mode = Literal["auto", "local", "quick", "standard", "evidence", "deep"]
SourceType = Literal["web", "news", "academic", "code", "patent", "local"]
Modality = Literal["text", "image", "video"]
NetworkScope = Literal["public", "private", "offline"]
Availability = Literal["available", "unavailable"]
JobStatus = Literal[
    "queued", "running", "cancelling", "completed", "partial", "failed", "cancelled"
]
type JsonObject = dict[str, object]

_CORRELATION = re.compile(r"[A-Za-z0-9._-]{1,128}")
_TRACE = re.compile(r"[a-fA-F0-9]{32}")
_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_PROVIDER_REASON = re.compile(r"(?:upstream|local_provider)_[a-z_]{1,40}")
_BEARER = re.compile(r"[A-Za-z0-9._~+/=-]{1,512}")
_BODY_LIMIT = 32_768
_MAX_RESPONSE_LIMIT = 16 * 1024 * 1024
_DEFAULT_RESPONSE_LIMIT = 4 * 1024 * 1024


DateRange = TypedDict("DateRange", {"from": str, "to": str})


class ImageOptions(TypedDict, total=False):
    min_width: int
    min_height: int
    orientation: Literal["landscape", "portrait", "square"]
    image_type: Literal["photo", "illustration", "transparent", "icon"]
    license_policy: Literal["any_with_status", "declared_only", "verified_only"]
    source_page_required: bool


class RequestMetadata(TypedDict, total=False):
    request_id: str
    idempotency_key: str


class SearchOptions(RequestMetadata, total=False):
    source_types: list[SourceType]
    modalities: list[Modality]
    language: str
    region: str
    time_range: DateRange
    max_results: int
    max_latency_ms: int
    max_pages: int
    max_search_rounds: int
    max_llm_tokens: int
    max_image_results: int
    max_image_metadata_fetches: int
    require_evidence: bool
    image_options: ImageOptions
    network_scope: NetworkScope
    domain_allowlist: list[str]
    domain_denylist: list[str]
    cache_policy: Literal["use", "refresh", "bypass"]


class SearchRequest(SearchOptions):
    query: str
    mode: NotRequired[Mode]


class ResearchRequest(SearchOptions):
    query: str
    mode: NotRequired[Literal["auto", "deep"]]


class FetchOptions(RequestMetadata, total=False):
    max_latency_ms: int
    max_pages: int
    network_scope: Literal["public"]
    domain_allowlist: list[str]
    domain_denylist: list[str]


class FetchOne(FetchOptions):
    url: str


class FetchBatch(FetchOptions):
    urls: list[str]


type FetchRequest = FetchOne | FetchBatch


class AvailableCombination(TypedDict):
    source_type: SourceType
    modality: Modality


class Capabilities(TypedDict):
    modes: dict[Mode, Availability]
    source_types: dict[SourceType, Availability]
    modalities: dict[Modality, Availability]
    network_scopes: dict[NetworkScope, Availability]
    combinations: list[AvailableCombination]
    fetch: Availability
    research: Availability
    ocr: Availability
    limits: JsonObject


class ImageAttribution(TypedDict):
    original_url: str
    thumbnail_url: str | None
    source_page_url: str | None
    source_site_name: str | None
    source_site_domain: str | None
    search_engines: list[str]
    width: int | None
    height: int | None
    mime_type: str | None
    creator: str | None
    copyright_holder: str | None
    license_name: str | None
    license_url: str | None
    attribution_text: str | None
    rights_status: Literal[
        "unknown", "declared_unverified", "verified_license", "conflict"
    ]
    rights_checked_at: str | None
    rights_evidence_url: str | None
    source_page_verified: bool
    source_page_status: Literal["unverified", "checked", "invalid"]
    source_checked_at: str | None
    asset_hash: str | None
    asset_hash_algorithm: Literal["sha1"] | None
    asset_hash_source: Literal["wikimedia_commons"] | None


class _SearchItem(TypedDict):
    result_id: str
    title: str
    url: str | None
    canonical_url: str | None
    source_type: SourceType
    provider_ids: list[str]
    authors: list[str]
    doi: str | None
    published_at: str | None
    retrieved_at: str
    content_type: str | None
    language: str | None
    snippet: str | None
    relevance_score: float
    evidence_status: Literal["unverified", "content_extracted", "cross_checked", "ocr_pending"]
    evidence_refs: list[str]
    warnings: list[str]


class TextSearchItem(_SearchItem):
    kind: Literal["text"]
    journal: str | None
    arxiv_id: str | None
    version: str | None
    license_name: str | None
    license_url: str | None
    repository_branch: str | None


class ImageSearchItem(_SearchItem):
    kind: Literal["image"]
    image: ImageAttribution


type SearchItem = TextSearchItem | ImageSearchItem


class SearchResponse(TypedDict):
    request_id: str
    status: Literal["completed", "partial"]
    mode_requested: Mode
    mode_executed: Mode
    partial: bool
    elapsed_ms: int
    result_count: int
    result_status: NotRequired[
        Literal["has_results", "no_results", "partial_results", "incomplete_no_results"]
    ]
    provider_reason_codes: NotRequired[list[str]]
    results: list[SearchItem]
    evidence_records: list[JsonObject]
    provider_runs: list[JsonObject]
    budget: dict[str, int]
    warnings: list[str]
    routing_policy_version: int
    next_cursor: str | None
    cache_hit: bool


class JobAccepted(TypedDict):
    job_id: str
    trace_id: NotRequired[str | None]
    status: JobStatus
    status_url: str
    budget: dict[str, int]


class JobProgress(TypedDict):
    stage: str
    completed: int
    total: int
    pages_fetched: int
    llm_tokens: int
    research_budget: dict[str, int] | None


class JobView(TypedDict):
    job_id: str
    request_id: NotRequired[str | None]
    trace_id: NotRequired[str | None]
    tenant_id: str
    kind: Literal["fetch", "research"]
    status: JobStatus
    created_at: str
    updated_at: str
    deadline_at: str
    attempts: int
    max_attempts: int
    progress: JobProgress
    result: JsonObject | None
    error: dict[str, str] | None


@dataclass(frozen=True)
class ApiResult[T]:
    body: T
    status: int
    request_id: str | None = None
    trace_id: str | None = None


class MultiSearchError(Exception):
    def __init__(
        self,
        code: str,
        *,
        status: int | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
        retryable: bool = False,
        retry_after_ms: int | None = None,
        provider_reason_codes: tuple[str, ...] = (),
    ):
        super().__init__(
            f"MultiSearch {code}" + (f" (HTTP {status})" if status is not None else "")
        )
        self.code = code
        self.status = status
        self.request_id = request_id
        self.trace_id = trace_id
        self.retryable = retryable
        self.retry_after_ms = retry_after_ms
        self.provider_reason_codes = provider_reason_codes


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 即使重定向目标是同源，也不重新发送 Bearer 凭据。
        return None


def _base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.port == 0
            or any(char.isspace() or ord(char) < 32 or char in "\\?#" for char in value)
        ):
            raise ValueError
        if parsed.scheme == "http" and not ip_address(parsed.hostname).is_loopback:
            raise ValueError
    except ValueError:
        raise ValueError(
            "base_url requires HTTPS or a literal loopback HTTP address"
        ) from None
    path = parsed.path.rstrip("/")
    if not path.endswith("/api/v1"):
        path += "/api/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _matching(value: object, pattern: re.Pattern) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _retry_delay(payload: JsonObject, header: str | None) -> int | None:
    delay = payload.get("retry_after_ms")
    if type(delay) is int and 0 <= delay <= 2**31 - 1:
        return delay
    if header and len(header) <= 64:
        if header.isascii() and header.isdecimal() and len(header) <= 7:
            return int(header) * 1000
        try:
            date = parsedate_to_datetime(header)
            if date.tzinfo is not None:
                return max(
                    0, math.ceil((date - datetime.now(UTC)).total_seconds() * 1000)
                )
        except (TypeError, ValueError, OverflowError):
            pass
    return None


def _encode(
    body: Mapping[str, object],
    request_id: str | None,
    idempotency_key: str | None,
) -> bytes:
    payload = dict(body)
    for name, supplied in (
        ("request_id", request_id),
        ("idempotency_key", idempotency_key),
    ):
        current = payload.get(name)
        if supplied is not None and current is not None and current != supplied:
            raise ValueError(f"Conflicting {name} values")
        value = supplied if supplied is not None else current
        if value is not None and not _matching(value, _CORRELATION):
            raise ValueError(f"Invalid {name}")
        if supplied is not None:
            payload[name] = supplied
    encoded = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode()
    if len(encoded) > _BODY_LIMIT:
        raise MultiSearchError("REQUEST_TOO_LARGE")
    return encoded


class MultiSearchClient:
    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        timeout_s: float = 30,
        max_response_bytes: int = _DEFAULT_RESPONSE_LIMIT,
    ):
        if api_key is not None and not _matching(api_key, _BEARER):
            raise ValueError("Invalid API Key")
        if not math.isfinite(timeout_s) or not 0 < timeout_s <= 900:
            raise ValueError("timeout_s must be in (0, 900]")
        if (
            type(max_response_bytes) is not int
            or not 1 <= max_response_bytes <= _MAX_RESPONSE_LIMIT
        ):
            raise ValueError("max_response_bytes must be in [1, 16777216]")
        self._base_url = _base_url(base_url)
        self._api_key = api_key
        self._timeout = timeout_s
        self._max_response = max_response_bytes
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def _read(self, response, deadline: float, identifiers: dict) -> JsonObject:
        size = response.headers.get("Content-Length", "")
        if (
            size.isascii()
            and size.isdecimal()
            and (len(size) > 9 or int(size) > self._max_response)
        ):
            raise MultiSearchError("RESPONSE_TOO_LARGE", **identifiers)
        body = bytearray()
        while True:
            if monotonic() >= deadline:
                raise MultiSearchError("TIMEOUT", retryable=True, **identifiers)
            try:
                chunk = response.read1(min(65_536, self._max_response + 1 - len(body)))
            except TimeoutError:
                raise MultiSearchError(
                    "TIMEOUT", retryable=True, **identifiers
                ) from None
            except (OSError, HTTPException):
                raise MultiSearchError(
                    "TRANSPORT_ERROR", retryable=True, **identifiers
                ) from None
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > self._max_response:
                raise MultiSearchError("RESPONSE_TOO_LARGE", **identifiers)
        if monotonic() >= deadline:
            raise MultiSearchError("TIMEOUT", retryable=True, **identifiers)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            raise MultiSearchError("INVALID_RESPONSE", **identifiers) from None
        if not isinstance(payload, dict):
            raise MultiSearchError("INVALID_RESPONSE", **identifiers)
        return payload

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Mapping[str, object] | None = None,
        public: bool = False,
        request_id: str | None = None,
        idempotency_key: str | None = None,
        required_key: str,
        expected_status: tuple[int, ...] = (200,),
    ) -> ApiResult[JsonObject]:
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if not public:
            if self._api_key is None:
                raise MultiSearchError("API_KEY_REQUIRED")
            headers["Authorization"] = f"Bearer {self._api_key}"
        data = None
        if body is not None:
            data = _encode(body, request_id, idempotency_key)
            headers["Content-Type"] = "application/json"
        request = Request(
            self._base_url + path, data=data, headers=headers, method=method
        )
        deadline = monotonic() + self._timeout
        try:
            try:
                response = self._opener.open(request, timeout=self._timeout)
            except HTTPError as error:
                response = error
            with response:
                status = response.code
                ids = {
                    "status": status,
                    "request_id": _matching(
                        response.headers.get("X-Request-ID"), _CORRELATION
                    ),
                    "trace_id": _matching(response.headers.get("X-Trace-ID"), _TRACE),
                }
                if 300 <= status < 400:
                    raise MultiSearchError("REDIRECT_BLOCKED", **ids)
                try:
                    payload = self._read(response, deadline, ids)
                except MultiSearchError as error:
                    if error.code == "INVALID_RESPONSE" and status >= 400:
                        raise MultiSearchError(
                            "HTTP_ERROR",
                            retryable=status == 429 or status >= 500,
                            retry_after_ms=_retry_delay(
                                {}, response.headers.get("Retry-After")
                            ),
                            **ids,
                        ) from None
                    raise
                if ids["request_id"] is None:
                    ids["request_id"] = _matching(
                        payload.get("request_id"), _CORRELATION
                    )
                if status >= 400:
                    reasons = payload.get("provider_reason_codes")
                    raise MultiSearchError(
                        _matching(payload.get("code"), _ERROR_CODE) or "HTTP_ERROR",
                        retryable=payload.get("retryable") is True
                        or (
                            "retryable" not in payload
                            and (status == 429 or status >= 500)
                        ),
                        retry_after_ms=_retry_delay(
                            payload, response.headers.get("Retry-After")
                        ),
                        provider_reason_codes=tuple(
                            code
                            for reason in (reasons[:16] if isinstance(reasons, list) else ())
                            if (code := _matching(reason, _PROVIDER_REASON)) is not None
                        ),
                        **ids,
                    )
                key = "job_id" if status == 202 else required_key
                if status not in expected_status or key not in payload:
                    raise MultiSearchError("INVALID_RESPONSE", **ids)
                return ApiResult(payload, status, ids["request_id"], ids["trace_id"])
        except TimeoutError:
            raise MultiSearchError("TIMEOUT", retryable=True) from None
        except URLError as error:
            code = (
                "TIMEOUT"
                if isinstance(error.reason, TimeoutError)
                else "TRANSPORT_ERROR"
            )
            raise MultiSearchError(code, retryable=True) from None
        except (OSError, HTTPException):
            raise MultiSearchError("TRANSPORT_ERROR", retryable=True) from None

    def capabilities(self) -> ApiResult[Capabilities]:
        return cast(
            ApiResult[Capabilities],
            self._request(
                "/capabilities",
                public=True,
                required_key="modes",
            ),
        )

    def search(
        self,
        request: SearchRequest,
        *,
        request_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> ApiResult[SearchResponse | JobAccepted]:
        return cast(
            ApiResult[SearchResponse | JobAccepted],
            self._request(
                "/search",
                method="POST",
                body=request,
                request_id=request_id,
                idempotency_key=idempotency_key,
                required_key="results",
                expected_status=(200, 202),
            ),
        )

    def fetch(
        self,
        request: FetchRequest,
        *,
        request_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> ApiResult[JobAccepted]:
        return cast(
            ApiResult[JobAccepted],
            self._request(
                "/fetch",
                method="POST",
                body=request,
                request_id=request_id,
                idempotency_key=idempotency_key,
                required_key="job_id",
                expected_status=(202,),
            ),
        )

    def research(
        self,
        request: ResearchRequest,
        *,
        request_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> ApiResult[JobAccepted]:
        return cast(
            ApiResult[JobAccepted],
            self._request(
                "/research",
                method="POST",
                body=request,
                request_id=request_id,
                idempotency_key=idempotency_key,
                required_key="job_id",
                expected_status=(202,),
            ),
        )

    def get_job(self, job_id: str | UUID) -> ApiResult[JobView]:
        return cast(
            ApiResult[JobView],
            self._request(
                f"/jobs/{UUID(str(job_id))}",
                required_key="job_id",
            ),
        )

    def cancel_job(self, job_id: str | UUID) -> ApiResult[JobView]:
        return cast(
            ApiResult[JobView],
            self._request(
                f"/jobs/{UUID(str(job_id))}/cancel",
                method="POST",
                required_key="job_id",
            ),
        )
