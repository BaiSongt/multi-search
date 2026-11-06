import asyncio
import json
import math
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Literal, Protocol

import httpx

from .contracts import Modality, ProviderReason, Scope, SearchRequest, SearchResult, Source
from .pipeline import normalize, public_link
from .settings import Settings

_CATEGORIES = {
    (Source.WEB, Modality.TEXT): "general",
    (Source.WEB, Modality.IMAGE): "images",
    (Source.NEWS, Modality.TEXT): "news",
}
_DDGS_METHODS = {
    "general": ("text", "duckduckgo"),
    "images": ("images", "bing"),
    "news": ("news", "bing"),
}
_MAX_UPSTREAM_BYTES = 1024 * 1024
_ENGINE_REASONS = {
    "captcha": ProviderReason.UPSTREAM_CAPTCHA,
    "too many requests": ProviderReason.UPSTREAM_RATE_LIMITED,
    "rate limit": ProviderReason.UPSTREAM_RATE_LIMITED,
    "rate limited": ProviderReason.UPSTREAM_RATE_LIMITED,
    "429": ProviderReason.UPSTREAM_RATE_LIMITED,
    "http 429": ProviderReason.UPSTREAM_RATE_LIMITED,
    "timeout": ProviderReason.UPSTREAM_TIMEOUT,
    "timed out": ProviderReason.UPSTREAM_TIMEOUT,
}
FailureStatus = Literal["failed", "timeout", "rate_limited"]
LocaleFilter = Literal[
    "unsupported", "upstream_best_effort", "upstream_requires_language", "upstream_requires_region"
]
DateFilter = Literal["unsupported", "metadata_postfilter", "upstream_and_metadata_postfilter"]


@dataclass(frozen=True)
class ImageFilterCapabilities:
    upstream: tuple[str, ...] = ()
    candidate_postfilter: tuple[str, ...] = (
        "min_width",
        "min_height",
        "orientation",
        "source_page_required",
    )
    platform_postfilter: tuple[str, ...] = ("license_policy",)
    unsupported: tuple[str, ...] = ("image_type",)


@dataclass(frozen=True)
class ProviderFilters:
    language: LocaleFilter = "unsupported"
    region: LocaleFilter = "unsupported"
    date_range: DateFilter = "metadata_postfilter"
    date_field: Literal["published_at", "submitted_at", "created_at"] = "published_at"
    exact_identifier_date_range: DateFilter | None = None
    image_options: ImageFilterCapabilities | None = None


@dataclass(frozen=True)
class ProviderCapabilities:
    filters: Mapping[Modality, ProviderFilters]
    network_scope: Scope = Scope.PUBLIC
    requires_api_key: bool = False
    supports_pagination: bool = False
    upstream_concurrency: int | None = None


_IMAGE_FILTERS = ImageFilterCapabilities()
_SEARXNG_FILTERS = ProviderFilters(
    language="upstream_best_effort", region="upstream_requires_language"
)


@dataclass
class ProviderResult:
    results: list[SearchResult]
    warnings: list[str] = field(default_factory=list)
    partial: bool = False
    reason_codes: tuple[ProviderReason, ...] = ()


class ProviderFailure(Exception):
    def __init__(
        self,
        status: FailureStatus,
        retry_after: float | None = None,
        *,
        reason_codes: tuple[ProviderReason, ...] = (),
    ):
        self.status = status
        self.retry_after = retry_after
        if status == "rate_limited" and not reason_codes:
            reason_codes = (ProviderReason.UPSTREAM_RATE_LIMITED,)
        self.reason_codes = reason_codes
        super().__init__(status)


class Provider(Protocol):
    provider_id: str
    pairs: frozenset[tuple[Source, Modality]]

    async def search(
        self,
        request: SearchRequest,
        source: Source,
        modality: Modality,
        limit: int,
        timeout_s: float,
    ) -> ProviderResult: ...


class PublicProvider:
    def __init__(self, enable_images: bool = True):
        self.pairs = frozenset(
            pair for pair in _CATEGORIES if enable_images or pair[1] == Modality.TEXT
        )

    def _validate(self, request: SearchRequest, source: Source, modality: Modality) -> None:
        if request.network_scope != Scope.PUBLIC or (source, modality) not in self.pairs:
            raise ProviderFailure("failed")


def _locale(request: SearchRequest) -> tuple[str | None, str | None]:
    parts = (request.language or "").split("-")
    language = parts[0].lower() or None
    language_region = parts[-1].upper() if len(parts) > 1 and len(parts[-1]) == 2 else None
    return language, request.region or language_region


