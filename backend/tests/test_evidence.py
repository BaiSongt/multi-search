import asyncio
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from unittest.mock import AsyncMock, Mock, call

import httpx
import pytest

from multisearch.crawl_parser import CrawlParser
from multisearch.dynamic_renderer import RenderedHTML
from multisearch.evidence import EvidenceBuilder
from multisearch.pdf_parser import OCRPending
from multisearch.safe_fetch import FetchedPage, FetchError
from multisearch.tracing import bind_trace

ORIGIN = "https://origin.example/doc"
CANONICAL = "https://source.example/doc"
WHEN = datetime(2026, 11, 5, tzinfo=UTC)


class FakeFetcher:
    def __init__(self, text, *, content_type="text/html", charset="utf-8", max_snippet=None):
        self.page = FetchedPage(
            ORIGIN, CANONICAL, content_type, text.encode(charset), WHEN, charset, max_snippet
        )
        self.options = {}

    async def fetch(self, url, **options):
        self.options = {"url": url, **options}
        return self.page


async def test_extraction_has_reproducible_hash_offsets_and_original_excerpt():
    fetcher = FakeFetcher(
        """
        <html><head><title>文章标题</title>
        <meta name="citation_doi" content="10.1234/example">
        <meta property="article:published_time" content="2026-03-01T11:00:00+08:00">
        </head><body><nav>站点导航</nav>
        <main><h1>技术说明</h1><p>背景段落。</p><h2>结果</h2>
        <p>原始 <strong>证据</strong> 内容。<script>ignored</script></p>
        <img src="http://169.254.169.254/"><p hidden>hidden</p>
        <iframe src="http://127.0.0.1/">removed</iframe></main>
        </body></html>
        """
    )
    evidence = await EvidenceBuilder(fetcher).extract(
        ORIGIN,
        timeout_s=1,
        query="证据",
        result_id="result-01",
        domain_allowlist=["origin.example"],
    )
    normalized = "技术说明\n背景段落。\n结果\n原始 证据 内容。"
    assert evidence.content_sha256 == sha256(normalized.encode()).hexdigest()
    locator = evidence.locator
    assert normalized[locator.text_offset_start : locator.text_offset_end] == evidence.evidence_text
    assert evidence.evidence_text == "原始 证据 内容。"
    assert locator.kind == "html_text" and locator.section == "结果"
    assert evidence.origin_url == ORIGIN and evidence.canonical_url == CANONICAL
    assert evidence.published_at == datetime(2026, 3, 1, 3, tzinfo=UTC)
    assert evidence.retrieved_at == WHEN and evidence.document_identifier.value == "10.1234/example"
    assert (
        evidence.extraction_method == "static_html"
        and evidence.verification_status == "content_extracted"
    )
    assert fetcher.options["domain_allowlist"] == ["origin.example"]
    repeated = await EvidenceBuilder(fetcher).extract(ORIGIN, timeout_s=1, query="证据")
    assert repeated.evidence_id == evidence.evidence_id


@pytest.mark.parametrize("directive", ["noindex", "nosnippet", "max-snippet: 0"])
async def test_meta_robots_denies_evidence(directive):
    fetcher = FakeFetcher(f'<meta name="robots" content="{directive}"><p>secret</p>')
    with pytest.raises(FetchError) as error:
        await EvidenceBuilder(fetcher).extract(ORIGIN, timeout_s=1)
    assert error.value.code == "FETCH_SITE_RESTRICTED"


async def test_combines_meta_and_header_snippet_limits():
    fetcher = FakeFetcher(
        '<meta name="MultiSearchBot" content="max-snippet: 4"><p>abcdefg</p>', max_snippet=5
    )
    result = await EvidenceBuilder(fetcher).extract(ORIGIN, timeout_s=1)
    assert result.evidence_text == "abcd" and result.locator.text_offset_end == 4


async def test_plain_text_charset_and_no_publication_guess():
    fetcher = FakeFetcher(
        " Café\r\nCrème   brûlée ", content_type="text/plain", charset="windows-1252"
    )
    result = await EvidenceBuilder(fetcher).extract(ORIGIN, timeout_s=1, query="brûlée")
    assert result.evidence_text == "Crème brûlée"
    assert result.locator.kind == "plain_text" and result.extraction_method == "plain_text"
    assert result.published_at is None


