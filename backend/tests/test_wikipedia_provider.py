import asyncio

import httpx
import pytest

from multisearch import wikipedia_provider
from multisearch.contracts import Modality, ProviderReason, SearchRequest, Source
from multisearch.providers import ProviderFailure
from multisearch.wikipedia_provider import WikipediaProvider

_PAIR = Source.WEB, Modality.TEXT


@pytest.mark.parametrize(
    ("language", "host"),
    [(None, "en.wikipedia.org"), ("en-US", "en.wikipedia.org"), ("zh-HK", "zh.wikipedia.org")],
)
async def test_wikipedia_uses_full_text_search_and_returns_title_and_page_link(language, host):
    def upstream(request):
        assert request.method == "GET"
        assert request.url.host == host
        assert request.url.path == "/w/api.php"
        assert "github.com/BaiSongt/multi-search/issues" in request.headers["user-agent"]
        assert dict(request.url.params) == {
            "action": "query",
            "list": "search",
            "format": "json",
            "formatversion": "2",
            "maxlag": "5",
            "srsearch": "光合作用 & chlorophyll",
            "srwhat": "text",
            "srnamespace": "0",
            "srlimit": "5",
            "srprop": "",
            "srinfo": "",
        }
        return httpx.Response(
            200,
            json={
                "continue": {"sroffset": 5},
                "query": {
                    "search": [
                        {
                            "pageid": 404,
                            "title": "Photosynthesis",
                            "snippet": "Article content",
                            "url": "https://elsewhere.example.org/untrusted",
                            "timestamp": "2026-05-01T00:00:00Z",
                        },
                    ]
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        provider = WikipediaProvider(client)
        request = SearchRequest(
            query="光合作用 & chlorophyll", language=language, domain_allowlist=["wikipedia.org"]
        )
        assert provider.supports_request(request)
        result = await provider.search(request, *_PAIR, 10)
    assert provider.fallback_only and not provider.capabilities.supports_pagination
    assert provider.capabilities.filters[Modality.TEXT].date_range == "unsupported"
    (item,) = result.results
    assert item.title == "Photosynthesis"
    assert item.url == item.canonical_url == f"https://{host}/?curid=404"
    assert item.provider_ids == ["wikipedia"]
    assert item.snippet is None
    assert item.published_at is None
    assert result.reason_codes == ()


@pytest.mark.parametrize(
    "constraints",
    [
        {"language": "ja"},
        {"query": "10.1038/s41586-020-2649-2"},
        {"query": "arXiv:1706.03762"},
        {"region": "HK", "language": "zh"},
        {"time_range": {"from": "2025-01-01", "to": "2025-12-31"}},
        {"domain_allowlist": ["wikipedia.org.example.net"]},
        {"domain_allowlist": ["zh.wikipedia.org"]},
        {"domain_denylist": ["wikipedia.org"]},
        {"network_scope": "private"},
        {"network_scope": "offline"},
    ],
)
async def test_wikipedia_never_requests_an_ineligible_query(constraints):
    def upstream(_request):
        pytest.fail("不适用的请求不应发给 Wikipedia")

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        provider = WikipediaProvider(client)
        request = SearchRequest(**{"query": "Moon", **constraints})
        assert not provider.supports_request(request)
        with pytest.raises(ProviderFailure, match="^failed$"):
            await provider.search(request, *_PAIR, 5)


@pytest.mark.parametrize(
    ("response", "status", "reason", "delay"),
    [
        (httpx.Response(429), "rate_limited", ProviderReason.UPSTREAM_RATE_LIMITED, 5),
        (
            httpx.Response(429, headers={"Retry-After": "0"}),
            "rate_limited",
            ProviderReason.UPSTREAM_RATE_LIMITED,
            5,
        ),
        (
            httpx.Response(429, headers={"Retry-After": "10"}),
            "rate_limited",
            ProviderReason.UPSTREAM_RATE_LIMITED,
            10,
        ),
        (
            httpx.Response(200, json={"error": {"code": "ratelimited", "info": "private detail"}}),
            "rate_limited",
            ProviderReason.UPSTREAM_RATE_LIMITED,
            5,
        ),
        (
            httpx.Response(200, json={"error": {"code": "maxlag"}}, headers={"Retry-After": "7"}),
            "failed",
            ProviderReason.UPSTREAM_UNAVAILABLE,
            7,
        ),
        (
            httpx.Response(200, json={"error": {"code": "maxlag"}}),
            "failed",
            ProviderReason.UPSTREAM_UNAVAILABLE,
            5,
        ),
        (httpx.Response(503), "failed", ProviderReason.UPSTREAM_UNAVAILABLE, 5),
    ],
)
async def test_wikipedia_respects_cooldown_for_http_and_api_errors(
    response, status, reason, delay, monkeypatch
):
    now = 100
    monkeypatch.setattr(wikipedia_provider, "monotonic", lambda: now)
    calls = 0

    def upstream(_request):
        nonlocal calls
        calls += 1
        return response if calls == 1 else httpx.Response(200, json={"query": {"search": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        provider = WikipediaProvider(client)
        request = SearchRequest(query="Moon")
        for elapsed in (0, delay - 1):
            now = 100 + elapsed
            with pytest.raises(ProviderFailure, match=f"^{status}$") as error:
                await provider.search(request, *_PAIR, 5)
            assert error.value.reason_codes == (reason,)
            assert error.value.retry_after == delay - elapsed
        assert calls == 1
        now = 100 + delay
        assert (await provider.search(request, *_PAIR, 5)).results == []
        assert calls == 2


@pytest.mark.parametrize(
    ("response", "status", "reason"),
    [
        (
            httpx.Response(302, headers={"Location": "https://elsewhere.example.org"}),
            "failed",
            ProviderReason.UPSTREAM_UNAVAILABLE,
        ),
        (httpx.Response(504), "timeout", ProviderReason.UPSTREAM_TIMEOUT),
        (
            httpx.Response(200, json={"error": {"code": "search-error"}}),
            "failed",
            ProviderReason.UPSTREAM_UNAVAILABLE,
        ),
        (
            httpx.Response(200, json={"query": {"search": [{"pageid": True, "title": "Invalid"}]}}),
            "failed",
            ProviderReason.UPSTREAM_UNAVAILABLE,
        ),
        (httpx.Response(200, content=b"\xff"), "failed", ProviderReason.UPSTREAM_UNAVAILABLE),
        (
            httpx.Response(200, content=b" " * (1024 * 1024 + 1)),
            "failed",
            ProviderReason.UPSTREAM_UNAVAILABLE,
        ),
    ],
)
async def test_wikipedia_rejects_bad_responses_without_following_redirects(
    response, status, reason
):
    def upstream(request):
        assert request.url.host == "en.wikipedia.org"
        return response

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(upstream), follow_redirects=True
    ) as client:
        with pytest.raises(ProviderFailure, match=f"^{status}$") as error:
            await WikipediaProvider(client).search(SearchRequest(query="Moon"), *_PAIR, 5)
    assert error.value.reason_codes == (reason,)


async def test_wikipedia_marks_a_mix_of_valid_and_invalid_candidates_as_partial():
    response = httpx.Response(
        200,
        json={
            "query": {
                "search": [
                    {"ns": 1, "pageid": 1, "title": "Talk:Moon"},
                    {"ns": 0, "pageid": 2, "title": "Moon"},
                    {"ns": 0, "pageid": 3, "title": "Invalid\0Title"},
                ]
            }
        },
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as client:
        result = await WikipediaProvider(client).search(SearchRequest(query="Moon"), *_PAIR, 5)
    assert [item.title for item in result.results] == ["Moon"]
    assert result.partial
    assert result.warnings == ["wikipedia:invalid_results"]
    assert result.reason_codes == (ProviderReason.UPSTREAM_UNAVAILABLE,)


async def test_wikipedia_serializes_upstream_calls_and_excludes_local_wait_from_timeout():
    started = asyncio.Event()
    finish = asyncio.Event()
    calls = 0

    async def upstream(_request):
        nonlocal calls
        calls += 1
        started.set()
        await finish.wait()
        return httpx.Response(200, json={"query": {"search": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        provider = WikipediaProvider(client)
        request = SearchRequest(query="Moon")
        running = asyncio.create_task(provider.search(request, *_PAIR, 5, 2))
        try:
            await started.wait()
            with pytest.raises(ProviderFailure, match="^timeout$") as error:
                await provider.search(request, *_PAIR, 5, 0.01)
            assert error.value.reason_codes == (ProviderReason.LOCAL_PROVIDER_QUEUE_TIMEOUT,)
            assert calls == 1
        finally:
            finish.set()
            await running
