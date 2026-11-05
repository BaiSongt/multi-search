import asyncio
from urllib.parse import parse_qs

import httpx
import pytest
from ddgs.exceptions import DDGSException

from multisearch.contracts import (
    ImageSearchResult,
    Modality,
    ProviderReason,
    ProviderRun,
    SearchRequest,
    Source,
)
from multisearch.ddgs_child import _FixedEndpoint
from multisearch.pipeline import fuse, normalize, public_link
from multisearch.providers import DDGSProvider, ProviderFailure, SearXNGProvider


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "http://localhost/",
        "http://127.0.0.1/",
        "http://0x7f.0.0.0x1/",
        "http://2130706433/",
        "http://169.254.169.254/",
        "http://[::ffff:192.168.0.2]/",
        "https://user:pw@example.com/",
        "http://safe.example.org\\@127.0.0.1/",
        "http://l%6fcalhost/",
        "http://metadata.google.internal/",
    ],
)
def test_result_metadata_rejects_unsafe_urls(url):
    assert public_link(url) is None


async def test_searxng_uses_image_category_and_preserves_attribution():
    def upstream(request):
        assert request.method == "POST"
        assert str(request.url) == "http://searxng:8080/search"
        assert parse_qs(request.content.decode()) == {
            "q": ["月球影像"],
            "categories": ["images"],
            "format": ["json"],
            "language": ["zh-HK"],
            "safesearch": ["1"],
        }
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "月球",
                        "category": "images",
                        "url": "https://example.org/work?utm_source=provider",
                        "img_src": "https://media.example.net/full.png",
                        "thumbnail_src": "https://media.example.net/small.png",
                        "resolution": "1920 x 1080",
                        "img_format": "png",
                        "engine": "duckduckgo images",
                        "source": "DuckDuckGo",
                        "author": "未经核实的作者",
                        "license": "未经核实的许可",
                    },
                    {"category": "general", "url": "https://example.org/other"},
                ],
                "unresponsive_engines": [["bing images", "timeout"]],
            },
        )

    query = SearchRequest(
        query="月球影像",
        modalities=["image"],
        language="zh-CN",
        region="HK",
        image_options={"min_width": 1200, "orientation": "landscape", "image_type": "photo"},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        result = await SearXNGProvider("http://searxng:8080", client).search(
            query,
            Source.WEB,
            Modality.IMAGE,
            10,
            1,
        )
    assert result.partial
    assert result.reason_codes == (ProviderReason.UPSTREAM_TIMEOUT,)
    assert "searxng:unresponsive_engines:1" in result.warnings
    assert "searxng:image_type_unsupported" in result.warnings
    assert "searxng:unexpected_result_category" in result.warnings
    (item,) = result.results
    assert isinstance(item, ImageSearchResult)
    assert item.canonical_url == "https://example.org/work"
    assert item.image.original_url == "https://media.example.net/full.png"
    assert item.image.thumbnail_url == "https://media.example.net/small.png"
    assert item.image.source_page_url == "https://example.org/work?utm_source=provider"
    assert item.image.search_engines == ["duckduckgo images"]
    assert item.image.width == 1920
    assert item.image.mime_type == "image/png"
    assert item.image.rights_status == "unknown"
    assert item.image.creator is None
    assert item.image.license_name is None
    assert not item.image.source_page_verified


@pytest.mark.parametrize(
    ("response", "status", "retry_after", "reason_codes"),
    [
        (httpx.Response(302, headers={"location": "http://127.0.0.1"}), "failed", None, ()),
        (
            httpx.Response(429, headers={"retry-after": "7"}),
            "rate_limited",
            7,
            (ProviderReason.UPSTREAM_RATE_LIMITED,),
        ),
        (httpx.Response(503), "failed", None, (ProviderReason.UPSTREAM_UNAVAILABLE,)),
        (httpx.Response(504), "timeout", None, (ProviderReason.UPSTREAM_TIMEOUT,)),
        (httpx.Response(200, content=b"X" * (1024 * 1024 + 1)), "failed", None, ()),
        (
            httpx.Response(
                200, json={"results": [], "unresponsive_engines": [["bing", "timeout"]]}
            ),
            "timeout",
            None,
            (ProviderReason.UPSTREAM_TIMEOUT,),
        ),
        (
            httpx.Response(
                200,
                json={
                    "results": [],
                    "unresponsive_engines": [["brave", "Suspended: too many requests"]],
                },
            ),
            "rate_limited",
            None,
            (ProviderReason.UPSTREAM_RATE_LIMITED,),
        ),
    ],
)
async def test_searxng_bounded_upstream_errors(response, status, retry_after, reason_codes):
    calls = []

    def upstream(request):
        calls.append(request.url.host)
        return response

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(upstream),
        follow_redirects=True,
    ) as client:
        with pytest.raises(ProviderFailure) as failure:
            await SearXNGProvider("http://searxng", client).search(
                SearchRequest(query="Moon"),
                Source.WEB,
                Modality.TEXT,
                10,
                1,
            )
    assert calls == ["searxng"]
    assert failure.value.status == status
    assert failure.value.retry_after == retry_after
    assert failure.value.reason_codes == reason_codes


