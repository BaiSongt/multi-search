from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints, model_validator

from .contracts import CorrelationID, Domain, InputModel, Scope


class JobKind(StrEnum):
    FETCH = "fetch"
    RESEARCH = "research"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


UrlInput = Annotated[str, StringConstraints(min_length=8, max_length=2048)]


class FetchRequest(InputModel):
    url: UrlInput | None = None
    urls: list[UrlInput] = Field(default_factory=list, max_length=20)
    max_latency_ms: int = Field(default=120_000, ge=1_000, le=900_000)
    max_pages: int = Field(default=20, ge=1, le=20)
    network_scope: Scope = Scope.PUBLIC
    domain_allowlist: list[Domain] = Field(default_factory=list, max_length=30)
    domain_denylist: list[Domain] = Field(default_factory=list, max_length=30)
    request_id: CorrelationID | None = None
    idempotency_key: CorrelationID | None = None

    @model_validator(mode="after")
    def one_input(self) -> Self:
        if bool(self.url) == bool(self.urls):
            raise ValueError("请提供 url 或 urls 其中之一")
        if len(set(self.urls)) != len(self.urls):
            raise ValueError("批量 URL 不得重复")
        return self

    def targets(self) -> list[str]:
        return ([self.url] if self.url is not None else self.urls)[: self.max_pages]


class JobError(BaseModel):
    code: str
    message: str


class JobProgress(BaseModel):
    stage: str
    completed: int
    total: int
    pages_fetched: int = 0
    llm_tokens: int = 0
    research_budget: dict[str, int] | None = None


class JobView(BaseModel):
    job_id: UUID
    request_id: str | None = None
    trace_id: str | None = None
    tenant_id: UUID
    kind: JobKind
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    deadline_at: datetime
    attempts: int
    max_attempts: int
    progress: JobProgress
    result: dict | None
    error: JobError | None


class JobAccepted(BaseModel):
    job_id: UUID
    trace_id: str | None = None
    status: JobStatus
    status_url: str
    budget: dict[str, int]
