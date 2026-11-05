import asyncio
from collections import Counter
from datetime import UTC, datetime
from time import monotonic

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from multisearch.contracts import (
    ImageAttribution,
    ImageSearchResult,
    Modality,
    Mode,
    SearchRequest,
    Source,
    TextSearchResult,
)
from multisearch.errors import ApiError
from multisearch.providers import ProviderFailure, ProviderResult
from multisearch.search import SearchService
from multisearch.settings import Settings


class MemoryRedis:
    def __init__(self):
        self.rate = Counter()
        self.cache = {}
        self.fail_rate = False

    async def eval(self, script, keys, key, limit, window):
        if self.fail_rate:
            raise RedisConnectionError("offline")
        if self.rate[key] >= limit:
            return window - 1
        self.rate[key] += 1
        return 0

    async def get(self, key):
        return self.cache.get(key)

    async def set(self, key, value, *, ex):
        self.cache[key] = value


class FakeProvider:
    pairs = frozenset({(Source.WEB, Modality.TEXT), (Source.NEWS, Modality.TEXT)})

    def __init__(self, provider_id, *, handler=None, url="https://example.org/result"):
        self.provider_id = provider_id
        self.handler = handler
        self.url = url
        self.calls = []

    async def search(self, request, source, modality, limit, timeout_s):
        call_number = len(self.calls) + 1
        self.calls.append((request.query, source, modality, limit, timeout_s))
        if self.handler is not None:
            await self.handler()
        return ProviderResult(
            [
                TextSearchResult(
                    result_id=f"{self.provider_id}-result-{call_number}",
                    title=request.query,
                    url=self.url,
                    canonical_url=self.url,
                    source_type=source,
                    provider_ids=[self.provider_id],
                    retrieved_at=datetime.now(UTC),
                ),
            ]
        )


def service(*providers, requests_per_minute=60, max_concurrent=20):
    config = Settings(
        "postgresql+asyncpg://ignored",
        "redis://ignored",
        "pepper",
        "http://ignored",
        requests_per_minute=requests_per_minute,
        max_concurrent=max_concurrent,
    )
    return SearchService(MemoryRedis(), list(providers), config)


async def failed():
    raise ProviderFailure("failed")


async def test_quick_falls_back_and_enforces_mode_caps():
    ddgs = FakeProvider("ddgs", handler=failed)
    searxng = FakeProvider("searxng")
    search = service(searxng, ddgs)

    result = await search.search(
        SearchRequest(query="clock", mode="quick", max_latency_ms=6000, max_results=30),
        "tenant",
        "test",
    )

    assert result.request_id == "test"
    assert result.mode_executed == Mode.QUICK
    assert result.partial and result.result_count == 1
    assert result.budget.max_latency_ms == 4000
    assert result.budget.max_results == 10
    assert result.budget.search_rounds == 1
    assert [run.provider_id for run in result.provider_runs] == ["ddgs", "searxng"]
    assert [run.status for run in result.provider_runs] == ["failed", "success"]
    assert ddgs.calls[0][3] == 10
    assert "PARTIAL_RESULT" in result.warnings
    assert not search.redis.cache


async def test_standard_fuses_provider_provenance_and_reports_missing_pairs():
    search = service(FakeProvider("searxng"), FakeProvider("ddgs"))
    result = await search.search(
        SearchRequest(query="clock", source_types=["web", "patent"]),
        "tenant",
        "test",
    )
    assert result.mode_executed == Mode.STANDARD
    assert result.result_count == 1
    assert set(result.results[0].provider_ids) == {"searxng", "ddgs"}
    assert result.partial
    assert "SOURCE_MODALITY_UNAVAILABLE:patent:text" in result.warnings
    assert search.capabilities()["source_types"]["patent"] == "unavailable"
    assert search.provider_status()[0]["status"] == "healthy"


async def test_standard_retains_both_modalities_under_total_result_limit():
    class MixedProvider(FakeProvider):
        pairs = FakeProvider.pairs | {(Source.WEB, Modality.IMAGE)}

        async def search(self, request, source, modality, limit, timeout_s):
            if modality == Modality.IMAGE:
                return ProviderResult(
                    [
                        ImageSearchResult(
                            result_id="photo",
                            title="Image",
                            url="https://example.org/image",
                            canonical_url="https://example.org/image",
                            source_type=source,
                            provider_ids=[self.provider_id],
                            retrieved_at=datetime.now(UTC),
                            image=ImageAttribution(
                                original_url="https://cdn.example.org/photo.jpg",
                                source_page_url="https://example.org/image",
                            ),
                        ),
                    ]
                )
            result = await super().search(request, source, modality, limit, timeout_s)
            result.results.append(
                result.results[0].model_copy(
                    update={
                        "result_id": "second-text",
                        "canonical_url": "https://example.org/second",
                    }
                )
            )
            return result

    first, second = MixedProvider("searxng"), MixedProvider("ddgs")
    second.pairs = FakeProvider.pairs
    search = service(first, second)
    response = await search.search(
        SearchRequest(query="clock", modalities=["text", "image"], max_results=2),
        "tenant",
        "mixed",
    )
    assert [result.kind for result in response.results] == ["text", "image"]


