from datetime import UTC, datetime
from hashlib import sha256

import pytest
from pydantic import ValidationError

from multisearch.ocr_contracts import OCRBBox, OCRInput, OCRPage, OCRResult

CONTENT = b"%PDF-1.7 fixture"
HASH = sha256(CONTENT).hexdigest()
NOW = datetime(2026, 11, 5, tzinfo=UTC)


def request(**changes):
    return OCRInput.model_validate(
        {
            "document_id": "upload-01",
            "content_sha256": HASH,
            "content_type": "application/pdf",
            "content": CONTENT,
            "page_numbers": [1, 3],
            **changes,
        }
    )


def test_local_ocr_bytes_are_checked_and_excluded_from_metadata():
    result = request(languages=["zh", "en"])
    assert result.content == CONTENT
    assert "content" not in result.model_dump()
    assert "%PDF" not in result.model_dump_json()
    with pytest.raises(ValidationError) as error:
        request(content_sha256="a" * 64)
    assert "%PDF" not in str(error.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"content": "https://example.org/doc.pdf"},
        {"url": "https://example.org/doc.pdf"},
        {"document_id": "../../file"},
        {"content_sha256": "a" * 64},
        {"page_numbers": [3, 1]},
        {"page_numbers": [1, 1]},
        {"content_type": "image/png"},
    ],
)
def test_ocr_contract_rejects_urls_mismatched_bytes_and_invalid_pages(changes):
    with pytest.raises(ValidationError):
        request(**changes)


def test_normalized_bbox_and_output_metadata():
    bbox = OCRBBox(left=0.1, top=0.2, right=0.4, bottom=0.8)
    with pytest.raises(ValidationError):
        OCRBBox(left=0.8, top=0.2, right=0.4, bottom=0.8)
    page = OCRPage(
        page_number=1, width_px=100, height_px=120, blocks=[{"text": "结果", "bbox": bbox}]
    )
    result = OCRResult(
        document_id="upload-01",
        content_sha256=HASH,
        engine="fixture",
        engine_version="1",
        extracted_at=NOW,
        status="completed",
        pages=[page],
    )
    assert result.pages[0].blocks[0].bbox == bbox
    with pytest.raises(ValidationError):
        OCRResult.model_validate({**result.model_dump(), "pages": [page, page]})
