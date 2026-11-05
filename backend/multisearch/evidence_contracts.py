from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

STATIC_EXTRACTION_VERSION = "1"


class DocumentIdentifier(BaseModel):
    type: Literal["doi"]
    value: str


class EvidenceLocator(BaseModel):
    kind: Literal["html_text", "plain_text"]
    section: str | None = None
    text_offset_start: int = Field(ge=0)
    text_offset_end: int = Field(gt=0)
    page_number: None = None


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
    extraction_method: Literal["static_html", "crawl4ai_static", "plain_text"]
    extraction_version: str = STATIC_EXTRACTION_VERSION
    verification_status: Literal["content_extracted"] = "content_extracted"
    access_policy: Literal["metadata_and_excerpt"] = "metadata_and_excerpt"
    warnings: list[str] = Field(default_factory=list)
