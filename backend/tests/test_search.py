import asyncio
from collections import Counter
from datetime import UTC, datetime
from time import monotonic
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from multisearch.application import SearchApplication
from multisearch.contracts import (
    ImageAttribution,
    ImageSearchResult,
    Modality,
    Mode,
    ProviderReason,
    SearchRequest,
    Source,
    TextSearchResult,
)
from multisearch.errors import ApiError
from multisearch.evidence_contracts import EvidenceLocator, EvidenceRecord
from multisearch.metering import Admission, SearchPrincipal
from multisearch.observability import Telemetry
from multisearch.policy import ProviderPolicy
from multisearch.providers import ProviderFailure, ProviderResult
from multisearch.routing_policy import DEFAULT_ROUTING, RoutingRules
from multisearch.search import SearchService
from multisearch.settings import Settings
from multisearch.wikipedia_provider import WikipediaProvider


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


async def test_quick_allocates_full_time_to_usable_source_when_fallback_is_open():
    ddgs = FakeProvider("ddgs")
    searxng = FakeProvider("searxng")
    search = service(ddgs, searxng)
    search.states["searxng"].circuits[(Source.WEB, Modality.TEXT)].open_until = monotonic() + 10
    result = await search.search(SearchRequest(query="clock", mode="quick"), "tenant", "test")
    assert result.result_count == 1 and not result.partial
    assert ddgs.calls[0][-1] > 3
    assert not searxng.calls


class TerminalProvider(FakeProvider):
    fallback_only = True
    pairs = frozenset({(Source.WEB, Modality.TEXT)})


@pytest.mark.parametrize("mode", ["quick", "standard"])
async def test_terminal_provider_never_precedes_ordinary_web_candidates(mode):
    ddgs = FakeProvider("ddgs")
    searxng = FakeProvider("searxng")
    terminal = TerminalProvider("wikipedia")
    search = service(terminal, searxng, ddgs)
    search.provider_policies["wikipedia"] = ProviderPolicy(priority=100)

    result = await search.search(SearchRequest(query="clock", mode=mode), "tenant", "test")

    assert not terminal.calls and not result.partial
    if mode == "quick":
        assert [run.provider_id for run in result.provider_runs] == ["ddgs"]
        assert 1.85 < ddgs.calls[0][-1] <= 2.0
        assert not searxng.calls
    else:
        assert [run.provider_id for run in result.provider_runs] == ["searxng", "ddgs"]


@pytest.mark.parametrize("mode", ["quick", "standard"])
async def test_terminal_provider_gets_time_after_slow_primary(mode):
    async def slow():
        await asyncio.Event().wait()

    terminal = TerminalProvider("wikipedia")
    search = service(FakeProvider("ddgs", handler=slow), terminal)
    request = SearchRequest(query="clock", mode=mode, max_latency_ms=1400)

    result = await search.search(request, "tenant", "test")

    assert result.partial and result.results[0].provider_ids == ["wikipedia"]
    assert [(run.status, run.attempts) for run in result.provider_runs] == [
        ("timeout", 1),
        ("success", 1),
    ]
    assert 0 < terminal.calls[0][-1] <= 0.3
    assert not search.redis.cache


async def test_quick_policy_can_disable_terminal_fallback():
    terminal = TerminalProvider("wikipedia")
    search = service(FakeProvider("ddgs", handler=failed), terminal)
    search.routing = DEFAULT_ROUTING.model_copy(
        update={"policy": RoutingRules(quick_fallback=False)}
    )
    request = SearchRequest(query="clock", mode="quick")

    with pytest.raises(ApiError) as failure:
        await search.search(request, "tenant", "test")
    assert failure.value.code == "PROVIDER_UNAVAILABLE"
    assert [run.provider_id for run in failure.value.provider_runs] == ["ddgs"]
    assert not terminal.calls


async def test_ineligible_wikipedia_is_not_advertised_for_filtered_request():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("network"))
    ) as client:
        search = service(WikipediaProvider(client))
        request = SearchRequest(query="tides", language="en", region="US")
        with pytest.raises(ApiError) as failure:
            await search.search(request, "tenant", "test")
        assert failure.value.code == "SOURCE_MODALITY_UNAVAILABLE"


