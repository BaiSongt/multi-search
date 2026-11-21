import asyncio
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from unittest.mock import AsyncMock, Mock, call

import httpx
import pytest

from multisearch import safe_fetch
from multisearch.crawl_parser import CrawlParser
from multisearch.dynamic_renderer import RenderedHTML
from multisearch.evidence import EvidenceBuilder
from multisearch.pdf_parser import OCRPending
from multisearch.render_resources import MAX_RESOURCE_BYTES, FetchedResource, ResourceSpec
from multisearch.safe_fetch import FetchedPage, FetchError, SafeFetcher
from multisearch.tracing import bind_trace

ORIGIN = "https://origin.example/doc"
CANONICAL = "https://source.example/doc"
WHEN = datetime(2026, 11, 5, tzinfo=UTC)
SCRIPT_SHELL = '<script>document.body.append("Rendered evidence")</script><main></main>'


class FakeFetcher:
    def __init__(self, text, *, content_type="text/html", charset="utf-8", max_snippet=None):
        self.page = FetchedPage(
            ORIGIN,
            CANONICAL,
            content_type,
            text.encode(charset),
            WHEN,
            charset,
            max_snippet,
            redirect_chain=(ORIGIN, CANONICAL),
        )
        self.options = {}

    async def fetch(self, url, **options):
        self.options = {"url": url, **options}
        return self.page


async def test_focused_fetch_prefers_body_to_navigation_even_without_a_literal_query_match():
    body = "统计公报显示，全市城镇调查失业率为5.1%，比上月下降0.1个百分点。"
    pages = FakeFetcher(f"<main><div>失业率</div><p>{body}</p><p>本地新闻</p></main>")
    for query in ("失业率", "劳动力市场统计口径"):
        result = await EvidenceBuilder(pages).extract(ORIGIN, timeout_s=1, query=query)
        assert result.evidence_text == body
        assert result.locator.text_offset_start == len("失业率\n")
    generic = await EvidenceBuilder(pages).extract(ORIGIN, timeout_s=1)
    assert generic.evidence_text == "失业率"


async def test_focused_fetch_does_not_treat_a_repeated_numeric_title_as_the_body():
    headline = "农民工总量首破3亿 月均收入超5000元"
    body = "报告显示，去年全国外出务工人员的收入和规模均比上年有所增长。"
    page = FakeFetcher(f"<title>{headline}_统计公报</title><div>{headline}</div><p>{body}</p>")
    result = await EvidenceBuilder(page).extract(ORIGIN, timeout_s=1, query="农民工总量 月均收入")
    assert result.evidence_text == body


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


async def test_article_images_use_ranked_publisher_references_and_preserve_image_query():
    fetcher = FakeFetcher(
        """
        <head><base href="/assets/"><meta property="og:image" content="hero.jpg?sig=abc">
        <meta name="twitter:image" content="photo-social.jpg">
        <script type="application/ld+json">
        {"@graph":[{"@type":"Organization","image":"logo.jpg"},
          {"@type":["NewsArticle","ReportageNewsArticle"],"image":
            {"contentUrl":"hero.jpg?sig=abc&utm_source=page","caption":"Hero caption\\ud800"}}]}
        </script></head>
        <body><header><img src="logo.jpg"></header><main>
        <aside><article><img src="unrelated-first-article.jpg"></article></aside><article>
          <p>The actual news.</p><figure>
            <img data-src="hero.jpg?sig=abc" src="data:image/gif;base64,abc" alt="Hero alt">
          </figure>
          <figure><picture><source data-srcset="small.avif 320w, full.avif 1200w">
            <img src="photo.jpg" alt="Second alt"></picture><figcaption>Second caption</figcaption>
          </figure>
          <img src="http://169.254.169.254/secret"><img src="//cdn.example/pixel" height="1">
          <img data-nosnippet src="restricted.jpg"><img hidden src="private.jpg">
          <aside><img src="related.jpg"></aside>
        </article></main></body>
        """
    )
    builder = EvidenceBuilder(fetcher)
    result = await builder.extract(ORIGIN, timeout_s=1)
    assert result.model_dump()["images"] == [
        {
            "url": "https://source.example/assets/hero.jpg?sig=abc&utm_source=page",
            "source": "structured_data",
            "caption": "Hero caption?",
            "alt": "Hero alt",
        },
        {"url": "https://source.example/assets/photo-social.jpg", "source": "page_metadata"},
        {
            "url": "https://source.example/assets/full.avif",
            "source": "article_body",
            "caption": "Second caption",
            "alt": "Second alt",
        },
    ]
    fetcher.page = replace(fetcher.page, body=fetcher.page.body.replace(b"full.avif", b"new.avif"))
    changed = await builder.extract(ORIGIN, timeout_s=1)
    assert changed.content_sha256 == result.content_sha256
    assert changed.evidence_id != result.evidence_id