async def test_empty_or_script_only_page_never_produces_evidence():
    fetcher = FakeFetcher("<script>unsafe()</script><h1>heading only</h1>")
    with pytest.raises(FetchError) as error:
        await EvidenceBuilder(fetcher).extract(ORIGIN, timeout_s=1)
    assert error.value.code == "FETCH_NO_EXCERPT"


async def test_dynamic_evidence_uses_rendered_offsets_and_preserves_original_policy_and_metadata():
    fetcher = FakeFetcher(
        '<html><head><meta name="robots" content="max-snippet: 8"><title>Original title</title>'
        "</head><body><main><p>Original text</p></main></body></html>",
        max_snippet=10,
    )
    fetcher.page = replace(
        fetcher.page, content_security_policy=("  base-uri 'none'  ", "object-src 'none'")
    )
    renderer = Mock(
        render=AsyncMock(
            return_value=RenderedHTML(
                '<html><head><title>Forged title</title><meta name="robots" content="max-snippet: 20">'
                "</head><body><main><h1>Generated</h1><p>Visible new text</p></main></body></html>",
                "renderer.1",
            )
        )
    )
    hosts = frozenset({"origin.example"})
    builder = EvidenceBuilder(fetcher, renderer=renderer, dynamic_hosts=hosts)
    result = await builder.extract(ORIGIN, timeout_s=2, extraction_mode="dynamic")
    assert result.locator.kind == "rendered_html_text" and result.locator.section == "Generated"
    assert (
        result.extraction_method == "browser_dom"
        and result.extraction_version == "renderer.1+normalizer.1"
    )
    assert result.evidence_text == "Visible" and result.title == "Original title"
    normalized = "Generated\nVisible new text"
    assert (
        normalized[result.locator.text_offset_start : result.locator.text_offset_end] == "Visible"
    )
    assert result.content_sha256 == sha256(normalized.encode()).hexdigest()
    assert renderer.render.call_args.kwargs["source_csp"] == "base-uri 'none', object-src 'none'"
    assert fetcher.options["authorized_hosts"] == hosts

    renderer.render.return_value = RenderedHTML(fetcher.page.body.decode(), "renderer.1")
    rendered_same = await builder.extract(ORIGIN, timeout_s=1, extraction_mode="dynamic")
    static = await builder.extract(ORIGIN, timeout_s=1)
    assert rendered_same.content_sha256 == static.content_sha256
    assert rendered_same.evidence_id != static.evidence_id


async def test_dynamic_extraction_honors_original_and_rendered_robots_and_requires_renderer():
    renderer = Mock(
        render=AsyncMock(
            return_value=RenderedHTML(
                '<meta name="robots" content="nosnippet"><p>Private</p>', "renderer.1"
            )
        )
    )
    fetcher = FakeFetcher('<meta name="robots" content="nosnippet"><p>Private</p>')
    builder = EvidenceBuilder(
        fetcher, renderer=renderer, dynamic_hosts=frozenset({"origin.example"})
    )
    with pytest.raises(FetchError) as original:
        await builder.extract(ORIGIN, timeout_s=1, extraction_mode="dynamic")
    assert original.value.code == "FETCH_SITE_RESTRICTED"
    renderer.render.assert_not_awaited()
    builder.fetcher = FakeFetcher("<p>Public shell</p>")
    with pytest.raises(FetchError) as rendered:
        await builder.extract(ORIGIN, timeout_s=1, extraction_mode="dynamic")
    assert rendered.value.code == "FETCH_SITE_RESTRICTED"

    renderer.render.return_value = None
    with pytest.raises(FetchError) as unavailable:
        await builder.extract(ORIGIN, timeout_s=1, extraction_mode="dynamic")
    assert unavailable.value.code == "FETCH_RENDER_UNAVAILABLE"

    renderer.render.reset_mock()
    builder.fetcher = FakeFetcher("<p>Café</p>", charset="windows-1252")
    with pytest.raises(FetchError) as encoding:
        await builder.extract(ORIGIN, timeout_s=1, extraction_mode="dynamic")
    assert encoding.value.code == "FETCH_CONTENT_UNSUPPORTED"
    renderer.render.assert_not_awaited()