async def test_terminal_upstream_unavailable_hint_prevents_new_attempt():
    async def overloaded():
        raise ProviderFailure(
            "failed", retry_after=5, reason_codes=(ProviderReason.UPSTREAM_UNAVAILABLE,)
        )

    terminal = TerminalProvider("wikipedia", handler=overloaded)
    search = service(terminal)
    for query, expected in (("first", ("failed", 1)), ("second", ("circuit_open", 0))):
        with pytest.raises(ApiError) as failure:
            await search.search(SearchRequest(query=query), "tenant", query)
        assert failure.value.code == "PROVIDER_UNAVAILABLE"
        assert 0 < failure.value.retry_after_ms <= 5000
        (run,) = failure.value.provider_runs
        assert (run.status, run.attempts) == expected
        assert run.reason_codes == ["upstream_unavailable"]
    assert len(terminal.calls) == 1


async def test_quick_falls_back_when_first_image_candidates_fail_requested_license():
    class ImageProvider(FakeProvider):
        pairs = frozenset({(Source.WEB, Modality.IMAGE)})

        async def search(self, request, source, modality, limit, timeout_s):
            self.calls.append((request, timeout_s))
            url = f"https://example.org/{self.provider_id}.png"
            return ProviderResult(
                [
                    ImageSearchResult(
                        result_id=self.provider_id,
                        title="image",
                        url=url,
                        canonical_url=url,
                        source_type=source,
                        provider_ids=[self.provider_id],
                        retrieved_at=datetime.now(UTC),
                        image=ImageAttribution(
                            original_url=url,
                            source_page_url=url,
                            rights_status="verified_license"
                            if self.provider_id == "searxng"
                            else "unknown",
                        ),
                    )
                ]
            )

    ddgs = ImageProvider("ddgs")
    searxng = ImageProvider("searxng")
    search = service(ddgs, searxng)
    strict = await search.search(
        SearchRequest(
            query="image",
            mode="quick",
            modalities=["image"],
            image_options={"license_policy": "verified_only"},
            max_image_metadata_fetches=0,
        ),
        "tenant",
        "strict",
    )
    assert [item.result_id for item in strict.results] == ["searxng"]
    assert [run.provider_id for run in strict.provider_runs] == ["ddgs", "searxng"]
    loose = await search.search(
        SearchRequest(query="image", mode="quick", modalities=["image"]), "tenant", "loose"
    )
    assert [item.result_id for item in loose.results] == ["ddgs"] and len(searxng.calls) == 1


async def test_failed_search_reports_provider_runs_without_recounting_replays():
    search = service(FakeProvider("searxng", handler=failed), FakeProvider("ddgs", handler=failed))
    event_id = uuid4()
    meter = AsyncMock()
    meter.reserve.return_value = Admission(event_id, True)
    telemetry = Telemetry()
    application = SearchApplication(search, meter, telemetry=telemetry)
    principal = SearchPrincipal(uuid4(), uuid4(), uuid4(), "key")
    request = SearchRequest(
        query="private diagnostic fixture", mode="standard", idempotency_key="idem-1"
    )

    with pytest.raises(ApiError) as raised:
        await application.search(request, principal, "req-1")
    error = raised.value
    assert (error.status, error.code) == (502, "PROVIDER_UNAVAILABLE")
    assert {run.provider_id: run.status for run in error.provider_runs} == {
        "ddgs": "failed",
        "searxng": "failed",
    }
    assert request.query not in error.body("req-1").model_dump_json()
    assert meter.finish.await_args.kwargs["error"] is error

    body = error.body("req-1").model_dump(mode="json", exclude={"request_id"})
    meter.reserve.return_value = Admission(event_id, True, {"error": {"status": 502, **body}})
    with pytest.raises(ApiError) as replay:
        await application.search(request, principal, "req-2")
    assert replay.value.provider_runs == error.provider_runs
    assert meter.finish.await_count == 1
    assert telemetry.counters["provider_requests"] == {
        ("ddgs", "failed"): 1,
        ("searxng", "failed"): 1,
    }


async def test_provider_with_partial_candidates_stays_available():
    class PartialProvider(FakeProvider):
        async def search(self, *args):
            result = await super().search(*args)
            result.partial = True
            return result

    provider = PartialProvider("searxng")
    search = service(provider)
    for index in range(5):
        result = await search.search(
            SearchRequest(query=f"fixture {index}", mode="standard"), "tenant", f"req-{index}"
        )
        assert result.partial and result.result_count == 1
        assert result.provider_runs[0].status == "partial"
    assert len(provider.calls) == 5