@pytest.mark.parametrize("directive", ["noimageindex", "max-image-preview: none, max-snippet: 5"])
async def test_image_restrictions_keep_text_evidence(directive):
    fetcher = FakeFetcher(
        f'<head><meta name="robots" content="{directive}">'
        '<meta property="og:image" content="hero.jpg"></head>'
        '<body><article><p>actual evidence</p><img src="photo.jpg"></article></body>'
    )
    result = await EvidenceBuilder(fetcher).extract(ORIGIN, timeout_s=1)
    assert "actual evidence".startswith(result.evidence_text)
    assert not result.images and "images" not in result.model_dump()


async def test_article_image_response_caps_unique_candidates():
    images = "".join(f'<figure><img src="photo-{i}.jpg"></figure>' for i in range(15))
    fetcher = FakeFetcher(f"<article><p>actual evidence</p>{images}</article>")
    result = await EvidenceBuilder(fetcher).extract(ORIGIN, timeout_s=1)
    assert [image.url for image in result.images] == [
        f"https://source.example/photo-{i}.jpg" for i in range(8)
    ]


async def test_plain_text_charset_and_no_publication_guess():
    fetcher = FakeFetcher(
        " Café\r\nCrème   brûlée ", content_type="text/plain", charset="windows-1252"
    )
    result = await EvidenceBuilder(fetcher).extract(ORIGIN, timeout_s=1, query="brûlée")
    assert result.evidence_text == "Crème brûlée"
    assert result.locator.kind == "plain_text" and result.extraction_method == "plain_text"
    assert result.published_at is None
    assert "images" not in result.model_dump()


async def test_empty_or_script_only_page_never_produces_evidence():
    fetcher = FakeFetcher("<script>unsafe()</script><h1>heading only</h1>")
    with pytest.raises(FetchError) as error:
        await EvidenceBuilder(fetcher).extract(ORIGIN, timeout_s=1)
    assert error.value.code == "FETCH_NO_EXCERPT"


async def test_auto_prefers_available_static_excerpt():
    fetcher = FakeFetcher(f"<html><body><p>Static excerpt</p>{SCRIPT_SHELL}</body></html>")
    renderer = Mock(render=AsyncMock())
    builder = EvidenceBuilder(
        fetcher, renderer=renderer, dynamic_hosts=frozenset({"origin.example", "source.example"})
    )
    result = await builder.extract(ORIGIN, timeout_s=1, extraction_mode="auto")
    assert result.evidence_text == "Static excerpt"
    assert result.extraction_method == "static_html"
    renderer.render.assert_not_awaited()