async def test_cache_is_tenant_scoped_and_local_never_calls_providers():
    provider = FakeProvider("ddgs")
    search = service(provider)
    request = SearchRequest(query="confidential clock", mode="standard", max_results=8)

    first = await search.search(request, "tenant-a", "first")
    cached = await search.search(request, "tenant-a", "cached")
    local_request = request.model_copy(update={"mode": Mode.LOCAL})
    local = await search.search(local_request, "tenant-a", "local")
    other = await search.search(local_request, "tenant-b", "other")

    assert not first.cache_hit
    assert cached.cache_hit and cached.results == first.results
    assert cached.request_id == "cached"
    assert cached.provider_runs == [] and cached.budget.search_rounds == 0
    assert local.cache_hit and local.mode_executed == Mode.LOCAL
    assert other.partial and not other.results and other.warnings == ["LOCAL_CACHE_MISS"]
    assert len(provider.calls) == 1
    assert all("confidential clock" not in key for key in search.redis.cache)


async def test_refresh_and_bypass_have_distinct_cache_behavior():
    provider = FakeProvider("ddgs")
    search = service(provider)
    request = SearchRequest(query="clock")

    first = await search.search(request, "tenant", "first")
    bypass = await search.search(
        request.model_copy(update={"cache_policy": "bypass"}),
        "tenant",
        "bypass",
    )
    cached = await search.search(request, "tenant", "cached")
    refresh = await search.search(
        request.model_copy(update={"cache_policy": "refresh"}),
        "tenant",
        "refresh",
    )
    latest = await search.search(request, "tenant", "latest")

    assert not bypass.cache_hit and not refresh.cache_hit
    assert cached.cache_hit and cached.results == first.results
    assert latest.cache_hit and latest.results == refresh.results
    assert len(provider.calls) == 3


@pytest.mark.parametrize(
    ("options", "code"),
    [
        ({"network_scope": "offline"}, "SOURCE_UNAVAILABLE"),
        ({"network_scope": "private"}, "SOURCE_UNAVAILABLE"),
        ({"source_types": ["local"]}, "SOURCE_MODALITY_UNAVAILABLE"),
        ({"modalities": ["video"]}, "SOURCE_MODALITY_UNAVAILABLE"),
        ({"require_evidence": True}, "EVIDENCE_UNAVAILABLE"),
        ({"mode": "evidence"}, "EVIDENCE_UNAVAILABLE"),
        ({"mode": "deep"}, "MODE_UNAVAILABLE"),
        ({"idempotency_key": "alpha-test"}, "INVALID_REQUEST"),
    ],
)
async def test_unsupported_contract_never_contacts_public_provider(options, code):
    provider = FakeProvider("ddgs")
    search = service(provider)
    with pytest.raises(ApiError) as failure:
        await search.search(SearchRequest(query="clock", **options), "tenant", "test")
    assert failure.value.status == 422
    assert failure.value.code == code
    assert not provider.calls


async def test_same_tenant_identical_work_is_coalesced_but_each_call_is_counted():
    upstream_started, release = asyncio.Event(), asyncio.Event()

    async def slow():
        upstream_started.set()
        await release.wait()

    provider = FakeProvider("ddgs", handler=slow)
    search = service(provider)
    request = SearchRequest(query="clock")
    first = asyncio.create_task(search.search(request, "tenant", "first"))
    await upstream_started.wait()
    second = asyncio.create_task(search.search(request, "tenant", "second"))
    await asyncio.sleep(0)
    release.set()
    responses = await asyncio.gather(first, second)

    assert [response.request_id for response in responses] == ["first", "second"]
    assert len(provider.calls) == 1
    assert list(search.redis.rate.values()) == [2]
    assert not search.flights


