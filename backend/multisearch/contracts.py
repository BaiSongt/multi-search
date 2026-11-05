from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .evidence_contracts import EvidenceRecord


class Mode(StrEnum):
    AUTO = "auto"
    LOCAL = "local"
    QUICK = "quick"
    STANDARD = "standard"
    EVIDENCE = "evidence"
    DEEP = "deep"


class Source(StrEnum):
    WEB = "web"
    NEWS = "news"
    ACADEMIC = "academic"
    CODE = "code"
    PATENT = "patent"
    LOCAL = "local"


class Modality(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"


class Scope(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"
    OFFLINE = "offline"


class Role(StrEnum):
    DEVELOPER = "developer"
    TENANT_ADMIN = "tenant_admin"
    OPERATIONS_ADMIN = "operations_admin"
    PLATFORM_ADMIN = "platform_admin"
    AUDITOR = "auditor"


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DateRange(InputModel):
    start: date = Field(alias="from")
    end: date = Field(alias="to")

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.start > self.end:
            raise ValueError("日期起点晚于终点")
        return self


class ImageOptions(InputModel):
    min_width: int | None = Field(default=None, ge=1, le=20_000)
    min_height: int | None = Field(default=None, ge=1, le=20_000)
    orientation: Literal["landscape", "portrait", "square"] | None = None
    image_type: Literal["photo", "illustration", "transparent", "icon"] | None = None
    license_policy: Literal["any_with_status", "declared_only", "verified_only"] = "any_with_status"
    source_page_required: bool = True


Domain = Annotated[
    str,
    StringConstraints(max_length=253, to_lower=True, pattern=r"^[a-zA-Z0-9-]+(\.[a-zA-Z0-9-]+)+$"),
]
CorrelationID = Annotated[
    str, StringConstraints(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9._-]+$")
]


class SearchRequest(InputModel):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1024)]
    mode: Mode = Mode.AUTO
    source_types: list[Source] = Field(
        default_factory=lambda: [Source.WEB], min_length=1, max_length=6
    )
    modalities: list[Modality] = Field(
        default_factory=lambda: [Modality.TEXT], min_length=1, max_length=3
    )
    language: str | None = Field(default=None, pattern=r"^[a-zA-Z]{2,3}(-[a-zA-Z0-9]{2,8}){0,2}$")
    region: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    time_range: DateRange | None = None
    max_results: int | None = Field(default=None, ge=1, le=30)
    max_latency_ms: int | None = Field(default=None, ge=100, le=900_000)
    max_pages: int | None = Field(default=None, ge=0, le=30)
    max_search_rounds: int | None = Field(default=None, ge=1, le=5)
    max_llm_tokens: int | None = Field(default=None, ge=0, le=100_000)
    max_image_results: int | None = Field(default=None, ge=1, le=30)
    max_image_metadata_fetches: int | None = Field(default=None, ge=0, le=30)
    require_evidence: bool = False
    image_options: ImageOptions | None = None
    network_scope: Scope = Scope.PUBLIC
    domain_allowlist: list[Domain] = Field(default_factory=list, max_length=30)
    domain_denylist: list[Domain] = Field(default_factory=list, max_length=30)
    cache_policy: Literal["use", "refresh", "bypass"] = "use"
    request_id: CorrelationID | None = None
    idempotency_key: CorrelationID | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if len(set(self.modalities)) != len(self.modalities):
            raise ValueError("modalities 不能重复")
        if len(set(self.source_types)) != len(self.source_types):
            raise ValueError("source_types 不能重复")
        if self.image_options is not None and Modality.IMAGE not in self.modalities:
            raise ValueError("image_options 需要 image 模态")
        return self


class ImageAttribution(BaseModel):
    original_url: str
    thumbnail_url: str | None = None
    source_page_url: str | None = None
    source_site_name: str | None = None
    source_site_domain: str | None = None
    search_engines: list[str] = Field(default_factory=list)
    width: int | None = None
    height: int | None = None
    mime_type: str | None = None
    creator: str | None = None
    copyright_holder: str | None = None
    license_name: str | None = None
    license_url: str | None = None
    attribution_text: str | None = None
    rights_status: Literal["unknown", "declared_unverified", "verified_license", "conflict"] = (
        "unknown"
    )
    rights_checked_at: datetime | None = None
    rights_evidence_url: str | None = None
    source_page_verified: bool = False
    source_page_status: Literal["unverified", "checked", "invalid"] = "unverified"
    source_checked_at: datetime | None = None
    asset_hash: str | None = None
    asset_hash_algorithm: Literal["sha1"] | None = None
    asset_hash_source: Literal["wikimedia_commons"] | None = None


class Result(BaseModel):
    result_id: str
    title: str
    url: str | None
    canonical_url: str | None
    source_type: Source
    provider_ids: list[str]
    authors: list[str] = Field(default_factory=list)
    doi: str | None = None
    published_at: datetime | None = None
    retrieved_at: datetime
    content_type: str | None = None
    language: str | None = None
    snippet: str | None = None
    relevance_score: float = 0
    evidence_status: Literal["unverified", "ocr_pending", "content_extracted", "cross_checked"] = (
        "unverified"
    )
    evidence_refs: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class TextSearchResult(Result):
    kind: Literal["text"] = "text"
    journal: str | None = None
    arxiv_id: str | None = None
    version: str | None = None
    license_name: str | None = None
    license_url: str | None = None
    repository_branch: str | None = None


class ImageSearchResult(Result):
    kind: Literal["image"] = "image"
    image: ImageAttribution


SearchResult = Annotated[TextSearchResult | ImageSearchResult, Field(discriminator="kind")]


class ProviderReason(StrEnum):
    UPSTREAM_CAPTCHA = "upstream_captcha"
    UPSTREAM_RATE_LIMITED = "upstream_rate_limited"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"


class ProviderRun(BaseModel):
    provider_id: str
    source_type: Source
    modality: Modality
    status: Literal[
        "success", "empty", "partial", "failed", "timeout", "rate_limited", "circuit_open"
    ]
    elapsed_ms: int
    count: int = 0
    attempts: int = 1
    reason_codes: list[ProviderReason] = Field(
        default_factory=list, exclude_if=lambda codes: not codes
    )


class SearchBudget(BaseModel):
    max_latency_ms: int
    max_results: int
    max_image_results: int
    search_rounds: int
    max_pages: int = 0
    max_image_metadata_fetches: int = 0
    image_metadata_fetches: int = 0
    pages_fetched: int = 0
    pages_attempted: int = 0
    llm_tokens: int = 0


class SearchResponse(BaseModel):
    request_id: str
    trace_id: str | None = None
    status: Literal["completed", "partial"]
    mode_requested: Mode
    mode_executed: Mode
    partial: bool
    elapsed_ms: int
    result_count: int
    results: list[SearchResult]
    evidence_records: list[EvidenceRecord] = Field(default_factory=list)
    routing_policy_version: int = 0
    warnings: list[str] = Field(default_factory=list)
    provider_runs: list[ProviderRun] = Field(default_factory=list)
    next_cursor: str | None = None
    cache_hit: bool = False
    budget: SearchBudget


class ErrorBody(BaseModel):
    request_id: str
    code: str
    message: str
    retryable: bool = False
    retry_after_ms: int | None = None
    provider_runs: list[ProviderRun] | None = None