async def test_auto_renders_shell_with_an_approved_external_script():
    spec = ResourceSpec("https://source.example/app.js", "script")
    asset = FetchedResource(spec.url, spec.kind, "text/javascript", "renderArticle()")
    fetcher = FakeFetcher('<main></main><script src="/app.js"></script>')
    fetcher.fetch_resource = AsyncMock(return_value=asset)
    rendered = RenderedHTML("<main><p>Rendered paragraph.</p></main>", "renderer.2")
    renderer = Mock(render=AsyncMock(), render_bundle=AsyncMock(return_value=rendered))
    builder = EvidenceBuilder(
        fetcher,
        renderer=renderer,
        dynamic_hosts=frozenset({"origin.example", "source.example"}),
        dynamic_resources={CANONICAL: (spec,)},
    )

    result = await builder.extract(ORIGIN, timeout_s=1, extraction_mode="auto")

    assert result.evidence_text == "Rendered paragraph." and result.render_bundle_sha256
    fetcher.fetch_resource.assert_awaited_once()
    assert fetcher.fetch_resource.await_args.args == (spec,)
    assert renderer.render_bundle.await_args.args[2] == [asset]
    renderer.render.assert_not_awaited()


@pytest.mark.parametrize("allow_redirect", [False, True])
async def test_auto_reuses_single_approved_redirect_chain(monkeypatch, allow_redirect):
    relay = "https://relay.example/hop"
    visits = []

    async def resolve(host, port):
        return ["8.8.8.8"]

    async def respond(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        host = request.headers["host"]
        visits.append((host, request.url.path))
        if host == "origin.example":
            return httpx.Response(302, headers={"Location": relay})
        if host == "relay.example":
            return httpx.Response(302, headers={"Location": CANONICAL})
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8", "X-Robots-Tag": "max-snippet: 8"},
            text=SCRIPT_SHELL,
        )

    monkeypatch.setattr(safe_fetch, "_resolve", resolve)
    monkeypatch.setattr(safe_fetch, "_transport", lambda: httpx.MockTransport(respond))
    renderer = Mock(
        render=AsyncMock(
            return_value=RenderedHTML("<main><p>Rendered evidence</p></main>", "browser.1")
        )
    )
    hosts = {"origin.example", "source.example"}
    if allow_redirect:
        hosts.add("relay.example")
    builder = EvidenceBuilder(SafeFetcher(), renderer=renderer, dynamic_hosts=frozenset(hosts))
    if allow_redirect:
        result = await builder.extract(ORIGIN, timeout_s=2, extraction_mode="auto")
        assert result.origin_url == ORIGIN and result.canonical_url == CANONICAL
        assert result.locator.kind == "rendered_html_text" and result.evidence_text == "Rendered"
        assert result.extraction_method == "browser_dom"
        renderer.render.assert_awaited_once()
    else:
        with pytest.raises(FetchError, match="来源未包含可提取正文") as error:
            await builder.extract(ORIGIN, timeout_s=2, extraction_mode="auto")
        assert error.value.code == "FETCH_NO_EXCERPT"
        renderer.render.assert_not_awaited()
    assert visits == [
        ("origin.example", "/doc"),
        ("relay.example", "/hop"),
        ("source.example", "/doc"),
    ]


@pytest.mark.parametrize(
    ("markup", "max_snippet", "error_code"),
    [
        ("<main></main>", None, "FETCH_NO_EXCERPT"),
        ('<script type="application/ld+json">{}</script>', None, "FETCH_NO_EXCERPT"),
        ('<script src="/app.js"></script>', None, "FETCH_NO_EXCERPT"),
        (SCRIPT_SHELL, 0, "FETCH_NO_EXCERPT"),
        (
            '<meta name="robots" content="max-snippet: 0">' + SCRIPT_SHELL,
            None,
            "FETCH_SITE_RESTRICTED",
        ),
        ('<meta name="robots" content="nosnippet">' + SCRIPT_SHELL, None, "FETCH_SITE_RESTRICTED"),
    ],
)
async def test_auto_skips_nonrenderable_or_disallowed_shell(markup, max_snippet, error_code):
    fetcher = FakeFetcher(markup, max_snippet=max_snippet)
    renderer = Mock(render=AsyncMock())
    builder = EvidenceBuilder(
        fetcher, renderer=renderer, dynamic_hosts=frozenset({"origin.example", "source.example"})
    )
    with pytest.raises(FetchError) as error:
        await builder.extract(ORIGIN, timeout_s=1, extraction_mode="auto")
    assert error.value.code == error_code
    renderer.render.assert_not_awaited()