async def test_extract_observes_each_attempt_with_known_type_and_preserves_errors(caplog):
    recorder = Mock()
    builder = EvidenceBuilder(FakeFetcher("<p>Valid text</p>"), record_attempt=recorder)
    await builder.extract(ORIGIN, timeout_s=1)

    fetch = AsyncMock(side_effect=FetchError(403, "FETCH_TARGET_BLOCKED", "blocked"))
    builder.fetcher = Mock(fetch=fetch)
    caplog.set_level(logging.INFO, logger="uvicorn.error.multisearch.telemetry")
    with bind_trace("test-trace"), pytest.raises(FetchError, match="blocked"):
        await builder.extract(ORIGIN, timeout_s=1)
    (trace,) = [json.loads(entry.message) for entry in caplog.records]
    assert trace["trace_id"] == "test-trace" and trace["status"] == "blocked"
    assert trace["error_code"] == "FETCH_TARGET_BLOCKED" and ORIGIN not in caplog.text
    fetch.side_effect = FetchError(403, "FETCH_ROBOTS_DENIED", "robots denied")
    with pytest.raises(FetchError, match="robots denied"):
        await builder.extract(ORIGIN, timeout_s=1)
    fetch.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        await builder.extract(ORIGIN, timeout_s=1)
    fetch.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await builder.extract(ORIGIN, timeout_s=1)

    builder.fetcher = FakeFetcher("%PDF-", content_type="application/pdf")
    parse = AsyncMock(side_effect=FetchError(422, "FETCH_PDF_INVALID", "invalid PDF"))
    builder.pdf = Mock(parse=parse)
    with pytest.raises(FetchError, match="invalid PDF"):
        await builder.extract(ORIGIN, timeout_s=1)
    parse.side_effect = OCRPending()
    with pytest.raises(OCRPending):
        await builder.extract(ORIGIN, timeout_s=1)
    assert recorder.call_args_list == [
        call("text/html", "completed"),
        call(None, "blocked"),
        call(None, "blocked"),
        call(None, "timeout"),
        call(None, "cancelled"),
        call("application/pdf", "failed"),
        call("application/pdf", "ocr_pending"),
    ]


async def test_isolated_crawl_parser_only_ranks_substrings_of_original_text():
    body = "<main><p>First paragraph.</p><p>Selected paragraph.</p></main>"
    fetcher = FakeFetcher(body)

    def service(request):
        assert request.url == "http://crawl4ai:11235/parse"
        assert request.content == body.encode("utf-8")
        assert request.headers["authorization"] == "Bearer fixture-token"
        return httpx.Response(
            200,
            json={"version": "0.9.5", "blocks": ["unsupported claim", "Selected paragraph."]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(service), trust_env=False) as client:
        parser = CrawlParser(client, "http://crawl4ai:11235", "fixture-token")
        result = await EvidenceBuilder(fetcher, parser).extract(ORIGIN, timeout_s=1)
    normalized = "First paragraph.\nSelected paragraph."
    assert result.evidence_text == "Selected paragraph."
    assert result.extraction_method == "crawl4ai_static"
    assert (
        normalized[result.locator.text_offset_start : result.locator.text_offset_end]
        == result.evidence_text
    )
    assert result.content_sha256 == sha256(normalized.encode()).hexdigest()


async def test_crawl_parser_does_not_follow_redirect_and_falls_back():
    calls = []

    def service(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(service), trust_env=False, follow_redirects=True
    ) as client:
        parser = CrawlParser(client, "http://crawl4ai:11235", "fixture-token")
        result = await EvidenceBuilder(FakeFetcher("<p>Public text.</p>"), parser).extract(
            ORIGIN, timeout_s=1
        )
    assert len(calls) == 1
    assert result.extraction_method == "static_html"
    assert result.warnings == ["crawl4ai_parser_unavailable"]