@pytest.mark.parametrize("mode", ["quick", "standard"])
async def test_academic_identifier_routes_only_to_capable_providers(mode):
    class DoiProvider(FakeProvider):
        pairs = frozenset({(Source.ACADEMIC, Modality.TEXT)})
        identifier_kind = "doi"

    class ArxivProvider(DoiProvider):
        identifier_kind = "arxiv"

    doi = DoiProvider("crossref", handler=failed)
    arxiv = ArxivProvider("arxiv")
    search = service(doi, arxiv)
    query = SearchRequest(query="arXiv:2512.12345v3", source_types=[Source.ACADEMIC], mode=mode)
    result = await search.search(query, "tenant", "request")
    assert [run.provider_id for run in result.provider_runs] == ["arxiv"]
    assert result.result_count == 1 and not doi.calls

    with pytest.raises(ApiError) as unavailable:
        service(doi).plan(query)
    assert unavailable.value.code == "SOURCE_MODALITY_UNAVAILABLE"


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


async def test_strict_image_search_verifies_before_filtering_and_caches_by_budget():
    class ImageProvider(FakeProvider):
        pairs = frozenset({(Source.WEB, Modality.IMAGE)})

        async def search(self, request, source, modality, limit, timeout_s):
            assert request.image_options.license_policy == "any_with_status"
            return ProviderResult(
                [
                    ImageSearchResult(
                        result_id=name,
                        title=name,
                        url=f"https://example.org/{name}",
                        canonical_url=f"https://example.org/{name}",
                        source_type=source,
                        provider_ids=[self.provider_id],
                        retrieved_at=datetime.now(UTC),
                        image=ImageAttribution(
                            original_url=f"https://example.org/{name}.jpg",
                            source_page_url=f"https://example.org/{name}",
                        ),
                    )
                    for name in ("unsupported", "verify")
                ]
            )

    class Verifier:
        def supports(self, image):
            return image.result_id == "verify"

        async def resolve(self, image, timeout_s):
            assert timeout_s > 0
            return image.model_copy(
                update={
                    "image": image.image.model_copy(
                        update={
                            "rights_status": "verified_license",
                            "source_page_verified": True,
                        }
                    )
                }
            )

    search = service(ImageProvider("searxng"))
    search.attribution = Verifier()
    request = SearchRequest(
        query="clock",
        modalities=["image"],
        max_results=1,
        image_options={"license_policy": "verified_only"},
        max_image_metadata_fetches=1,
    )
    response = await search.search(request, "tenant", "verify")
    assert [item.result_id for item in response.results] == ["verify"]
    assert response.budget.image_metadata_fetches == 1
    cached = await search.search(request, "tenant", "cache")
    assert cached.cache_hit and cached.budget.image_metadata_fetches == 0
    unverified = await search.search(
        request.model_copy(update={"max_image_metadata_fetches": 0}), "tenant", "no-lookup"
    )
    assert not unverified.cache_hit and unverified.results == []
    assert "IMAGE_LICENSE_VERIFICATION_UNAVAILABLE" in unverified.warnings


async def test_provider_policy_affects_priority_disabled_state_and_cache():
    ddgs, searxng = FakeProvider("ddgs"), FakeProvider("searxng")
    search = service(ddgs, searxng)
    request = SearchRequest(query="clock", mode="quick")
    original = await search.search(request, "tenant", "original")
    assert original.provider_runs[0].provider_id == "ddgs"

    search.provider_policies["searxng"] = ProviderPolicy(priority=5)
    reordered = await search.search(request, "tenant", "reordered")
    assert not reordered.cache_hit and reordered.provider_runs[0].provider_id == "searxng"

    search.provider_policies["searxng"] = ProviderPolicy(enabled=False, priority=5)
    disabled = await search.search(request, "tenant", "disabled")
    assert not disabled.cache_hit and disabled.provider_runs[0].provider_id == "ddgs"
    assert search.provider_status()[1]["status"] == "disabled"