async def test_rate_limit_is_tenant_scoped_and_fails_closed():
    provider = FakeProvider("ddgs")
    search = service(provider, requests_per_minute=1)
    request = SearchRequest(query="clock")
    await search.search(request, "tenant-a", "first")

    with pytest.raises(ApiError) as denied:
        await search.search(request, "tenant-a", "second")
    assert denied.value.status == 429 and denied.value.retry_after_ms == 59_999
    await search.search(request, "tenant-b", "third")

    search.redis.fail_rate = True
    with pytest.raises(ApiError) as unavailable:
        await search.search(request, "tenant-c", "fourth")
    assert unavailable.value.status == 503
    assert len(provider.calls) == 2


async def test_twenty_concurrent_calls_keep_results_separate_and_bound_provider_load():
    active, peak = 0, 0

    async def slow():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.02)
        finally:
            active -= 1

    provider = FakeProvider("ddgs", handler=slow)
    search = service(provider)
    # 四个租户检索相同的五个查询，同时验证同租户可复用、跨租户不可合并。
    requests = [SearchRequest(query=f"query {index % 5}") for index in range(20)]
    tasks = [
        search.search(request, f"tenant-{index // 5}", str(index))
        for index, request in enumerate(requests)
    ]
    responses = await asyncio.gather(*tasks)
    assert [response.results[0].title for response in responses] == [
        request.query for request in requests
    ]
    assert [response.request_id for response in responses] == [str(index) for index in range(20)]
    assert len({response.results[0].result_id for response in responses}) == 20
    assert peak == 5 and search.active == 0
    assert sorted(search.redis.rate.values()) == [5, 5, 5, 5]
    for index, request in enumerate(requests):
        local = await search.search(
            request.model_copy(update={"mode": Mode.LOCAL}), f"tenant-{index // 5}", "local"
        )
        assert local.cache_hit and local.results == responses[index].results


async def test_concurrency_limits_release_slots_on_cancellation():
    upstream_started, cancelled = asyncio.Event(), asyncio.Event()

    async def slow():
        upstream_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    provider = FakeProvider("ddgs", handler=slow)
    search = service(provider, max_concurrent=1)
    request = SearchRequest(query="clock")
    first = asyncio.create_task(search.search(request, "tenant-a", "first"))
    await upstream_started.wait()
    with pytest.raises(ApiError) as failure:
        await search.search(request, "tenant-b", "second")
    assert failure.value.status == 429

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert cancelled.is_set()
    assert search.active == 0 and not search.flights
    provider.handler = None
    result = await search.search(request, "tenant-b", "third")
    assert result.result_count == 1


async def test_timeout_cancels_adapter_and_keeps_already_completed_standard_results():
    stopped = asyncio.Event()

    async def slow():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    search = service(FakeProvider("ddgs", handler=slow), FakeProvider("searxng"))
    request = SearchRequest(query="clock", mode="standard", max_latency_ms=100)
    result = await search.search(request, "tenant", "test")

    assert stopped.is_set()
    assert result.partial and result.result_count == 1
    assert result.budget.max_latency_ms == 100
    assert "PROVIDER_TIMEOUT:ddgs:web:text" in result.warnings


async def test_upstream_retry_after_prevents_calls_during_cooldown():
    async def rate_limited():
        raise ProviderFailure("rate_limited", retry_after=30)

    provider = FakeProvider("ddgs", handler=rate_limited)
    search = service(provider)
    for query in ("one", "two"):
        with pytest.raises(ApiError) as failure:
            await search.search(SearchRequest(query=query), "tenant", query)
        assert failure.value.code == "PROVIDER_UNAVAILABLE"
    assert len(provider.calls) == 1
    assert search.provider_status()[0]["status"] == "circuit_open"


async def test_repeated_failures_open_circuit_and_probe_after_cooldown():
    provider = FakeProvider("ddgs", handler=failed)
    search = service(provider)
    for query in ("one", "two", "three", "four"):
        with pytest.raises(ApiError):
            await search.search(SearchRequest(query=query), "tenant", query)
    assert len(provider.calls) == 3
    health = {
        row["source_type"]: row["status"] for row in search.provider_status()[0]["pair_health"]
    }
    assert health == {"web": "circuit_open", "news": "unknown"}

    provider.handler = None
    news = await search.search(
        SearchRequest(query="headlines", source_types=["news"]), "tenant", "separate"
    )
    assert news.result_count == 1
    search.states["ddgs"].circuits[(Source.WEB, Modality.TEXT)].open_until = monotonic() - 1
    health = {
        row["source_type"]: row["status"] for row in search.provider_status()[0]["pair_health"]
    }
    assert health == {"web": "half_open", "news": "healthy"}
    recovered = await search.search(SearchRequest(query="five"), "tenant", "recovered")
    assert recovered.result_count == 1
    assert search.provider_status()[0]["status"] == "healthy"
