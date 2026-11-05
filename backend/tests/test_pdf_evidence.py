import asyncio
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.actions import JavaScript
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from multisearch import pdf_parser
from multisearch.evidence import EvidenceBuilder
from multisearch.pdf_child import PDF_MAX_OUTPUT, PDF_MAX_PAGES
from multisearch.pdf_parser import OCRPending, PdfParser
from multisearch.safe_fetch import FetchedPage, FetchError

ORIGIN = "https://example.org/reference.pdf"


def make_pdf(*pages: str, encrypted: bool = False, active_content: bool = False) -> bytes:
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
            NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
        }
    )
    for value in pages:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        if not value:
            continue
        stream = DecodedStreamObject()
        encoded = (
            value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("cp1252")
        )
        stream.set_data(b"BT /F1 12 Tf 72 700 Td (" + encoded + b") Tj ET")
        page.replace_contents(stream)
    if active_content:
        writer.add_open_action(JavaScript('app.launchURL("https://example.org/external", true);'))
    if encrypted:
        writer.encrypt("sample-password")
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def make_image_pdf(*, inline: bool, blank: bool = False) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    stream = DecodedStreamObject()
    if blank:
        stream.set_data(b"q Q\n")
    elif inline:
        stream.set_data(b"q 10 0 0 10 0 0 cm BI /W 1 /H 1 /BPC 8 /CS /RGB ID \xff\x00\x00 EI Q\n")
    else:
        image = DecodedStreamObject()
        image.set_data(b"\xff\x00\x00")
        image.update(
            {
                NameObject("/Type"): NameObject("/XObject"),
                NameObject("/Subtype"): NameObject("/Image"),
                NameObject("/Width"): NumberObject(1),
                NameObject("/Height"): NumberObject(1),
                NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                NameObject("/BitsPerComponent"): NumberObject(8),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/XObject"): DictionaryObject(
                    {NameObject("/Im0"): writer._add_object(image)}
                ),
            }
        )
        stream.set_data(b"q 10 0 0 10 0 0 cm /Im0 Do Q\n")
    page.replace_contents(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


class PDFFetcher:
    def __init__(self, body: bytes, *, max_snippet: int | None = None):
        self.body = body
        self.max_snippet = max_snippet

    async def fetch(self, *_args, **_kwargs):
        return FetchedPage(
            ORIGIN,
            ORIGIN,
            "application/pdf",
            self.body,
            datetime(2026, 11, 6, tzinfo=UTC),
            max_snippet=self.max_snippet,
        )


async def test_pdf_locator_keeps_original_page_number_and_page_relative_offsets():
    body = make_pdf("Ordinary introduction", "", "beginning " * 40 + "Café needle near the end")
    builder = EvidenceBuilder(PDFFetcher(body, max_snippet=40))

    record = await builder.extract(ORIGIN, timeout_s=3, query="needle")

    assert record.content_type == "application/pdf"
    assert record.extraction_method == "pypdf" and record.extraction_version.startswith("6.21.4")
    assert record.locator.kind == "pdf_text" and record.locator.page_number == 3
    assert "needle" in record.evidence_text and len(record.evidence_text) <= 40
    normalized = "beginning " * 40 + "Café needle near the end"
    assert (
        normalized[record.locator.text_offset_start : record.locator.text_offset_end]
        == record.evidence_text
    )
    repeated = await builder.extract(ORIGIN, timeout_s=3, query="needle")
    first_page = await builder.extract(ORIGIN, timeout_s=3, query="ordinary")
    assert repeated.evidence_id == record.evidence_id
    assert first_page.evidence_id != record.evidence_id and first_page.locator.page_number == 1
    assert first_page.content_sha256 == record.content_sha256


async def test_pdf_active_content_is_ignored_and_page_count_is_bounded():
    body = make_pdf(
        "Public fixture.", *([""] * (PDF_MAX_PAGES - 1)), "Excluded page", active_content=True
    )
    parsed = await PdfParser().parse(body, timeout_s=3)
    assert parsed.truncated and len(parsed.pages) == PDF_MAX_PAGES
    assert parsed.pages[0] == "Public fixture."
    assert all("launchURL" not in text and "Excluded" not in text for text in parsed.pages)
    record = await EvidenceBuilder(PDFFetcher(body)).extract(ORIGIN, timeout_s=3)
    assert record.warnings == ["pdf_page_or_text_limit_reached"]


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        ("blank", "FETCH_NO_EXCERPT"),
        ("blank_stream", "FETCH_NO_EXCERPT"),
        ("encrypted", "FETCH_PDF_ENCRYPTED"),
        ("malformed", "FETCH_PDF_INVALID"),
    ],
)
async def test_invalid_encrypted_and_blank_pdfs_do_not_emit_evidence(kind, code):
    body = {
        "blank": lambda: make_pdf(""),
        "blank_stream": lambda: make_image_pdf(inline=False, blank=True),
        "encrypted": lambda: make_pdf("Hidden", encrypted=True),
        "malformed": lambda: b"%PDF-1.7\nnot a valid cross reference or trailer",
    }[kind]()
    with pytest.raises(FetchError) as error:
        await EvidenceBuilder(PDFFetcher(body)).extract(ORIGIN, timeout_s=3)
    assert error.value.code == code


@pytest.mark.parametrize("inline", [True, False])
async def test_image_only_pdf_requires_ocr_without_emitting_fake_evidence(inline):
    body = make_image_pdf(inline=inline)
    parsed = await PdfParser().parse(body, timeout_s=3)
    assert parsed.has_visual_content and not any(parsed.pages)
    with pytest.raises(OCRPending) as error:
        await EvidenceBuilder(PDFFetcher(body)).extract(ORIGIN, timeout_s=3)
    assert error.value.code == "OCR_PENDING" and not error.value.retryable
    with pytest.raises(FetchError) as denied:
        await EvidenceBuilder(PDFFetcher(body, max_snippet=0)).extract(ORIGIN, timeout_s=3)
    assert denied.value.code == "FETCH_NO_EXCERPT"


@pytest.mark.parametrize(
    ("source", "budget", "code"),
    [
        ("import time; time.sleep(60)", 0.15, "TIME_BUDGET_EXCEEDED"),
        (
            f"import sys; sys.stdout.buffer.write(b'x' * {PDF_MAX_OUTPUT + 1})",
            1.5,
            "FETCH_PDF_LIMIT",
        ),
    ],
)
async def test_pdf_child_time_and_output_limits_terminate_cleanly(
    monkeypatch, tmp_path: Path, source, budget, code
):
    child = tmp_path / "controlled_pdf_child.py"
    child.write_text(source, encoding="utf-8")
    monkeypatch.setattr(pdf_parser, "_CHILD", child)
    async with asyncio.timeout(3):
        with pytest.raises(FetchError) as error:
            await PdfParser().parse(b"%PDF-1.7", timeout_s=budget)
    assert error.value.code == code