@pytest.mark.parametrize(
    "error_code",
    [
        "FETCH_ROBOTS_DENIED",
        "FETCH_SITE_RESTRICTED",
        "FETCH_UNAVAILABLE",
        "FETCH_TARGET_BLOCKED",
        "FETCH_RATE_LIMITED",
    ],
)
async def test_auto_preserves_transport_errors(error_code):
    fetcher = Mock(fetch=AsyncMock(side_effect=FetchError(403, error_code, "fixture")))
    renderer = Mock(render=AsyncMock())
    builder = EvidenceBuilder(
        fetcher, renderer=renderer, dynamic_hosts=frozenset({"origin.example", "source.example"})
    )
    with pytest.raises(FetchError) as error:
        await builder.extract(ORIGIN, timeout_s=1, extraction_mode="auto")
    assert error.value.code == error_code
    fetcher.fetch.assert_awaited_once()
    renderer.render.assert_not_awaited()


async def test_dynamic_evidence_uses_rendered_offsets_and_preserves_original_policy_and_metadata():
    fetcher = FakeFetcher(
        '<html><head><meta name="robots" content="max-snippet: 8"><title>Original title</title>'
        '<meta property="og:image" content="original.jpg">'
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
                '<meta property="og:image" content="forged.jpg"></head><body><main>'
                '<h1>Generated</h1><p>Visible new text</p><img data-src="rendered.jpg">'
                "</main></body></html>",
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
    assert [image.url for image in result.images] == [
        "https://source.example/original.jpg",
        "https://source.example/rendered.jpg",
    ]
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


@pytest.mark.parametrize("mode", ["dynamic", "auto"])
async def test_dynamic_bundle_uses_final_page_and_tracks_fetched_resource_fingerprint(mode):
    script = ResourceSpec("https://source.example/app.js", "script")
    data = ResourceSpec("https://source.example/api", "json")
    script_asset = FetchedResource(
        script.url, "script", "text/javascript", "a" * MAX_RESOURCE_BYTES
    )
    json_asset = FetchedResource(data.url, "json", "application/json", '{"data":1}', 6)
    fetcher = FakeFetcher('<script src="/app.js"></script>' if mode == "auto" else "<p>Shell</p>")
    fetcher.fetch_resource = AsyncMock(side_effect=[script_asset, json_asset])
    rendered = RenderedHTML(
        "<html><head></head><body><p>Generated paragraph</p></body></html>", "renderer.2"
    )
    renderer = Mock(render=AsyncMock(), render_bundle=AsyncMock(return_value=rendered))
    hosts = frozenset({"origin.example", "source.example"})
    builder = EvidenceBuilder(
        fetcher,
        renderer=renderer,
        dynamic_hosts=hosts,
        dynamic_resources={CANONICAL: (script, data)},
    )
    result = await builder.extract(ORIGIN, timeout_s=1, extraction_mode=mode)
    assert result.evidence_text == "Genera"
    assert result.extraction_version == "renderer.2+normalizer.1"
    legacy_inputs = [
        (asset.url, asset.kind, asset.content_type, sha256(asset.body.encode()).hexdigest())
        for asset in [json_asset, script_asset]
    ]
    assert (
        result.render_bundle_sha256
        == sha256(json.dumps(legacy_inputs, separators=(",", ":")).encode()).hexdigest()
    )
    assert renderer.render_bundle.await_args.args[0] == CANONICAL
    assert renderer.render_bundle.await_args.args[2] == [script_asset, json_asset]
    renderer.render.assert_not_awaited()
    for request in fetcher.fetch_resource.await_args_list:
        assert request.kwargs["page_url"] == CANONICAL
        assert request.kwargs["authorized_hosts"] == hosts

    fetcher.fetch_resource.side_effect = [script_asset, replace(json_asset, body='{"data":2}')]
    changed = await builder.extract(ORIGIN, timeout_s=1, extraction_mode=mode)
    assert changed.content_sha256 == result.content_sha256
    assert changed.render_bundle_sha256 != result.render_bundle_sha256
    assert changed.evidence_id != result.evidence_id


async def test_dynamic_cross_bundle_preserves_embedder_policy_and_fingerprints_cors():
    spec = ResourceSpec("https://cdn.example.net/data", "json")
    asset = FetchedResource(
        spec.url,
        "json",
        "application/json",
        "{}",
        allow_origin="*",
        resource_policy="cross-origin",
    )
    fetcher = FakeFetcher("<p>Shell</p>")
    fetcher.page = replace(
        fetcher.page, cross_origin_embedder_policy=('require-corp; report-to="origin report"',)
    )
    fetcher.fetch_resource = AsyncMock(return_value=asset)
    renderer = Mock(
        render_bundle=AsyncMock(
            return_value=RenderedHTML("<html><body><p>Generated</p></body></html>", "renderer.3")
        )
    )
    resource_hosts = frozenset({"cdn.example.net"})
    builder = EvidenceBuilder(
        fetcher,
        renderer=renderer,
        dynamic_hosts=frozenset({"source.example"}),
        dynamic_resource_hosts=resource_hosts,
        dynamic_resources={CANONICAL: (spec,)},
    )
    first = await builder.extract(ORIGIN, timeout_s=1, extraction_mode="dynamic")
    assert renderer.render_bundle.await_args.kwargs["source_coep"] == "require-corp"
    assert fetcher.fetch_resource.await_args.kwargs["authorized_resource_hosts"] == resource_hosts
    assert first.extraction_version == "renderer.3+normalizer.1"
    fetcher.fetch_resource.return_value = replace(asset, allow_origin="https://source.example")
    second = await builder.extract(ORIGIN, timeout_s=1, extraction_mode="dynamic")
    assert first.content_sha256 == second.content_sha256
    assert first.render_bundle_sha256 != second.render_bundle_sha256
    assert first.evidence_id != second.evidence_id

    fetcher.page = replace(
        fetcher.page, cross_origin_embedder_policy=("require-corp", "unsafe-none")
    )
    fetcher.fetch_resource.reset_mock()
    renderer.render_bundle.reset_mock()
    with pytest.raises(FetchError) as invalid:
        await builder.extract(ORIGIN, timeout_s=1, extraction_mode="dynamic")
    assert invalid.value.code == "FETCH_RENDER_POLICY_UNSUPPORTED"
    fetcher.fetch_resource.assert_not_awaited()
    renderer.render_bundle.assert_not_awaited()


async def test_dynamic_bundle_is_not_rendered_if_a_required_resource_is_blocked():
    asset = ResourceSpec("https://source.example/app.js", "script")
    fetcher = FakeFetcher("<p>Shell</p>")
    fetcher.fetch_resource = AsyncMock(side_effect=FetchError(403, "FETCH_ROBOTS_DENIED", "denied"))
    renderer = Mock(render=AsyncMock(), render_bundle=AsyncMock())
    builder = EvidenceBuilder(
        fetcher,
        renderer=renderer,
        dynamic_hosts=frozenset({"source.example"}),
        dynamic_resources={CANONICAL: (asset,)},
    )
    with pytest.raises(FetchError) as error:
        await builder.extract(ORIGIN, timeout_s=1, extraction_mode="dynamic")
    assert error.value.code == "FETCH_ROBOTS_DENIED"
    renderer.render.assert_not_awaited()
    renderer.render_bundle.assert_not_awaited()


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