async def test_evidence_route_respects_page_budget_and_preserves_locator():
    calls = []

    class Evidence:
        async def extract(self, url, *, result_id, **kwargs):
            calls.append(url)
            return EvidenceRecord(
                evidence_id="ev_" + "f" * 32,
                result_id=result_id,
                origin_url=url,
                canonical_url=url,
                retrieved_at=datetime.now(UTC),
                content_type="text/plain",
                evidence_text="fact",
                locator=EvidenceLocator(
                    kind="plain_text", text_offset_start=10, text_offset_end=14
                ),
                content_sha256="a" * 64,
                extraction_method="plain_text",
            )

    search = service(FakeProvider("ddgs"), FakeProvider("searxng", url="https://example.org/other"))
    search.evidence = Evidence()
    result = await search.search(
        SearchRequest(query="fact", mode="auto", require_evidence=True, max_pages=1),
        "tenant",
        "test",
    )
    assert result.mode_executed == Mode.EVIDENCE and result.partial
    assert (
        result.budget.max_pages == result.budget.pages_attempted == result.budget.pages_fetched == 1
    )
    assert len(calls) == len(result.evidence_records) == 1
    assert result.results[0].evidence_refs == [result.evidence_records[0].evidence_id]
    assert result.results[0].evidence_status == "content_extracted"
    assert result.results[1].evidence_status == "unverified"
    assert "EVIDENCE_PAGE_BUDGET_REACHED" in result.warnings


async def test_evidence_route_marks_only_the_pdf_candidate_as_ocr_pending():
    from multisearch.pdf_parser import OCRPending
    from multisearch.safe_fetch import FetchError

    class Evidence:
        async def extract(self, url, **_kwargs):
            if url.endswith("scanned.pdf"):
                raise OCRPending()
            raise FetchError(422, "FETCH_NO_EXCERPT", "没有可提取正文")

    search = service(
        FakeProvider("ddgs", url="https://example.org/scanned.pdf"),
        FakeProvider("searxng", url="https://example.org/blank.pdf"),
    )
    search.evidence = Evidence()
    result = await search.search(
        SearchRequest(query="fact", mode="evidence", max_pages=2), "tenant", "test"
    )
    assert result.partial and not result.evidence_records
    assert {item.url: item.evidence_status for item in result.results} == {
        "https://example.org/scanned.pdf": "ocr_pending",
        "https://example.org/blank.pdf": "unverified",
    }
    assert all(not item.evidence_refs for item in result.results)
    assert {"OCR_PENDING", "EVIDENCE_EXTRACTION_INCOMPLETE", "EVIDENCE_UNAVAILABLE"}.issubset(
        result.warnings
    )
    assert result.budget.pages_attempted == 2 and result.budget.pages_fetched == 0


async def test_published_routing_and_tenant_cache_epoch_change_execution():
    ddgs = FakeProvider("ddgs", handler=failed)
    searxng = FakeProvider("searxng")
    search = service(ddgs, searxng)
    search.routing = DEFAULT_ROUTING.model_copy(
        update={
            "version": 1,
            "policy": RoutingRules(quick_fallback=False),
        }
    )
    with pytest.raises(ApiError) as failure:
        await search.search(SearchRequest(query="sample", mode="quick"), "tenant", "failed")
    assert failure.value.code == "PROVIDER_UNAVAILABLE" and not searxng.calls
    search.routing = DEFAULT_ROUTING
    versions = {"tenant-a": 0, "tenant-b": 0}

    async def epochs(tenant):
        return {"web": versions[tenant]}

    search.load_cache_epochs = epochs
    ddgs.handler = None
    request = SearchRequest(query="sample")
    await search.search(request, "tenant-a", "first")
    await search.search(request, "tenant-b", "second")
    versions["tenant-a"] = 1
    assert not (await search.search(request, "tenant-a", "invalidated")).cache_hit
    assert (await search.search(request, "tenant-b", "preserved")).cache_hit


