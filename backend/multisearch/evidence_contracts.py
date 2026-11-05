from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

STATIC_EXTRACTION_VERSION = "1"
type FetchStatus = Literal["blocked", "cancelled", "completed", "failed", "ocr_pending", "timeout"]


class DocumentIdentifier(BaseModel):
    type: Literal["doi"]
    value: str


class EvidenceLocator(BaseModel):
    kind: Literal["html_text", "plain_text", "pdf_text"]
    section: str | None = None
    text_offset_start: int = Field(ge=0)
    text_offset_end: int = Field(gt=0)
    page_number: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_page(self) -> Self:
        if (self.kind == "pdf_text") != (self.page_number is not None):
            raise ValueError("仅 PDF 文本证据使用页码")
        return self


class EvidenceRecord(BaseModel):
    evidence_id: str
    result_id: str | None = None
    origin_url: str
    canonical_url: str
    document_identifier: DocumentIdentifier | None = None
    retrieved_at: datetime
    published_at: datetime | None = None
    title: str | None = None
    content_type: str
    evidence_text: str
    locator: EvidenceLocator
    content_sha256: str
    extraction_method: Literal["static_html", "crawl4ai_static", "plain_text", "pypdf"]
    extraction_version: str = STATIC_EXTRACTION_VERSION
    verification_status: Literal["content_extracted"] = "content_extracted"
    access_policy: Literal["metadata_and_excerpt"] = "metadata_and_excerpt"
    warnings: list[str] = Field(default_factory=list)