@pytest.mark.parametrize("has_candidates", [False, True])
async def test_searxng_reports_mixed_engine_reasons_without_raw_details(has_candidates):
    def upstream(_request):
        return httpx.Response(
            200,
            json={
                "results": [{"url": "https://example.org/result"}] if has_candidates else [],
                "unresponsive_engines": [
                    ["upstream-detail", "Suspended: too many requests"],
                    ["upstream-detail", "CAPTCHA"],
                    ["upstream-detail", "timed out"],
                    ["upstream-detail", "private upstream diagnostic"],
                    ["upstream-detail", "CAPTCHA"],
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        provider = SearXNGProvider("http://searxng", client)
        args = SearchRequest(query="Moon"), Source.WEB, Modality.TEXT, 10, 1
        if has_candidates:
            result = await provider.search(*args)
            assert result.partial and len(result.results) == 1
            assert result.warnings == ["searxng:unresponsive_engines:5"]
            reason_codes = result.reason_codes
        else:
            with pytest.raises(ProviderFailure, match="^failed$") as failure:
                await provider.search(*args)
            assert failure.value.status == "failed"
            reason_codes = failure.value.reason_codes
    assert reason_codes == tuple(ProviderReason)


def test_provider_run_serializes_only_present_sanitized_reasons():
    fields = {
        "provider_id": "searxng",
        "source_type": Source.WEB,
        "modality": Modality.TEXT,
        "status": "partial",
        "elapsed_ms": 4,
    }
    assert "reason_codes" not in ProviderRun(**fields).model_dump(mode="json")
    run = ProviderRun(**fields, reason_codes=[ProviderReason.UPSTREAM_CAPTCHA])
    assert run.model_dump(mode="json")["reason_codes"] == ["upstream_captcha"]


async def test_private_scope_never_calls_public_adapters(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("非公网作用域尝试调用公开搜索")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unexpected)
    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        for provider in (SearXNGProvider("http://searxng", client), DDGSProvider()):
            with pytest.raises(ProviderFailure):
                await provider.search(
                    SearchRequest(query="内部关键词", network_scope="offline"),
                    Source.WEB,
                    Modality.TEXT,
                    10,
                    1,
                )


def test_image_missing_landing_and_unknown_license_policy():
    original = "https://media.example.org/example.jpg"
    row = {"image": original, "url": original + "#fragment", "title": "示例"}

    def run(**options):
        return normalize(
            [row],
            SearchRequest(query="图片", modalities=["image"], image_options=options),
            "ddgs",
            Source.WEB,
            Modality.IMAGE,
            10,
            ddgs_engine="duckduckgo",
        )

    results, warnings = run()
    assert not results
    assert warnings == ["ddgs:image_source_page_missing:1"]

    results, _ = run(source_page_required=False)
    (item,) = results
    assert isinstance(item, ImageSearchResult)
    assert item.url is None and item.image.source_page_url is None
    assert "image_source_page_missing" in item.warnings

    for policy in ("declared_only", "verified_only"):
        results, warnings = run(source_page_required=False, license_policy=policy)
        assert not results
        assert "ddgs:image_license_verification_unavailable" in warnings

    results, warnings = run(source_page_required=False, min_width=500)
    assert not results
    assert "ddgs:image_dimensions_missing:1" in warnings


def test_exact_dates_and_domain_allowlist():
    rows = [
        {"url": "https://news.example.org/p/1", "date": "2026-03-20T12:30:00+08:00"},
        {"url": "https://news.example.org/p/2"},
        {"url": "https://news.example.org/p/3", "date": "2025-03-20"},
        {"url": "https://badexample.org/p/4", "date": "2026-03-20"},
        {"url": "https://denied.example.org/p/5", "date": "2026-03-20"},
    ]
    results, warnings = normalize(
        rows,
        SearchRequest(
            query="news",
            time_range={"from": "2026-03-01", "to": "2026-03-31"},
            domain_allowlist=["example.org"],
            domain_denylist=["denied.example.org"],
        ),
        "searxng",
        Source.NEWS,
        Modality.TEXT,
        10,
    )
    (item,) = results
    assert item.url == rows[0]["url"]
    assert item.published_at.isoformat() == "2026-03-20T04:30:00+00:00"
    assert warnings == ["searxng:publication_date_missing:1"]


def test_fusion_combines_provenance_without_merging_conflicting_sizes():
    query = SearchRequest(query="图片", modalities=["image"])
    searxng, _ = normalize(
        [
            {
                "url": "https://example.org/item",
                "img_src": "https://cdn.example.org/img",
                "resolution": "2000 x 1000",
                "engine": "bing images",
            },
            {
                "url": "https://example.org/item",
                "img_src": "https://cdn.example.org/img",
                "resolution": "1000 x 500",
                "engine": "bing images",
            },
        ],
        query,
        "searxng",
        Source.WEB,
        Modality.IMAGE,
        10,
    )
    ddgs, _ = normalize(
        [
            {
                "url": "https://EXAMPLE.ORG:443/item?utm_medium=cpc#work",
                "image": "https://cdn.example.org/img?fbclid=1",
                "width": "2000",
                "height": "1000",
            }
        ],
        query,
        "ddgs",
        Source.WEB,
        Modality.IMAGE,
        10,
        ddgs_engine="duckduckgo",
    )
    first, second = fuse([searxng, ddgs], 10, 10)
    assert isinstance(first, ImageSearchResult) and isinstance(second, ImageSearchResult)
    assert first.provider_ids == ["ddgs", "searxng"]
    assert first.image.search_engines == ["bing images", "duckduckgo"]
    assert second.provider_ids == ["searxng"]
    assert second.image.width == 1000
    assert len(fuse([searxng, ddgs], 10, 1)) == 1


def test_ddgs_client_rejects_unapproved_hosts_and_redirects():
    class Client:
        def request(self, method, url, **kwargs):
            assert method == "GET"
            assert url == "https://www.bing.com/news/infinitescrollajax"
            assert kwargs["follow_redirects"] is False
            return httpx.Response(429, headers={"retry-after": "9"})

    client = _FixedEndpoint(Client())
    with pytest.raises(DDGSException):
        client.request("GET", "https://127.0.0.1")
    with pytest.raises(DDGSException):
        client.request("GET", "https://www.bing.com/news/infinitescrollajax")
    assert client.status == 429 and client.retry_after == "9"


async def test_ddgs_passes_selected_engine_and_query_through_stdin(monkeypatch):
    spawn = asyncio.create_subprocess_exec
    script = """
import json, sys
args = json.load(sys.stdin)
assert args["method"] == "images" and args["backend"] == "bing"
assert args["query"] == "月球"
print(json.dumps({"status":"success", "results":[{
    "url":"https://example.org/work", "image":"https://example.net/image.png",
}]}))
"""

    async def fake_child(executable, entry, **kwargs):
        assert entry.endswith("ddgs_child.py")
        assert "API_KEY_HASH_PEPPER" not in kwargs["env"]
        return await spawn(executable, "-c", script, **kwargs)

    monkeypatch.setenv("API_KEY_HASH_PEPPER", "测试值")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_child)
    request = SearchRequest(
        query="月球", modalities=["image"], image_options={"image_type": "photo"}
    )
    response = await DDGSProvider().search(request, Source.WEB, Modality.IMAGE, 3, 3)
    (result,) = response.results
    assert isinstance(result, ImageSearchResult)
    assert result.image.search_engines == ["bing"]
    assert "ddgs:image_type_unsupported" in response.warnings


async def test_ddgs_cancellation_terminates_the_sdk_child(monkeypatch):
    spawn = asyncio.create_subprocess_exec
    started = asyncio.Event()
    process = None

    async def fake_child(executable, entry, **kwargs):
        nonlocal process
        process = await spawn(executable, "-c", "import time; time.sleep(30)", **kwargs)
        started.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_child)
    query = SearchRequest(query="moon")
    task = asyncio.create_task(DDGSProvider().search(query, Source.WEB, Modality.TEXT, 3, 5))
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process is not None and process.returncode is not None