async def test_offline_local_index_never_sends_query_to_public_providers():
    external = FakeProvider("ddgs")
    search = service(external)

    class Local:
        async def search(self, tenant, request, limit):
            assert tenant == "tenant" and limit == 10
            return [
                TextSearchResult(
                    result_id=f"private-{number}",
                    title=request.query,
                    url=None,
                    canonical_url=None,
                    source_type=Source.LOCAL,
                    provider_ids=["local_index"],
                    retrieved_at=datetime.now(UTC),
                )
                for number in range(2)
            ]

    search.local_index = Local()
    body = SearchRequest(
        query="private query", source_types=["local"], network_scope="offline", max_results=10
    )
    response = await search.search(body, "tenant", "private")
    assert response.mode_executed == Mode.LOCAL and response.result_count == 2
    with pytest.raises(ApiError) as blocked:
        await search.search(
            body.model_copy(update={"source_types": [Source.LOCAL, Source.WEB]}), "tenant", "mixed"
        )
    assert blocked.value.code == "SOURCE_UNAVAILABLE" and external.calls == []
    with pytest.raises(ApiError) as mixed:
        await search.search(
            SearchRequest(query="private query", source_types=["local", "web"]), "tenant", "public"
        )
    assert mixed.value.code == "SOURCE_UNAVAILABLE" and external.calls == []


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
        raise ProviderFailure(
            "rate_limited", retry_after=30, reason_codes=(ProviderReason.UPSTREAM_RATE_LIMITED,)
        )

    provider = FakeProvider("ddgs", handler=rate_limited)
    search = service(provider)
    for query, expected_attempts in (("one", 1), ("two", 0)):
        with pytest.raises(ApiError) as failure:
            await search.search(SearchRequest(query=query), "tenant", query)
        assert failure.value.code == "PROVIDER_UNAVAILABLE"
        assert 25_000 < failure.value.retry_after_ms <= 30_000
        (run,) = failure.value.provider_runs
        assert run.attempts == expected_attempts and run.reason_codes == ["upstream_rate_limited"]
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


async def test_circuit_keeps_trigger_reason_despite_late_success():
    provider = FakeProvider("ddgs")
    search = service(provider)
    started, resume = asyncio.Event(), asyncio.Event()

    async def slow():
        started.set()
        await resume.wait()

    async def blocked():
        raise ProviderFailure("failed", reason_codes=(ProviderReason.UPSTREAM_CAPTCHA,))

    provider.handler = slow
    task = asyncio.create_task(search.search(SearchRequest(query="slow"), "tenant", "late"))
    try:
        await asyncio.wait_for(started.wait(), 1)
        provider.handler = blocked
        for query in ("one", "two", "three"):
            with pytest.raises(ApiError):
                await search.search(SearchRequest(query=query), "tenant", query)
        resume.set()
        assert (await task).result_count == 1
        with pytest.raises(ApiError) as failure:
            await search.search(SearchRequest(query="skip"), "tenant", "skip")
        (run,) = failure.value.provider_runs
        assert (run.status, run.attempts, run.reason_codes) == (
            "circuit_open",
            0,
            ["upstream_captcha"],
        )
        assert 0 < failure.value.retry_after_ms <= 15_000
        assert len(provider.calls) == 4
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_local_slot_wait_is_distinct_from_upstream_timeout():
    provider = FakeProvider("ddgs")
    search = service(provider)
    state = search.states["ddgs"]
    state.slots = asyncio.Semaphore(0)
    pair = (Source.WEB, Modality.TEXT)
    _, run = await search._run_provider(
        provider, SearchRequest(query="wait"), pair, 1, monotonic() + 0.05
    )
    assert (run.status, run.attempts, run.reason_codes) == (
        "timeout",
        0,
        ["local_provider_queue_timeout"],
    )
    assert state.circuits[pair].failures == 0 and not provider.calls


async def test_provider_retry_sleep_leaves_slot_available():
    first_attempt = asyncio.Event()

    async def throttle():
        first_attempt.set()
        raise ProviderFailure("rate_limited")

    provider = FakeProvider("ddgs", handler=throttle)
    search = service(provider)
    search.states["ddgs"].slots = asyncio.Semaphore(1)
    pair = (Source.WEB, Modality.TEXT)
    initial = asyncio.create_task(
        search._run_provider(provider, SearchRequest(query="retry"), pair, 1, monotonic() + 1)
    )
    try:
        await asyncio.wait_for(first_attempt.wait(), 1)
        provider.handler = None
        _, prompt = await search._run_provider(
            provider, SearchRequest(query="prompt"), pair, 1, monotonic() + 0.08
        )
        _, retried = await initial
        assert (prompt.status, prompt.attempts) == ("success", 1)
        assert (retried.status, retried.attempts) == ("success", 2)
    finally:
        initial.cancel()
        await asyncio.gather(initial, return_exceptions=True)