def _retry_after(header: object) -> float | None:
    if not isinstance(header, str) or not header:
        return None
    try:
        if len(header) <= 10 and header.isascii() and header.isdecimal():
            return float(header)
        target = parsedate_to_datetime(header)
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        return max(0.0, (target - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def _engine_diagnostics(errors: list) -> tuple[FailureStatus, tuple[ProviderReason, ...]]:
    reasons: set[ProviderReason] = set()
    for error in errors:
        detail = (
            error[1].strip().casefold().removeprefix("suspended:").lstrip()
            if isinstance(error, (tuple, list)) and len(error) >= 2 and isinstance(error[1], str)
            else ""
        )
        reasons.add(_ENGINE_REASONS.get(detail, ProviderReason.UPSTREAM_UNAVAILABLE))
    status: FailureStatus = "failed"
    if reasons == {ProviderReason.UPSTREAM_TIMEOUT}:
        status = "timeout"
    elif reasons == {ProviderReason.UPSTREAM_RATE_LIMITED}:
        status = "rate_limited"
    return status, tuple(reason for reason in ProviderReason if reason in reasons)


class SearXNGProvider(PublicProvider):
    provider_id = "searxng"
    cache_version = "2026.11.2-cccbc9e15-diagnostics2"
    capabilities = ProviderCapabilities(
        {
            Modality.TEXT: _SEARXNG_FILTERS,
            Modality.IMAGE: replace(_SEARXNG_FILTERS, image_options=_IMAGE_FILTERS),
        }
    )

    def __init__(self, base_url: str, client: httpx.AsyncClient, enable_images: bool = True):
        super().__init__(enable_images)
        self._url = f"{base_url.rstrip('/')}/search"
        self._client = client

    async def search(
        self,
        request: SearchRequest,
        source: Source,
        modality: Modality,
        limit: int,
        timeout_s: float,
    ) -> ProviderResult:
        self._validate(request, source, modality)
        category = _CATEGORIES[source, modality]
        # 上游 JSON 的引擎错误按界面语言翻译，单独固定诊断语言。
        data = {
            "q": request.query,
            "categories": category,
            "format": "json",
            "safesearch": "1",
            "locale": "en",
        }
        language, region = _locale(request)
        if language:
            data["language"] = f"{language}-{region}" if region else request.language or language
        try:
            async with (
                asyncio.timeout(timeout_s),
                self._client.stream(
                    "POST",
                    self._url,
                    data=data,
                    timeout=timeout_s,
                    follow_redirects=False,
                    headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                ) as response,
            ):
                if response.status_code == 429:
                    raise ProviderFailure(
                        "rate_limited",
                        _retry_after(response.headers.get("retry-after")),
                        reason_codes=(ProviderReason.UPSTREAM_RATE_LIMITED,),
                    )
                if response.status_code in {408, 504}:
                    raise ProviderFailure(
                        "timeout", reason_codes=(ProviderReason.UPSTREAM_TIMEOUT,)
                    )
                if 500 <= response.status_code < 600:
                    raise ProviderFailure(
                        "failed", reason_codes=(ProviderReason.UPSTREAM_UNAVAILABLE,)
                    )
                if (
                    response.status_code != 200
                    or response.headers.get("content-encoding", "identity").lower() != "identity"
                ):
                    raise ProviderFailure("failed")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > _MAX_UPSTREAM_BYTES:
                        raise ProviderFailure("failed")
                    body.extend(chunk)
            payload = json.loads(body)
        except (TimeoutError, httpx.TimeoutException) as error:
            raise ProviderFailure(
                "timeout", reason_codes=(ProviderReason.UPSTREAM_TIMEOUT,)
            ) from error
        except httpx.HTTPError as error:
            raise ProviderFailure(
                "failed", reason_codes=(ProviderReason.UPSTREAM_UNAVAILABLE,)
            ) from error
        except (RecursionError, ValueError) as error:
            raise ProviderFailure("failed") from error
        if not isinstance(payload, dict) or not isinstance(rows := payload.get("results"), list):
            raise ProviderFailure("failed")
        unavailable = payload.get("unresponsive_engines", [])
        if not isinstance(unavailable, list):
            raise ProviderFailure("failed")
        failure_status, reason_codes = _engine_diagnostics(unavailable)
        if unavailable and not rows:
            raise ProviderFailure(failure_status, reason_codes=reason_codes)
        expected_rows = [
            row
            for row in rows
            if isinstance(row, dict) and row.get("category") in (None, "", category)
        ]
        results, warnings = normalize(
            expected_rows, request, self.provider_id, source, modality, limit
        )
        if rows and not results:
            # 区分请求过滤后的空结果和上游没有提供本模态的有效候选。
            links = (
                (row.get("img_src") for row in expected_rows)
                if modality == Modality.IMAGE
                else (row.get("url") or row.get("href") for row in expected_rows)
            )
            if not any(public_link(link) for link in links):
                raise ProviderFailure(
                    failure_status,
                    reason_codes=reason_codes or (ProviderReason.UPSTREAM_UNAVAILABLE,),
                )
        mismatched_categories = len(expected_rows) != len(rows)
        if mismatched_categories:
            warnings.append("searxng:unexpected_result_category")
        if unavailable:
            warnings.append(f"searxng:unresponsive_engines:{len(unavailable)}")
        if region and not language:
            warnings.append("searxng:region_requires_language")
        elif language:
            warnings.append("searxng:locale_best_effort")
        if (
            modality == Modality.IMAGE
            and request.image_options
            and request.image_options.image_type
        ):
            warnings.append("searxng:image_type_unsupported")
        return ProviderResult(
            results,
            warnings,
            partial=bool(unavailable) or mismatched_categories,
            reason_codes=reason_codes,
        )


class DDGSProvider(PublicProvider):
    provider_id = "ddgs"
    cache_version = "9.16.1-ddg-bing"
    capabilities = ProviderCapabilities(
        {
            Modality.TEXT: ProviderFilters(
                language="upstream_requires_region", region="upstream_best_effort"
            ),
            Modality.IMAGE: ProviderFilters(image_options=_IMAGE_FILTERS),
        }
    )

    async def search(
        self,
        request: SearchRequest,
        source: Source,
        modality: Modality,
        limit: int,
        timeout_s: float,
    ) -> ProviderResult:
        self._validate(request, source, modality)
        method, engine = _DDGS_METHODS[_CATEGORIES[source, modality]]
        language, region = _locale(request)
        if region == "GB" and engine == "duckduckgo":
            region = "UK"
        args = {
            "method": method,
            "backend": engine,
            "query": request.query,
            "max_results": limit,
            "region": f"{region.lower()}-{language or 'en'}" if region else "us-en",
            "timeout": max(1, math.ceil(timeout_s)),
        }
        warnings = []
        if request.language or request.region:
            if modality == Modality.IMAGE:
                warnings.append("ddgs:locale_unsupported")
            elif not region and language != "en":
                warnings.append("ddgs:locale_requires_region")
            else:
                warnings.append("ddgs:locale_best_effort")
        if (
            modality == Modality.IMAGE
            and request.image_options
            and request.image_options.image_type
        ):
            warnings.append("ddgs:image_type_unsupported")
        # 子进程仅接收解释器运行所需的环境变量，不继承应用密钥和代理设置。
        env = {
            key: os.environ[key]
            for key in ("PATH", "SYSTEMROOT", "TMP", "TEMP")
            if key in os.environ
        }
        try:
            async with asyncio.timeout(timeout_s):
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    str(Path(__file__).with_name("ddgs_child.py")),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=env,
                )
                try:
                    output, _ = await process.communicate(json.dumps(args).encode("utf-8"))
                finally:
                    if process.returncode is None:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                        await process.communicate()
            if process.returncode or len(output) > _MAX_UPSTREAM_BYTES:
                raise ProviderFailure("failed")
            payload = json.loads(output)
        except TimeoutError as error:
            raise ProviderFailure("timeout") from error
        except (OSError, RecursionError, ValueError) as error:
            raise ProviderFailure("failed") from error
        if not isinstance(payload, dict):
            raise ProviderFailure("failed")
        match payload.get("status"):
            case "timeout":
                raise ProviderFailure("timeout")
            case "upstream_timeout":
                raise ProviderFailure("timeout", reason_codes=(ProviderReason.UPSTREAM_TIMEOUT,))
            case "rate_limited":
                raise ProviderFailure(
                    "rate_limited",
                    _retry_after(payload.get("retry_after")),
                    reason_codes=(ProviderReason.UPSTREAM_RATE_LIMITED,),
                )
            case "upstream_unavailable":
                raise ProviderFailure("failed", reason_codes=(ProviderReason.UPSTREAM_UNAVAILABLE,))
            case "success" if isinstance(payload.get("results"), list):
                rows = payload["results"]
            case _:
                raise ProviderFailure("failed")
        results, result_warnings = normalize(
            rows,
            request,
            self.provider_id,
            source,
            modality,
            limit,
            ddgs_engine=engine,
        )
        return ProviderResult(results, warnings + result_warnings)


def create_providers(config: Settings, client: httpx.AsyncClient) -> list[Provider]:
    providers: list[Provider] = [SearXNGProvider(config.searxng_url, client, config.enable_images)]
    if config.enable_ddgs:
        providers.append(DDGSProvider(config.enable_images))
    return providers
