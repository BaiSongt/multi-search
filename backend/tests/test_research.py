import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from multisearch.contracts import (
    ImageAttribution,
    ImageSearchResult,
    Modality,
    Mode,
    Scope,
    SearchBudget,
    SearchRequest,
    SearchResponse,
    Source,
    TextSearchResult,
)
from multisearch.evidence_contracts import EvidenceLocator, EvidenceRecord
from multisearch.job_contracts import JobKind, JobProgress, JobStatus
from multisearch.job_store import Claim
from multisearch.research import (
    ResearchOrchestrator,
    ResearchPricing,
    ResearchResult,
    decompose,
    research_budget,
)
from multisearch.research_model import (
    ModelGeneration,
    ModelPlan,
    ResearchModelError,
    create_research_model,
)
from multisearch.routing_policy import DEFAULT_ROUTING


def _request(**kwargs) -> SearchRequest:
    return SearchRequest(query="lunar observations", mode=Mode.DEEP, **kwargs)


def _claim(request, *, milliseconds=30_000, progress=None, checkpoint=None) -> Claim:
    return Claim(
        uuid4(),
        uuid4(),
        uuid4(),
        JobKind.RESEARCH,
        request.model_dump(mode="json"),
        datetime.now(UTC) + timedelta(milliseconds=milliseconds),
        progress,
        checkpoint,
    )


def _record(label, host, digest, phrase=None) -> tuple[TextSearchResult, EvidenceRecord]:
    url = f"https://{host}/paper"
    snippet = (
        phrase or f"Observational study from {host} discusses a source-specific lunar measurement."
    )
    document = EvidenceRecord(
        evidence_id=f"evidence-{label}",
        result_id=f"result-{label}",
        origin_url=url,
        canonical_url=url,
        retrieved_at=datetime.now(UTC),
        title=f"Report {label}",
        content_type="text/html",
        evidence_text=snippet,
        locator=EvidenceLocator(
            kind="html_text", text_offset_start=0, text_offset_end=len(snippet)
        ),
        content_sha256=digest * 64,
        extraction_method="static_html",
    )
    result = TextSearchResult(
        result_id=f"result-{label}",
        title=f"Report {label}",
        url=url,
        canonical_url=url,
        source_type=Source.WEB,
        provider_ids=["mock"],
        retrieved_at=document.retrieved_at,
        evidence_refs=[document.evidence_id],
        evidence_status="content_extracted",
    )
    return result, document


def _response(mode, results=(), records=(), *, attempted=0, metadata=0, cached=False):
    return SearchResponse(
        request_id="round",
        status="completed",
        mode_requested=mode,
        mode_executed=mode,
        partial=False,
        elapsed_ms=2,
        result_count=len(results),
        results=list(results),
        evidence_records=list(records),
        cache_hit=cached,
        budget=SearchBudget(
            max_latency_ms=12000,
            max_results=30,
            max_image_results=0,
            search_rounds=1,
            pages_fetched=len(records),
            pages_attempted=attempted,
            image_metadata_fetches=metadata,
        ),
    )


class Store:
    def __init__(self, live=True):
        self.live = live
        self.checkpoints = []

    async def progress(self, claim, progress, *, checkpoint=None):
        self.checkpoints.append((progress, checkpoint))
        return self.live


class Search:
    routing = DEFAULT_ROUTING

    def __init__(self, store, responses):
        self.store = store
        self.responses = iter(responses)
        self.calls = []

    async def search(self, request, tenant, request_id, *, metered, deadline, routing):
        checkpoint, snapshot = self.store.checkpoints[-1]
        assert checkpoint.stage == "searching" and checkpoint.research_budget["search_rounds"] > 0
        pending = snapshot["rounds"][-1]
        assert pending["status"] == "running" and checkpoint.completed == pending["number"] - 1
        assert pending["elapsed_ms"] is pending["result_count"] is pending["source_count"] is None
        assert metered is True and request.idempotency_key is None and request.max_llm_tokens == 0
        assert routing == DEFAULT_ROUTING and deadline >= asyncio.get_running_loop().time()
        self.calls.append((request, tenant, request_id))
        response = next(self.responses)
        if isinstance(response, float):
            await asyncio.sleep(response)
            pytest.fail("the orchestrator must cancel a round after the deadline")
        return response


def test_decomposition_is_deterministic_and_bounded():
    body = SearchRequest(query="研究月球资源；比较采样方式？研究月球资源", mode=Mode.DEEP)
    first = decompose(body, 5)
    assert first == decompose(body, 5)
    assert len(first) == 5
    assert first[1].query == "研究月球资源" and first[2].query == "比较采样方式"
    assert len({step.query for step in first}) == len(first)
    assert len(decompose(body.model_copy(update={"query": "中" * 1024}), 5)[-1].query) <= 1024
    assert research_budget(body).max_latency_ms == 900_000
    assert research_budget(body).max_llm_tokens == 0
    comparison = SearchRequest(query="比较神经网络与决策树的优缺点", mode=Mode.DEEP)
    assert [step.query for step in decompose(comparison, 3)[1:]] == ["神经网络", "决策树"]


async def test_iterative_rounds_refund_unspent_reservations_and_deduplicate_evidence():
    store = Store()
    original, source_a = _record("a", "first.example.org", "a")
    clone, syndicated = _record("duplicate", "mirror.example.org", "a")
    alternative, source_b = _record("b", "second.example.org", "b")
    search = Search(
        store,
        [
            _response(Mode.STANDARD, [original]),
            _response(Mode.EVIDENCE, [original], [source_a], attempted=1),
            _response(Mode.EVIDENCE, [clone, alternative], [syndicated, source_b], attempted=2),
        ],
    )
    request = _request(
        max_search_rounds=3,
        max_pages=3,
        max_results=3,
        time_range={"from": "2026-03-01", "to": "2026-03-31"},
    )
    claim = replace(_claim(request), request_id="client-research-id")
    assert claim.payload["time_range"] == {"start": "2026-03-01", "end": "2026-03-31"}
    outcome = await ResearchOrchestrator(search, store).run(claim)
    assert outcome.status == JobStatus.COMPLETED
    assert [call[0].mode for call in search.calls] == [Mode.STANDARD, Mode.EVIDENCE, Mode.EVIDENCE]
    assert [call[0].max_pages for call in search.calls] == [0, 3, 2]
    assert all(call[0].time_range == request.time_range for call in search.calls)
    assert all(call[1] == str(claim.tenant_id) for call in search.calls)
    assert [call[2] for call in search.calls] == [f"client-research-id.{n}" for n in range(1, 4)]
    result = ResearchResult.model_validate(outcome.result)
    assert result.usage.search_rounds == 3 and result.usage.pages_attempted == 3
    assert result.usage.pages_charged == 3 and outcome.pages_fetched == 3
    assert result.usage.llm_usage_known and result.usage.estimated_cost == 0
    assert [round.source_count for round in result.rounds] == [1, 1, 2]
    assert [round.evidence_coverage_ratio for round in result.rounds] == [0, 1, 1]
    assert result.evidence_coverage_ratio == pytest.approx(2 / 3)
    assert {record.evidence_id for record in result.evidence_records} == {
        "evidence-a",
        "evidence-b",
    }
    assert any(record.evidence_refs == ["evidence-a"] for record in result.findings)
    assert result.comparisons[0].assessment == "source_independence_pending"
    assert result.comparisons[0].source_hosts == ["first.example.org", "second.example.org"]
    mirror = next(item for item in result.results if item.result_id == clone.result_id)
    assert mirror.evidence_status == "unverified" and not mirror.evidence_refs
    assert all(
        "lunar observations" not in json.dumps(data) for _, data in store.checkpoints if data
    )


async def test_research_preserves_ocr_pending_through_fusion_and_recovery():
    source, _ = _record("scan", "scan.example.org", "a")
    source = source.model_copy(update={"evidence_refs": [], "evidence_status": "unverified"})
    pending = source.model_copy(update={"evidence_status": "ocr_pending"})
    pending_response = _response(Mode.EVIDENCE, [pending], attempted=1).model_copy(
        update={"status": "partial", "partial": True, "warnings": ["OCR_PENDING"]}
    )
    store = Store()
    search = Search(store, [_response(Mode.STANDARD, [source]), pending_response])
    claim = _claim(_request(max_search_rounds=2, max_pages=1))
    outcome = await ResearchOrchestrator(search, store).run(claim)
    progress, checkpoint = store.checkpoints[-1]
    assert checkpoint["results"][0]["evidence_status"] == "ocr_pending"

    resumed_store = Store()
    resumed = await ResearchOrchestrator(Search(resumed_store, []), resumed_store).run(
        replace(claim, progress=progress, checkpoint=checkpoint)
    )
    for completed in (outcome, resumed):
        result = ResearchResult.model_validate(completed.result)
        assert completed.status == JobStatus.PARTIAL and completed.pages_fetched == 0
        assert result.results[0].evidence_status == "ocr_pending"
        assert (
            not result.results[0].evidence_refs
            and not result.evidence_records
            and not result.findings
        )
        assert result.evidence_coverage_ratio == 0 and "OCR_PENDING" in result.warnings


async def test_local_research_coverage_is_not_applicable_and_public_image_sources_are_deduplicated():
    source, _ = _record("local", "example.org", "a")
    local = source.model_copy(
        update={"source_type": Source.LOCAL, "url": None, "canonical_url": None}
    )
    store = Store()
    result = await ResearchOrchestrator(
        Search(store, [_response(Mode.STANDARD, [local])]), store
    ).run(
        _claim(
            _request(
                source_types=[Source.LOCAL],
                network_scope=Scope.OFFLINE,
                max_pages=0,
                max_search_rounds=1,
            )
        )
    )
    assert result.status == JobStatus.COMPLETED
    assert result.result["rounds"][0]["source_count"] == 1
    assert result.result["rounds"][0]["evidence_coverage_ratio"] is None
    assert result.result["evidence_coverage_ratio"] is None

    page_url = "https://example.org/gallery"
    images = [
        ImageSearchResult(
            result_id=f"image-{number}",
            title=f"Image {number}",
            url=None,
            canonical_url=None,
            source_type=Source.WEB,
            provider_ids=["mock"],
            retrieved_at=source.retrieved_at,
            image=ImageAttribution(
                original_url=f"https://example.org/{number}.jpg",
                source_page_url=page_url,
                source_page_verified=bool(number),
            ),
        )
        for number in range(2)
    ]
    store = Store()
    result = await ResearchOrchestrator(
        Search(store, [_response(Mode.EVIDENCE, images)]), store
    ).run(_claim(_request(modalities=[Modality.IMAGE], max_search_rounds=1)))
    assert result.result["rounds"][0]["source_count"] == 1
    assert result.result["rounds"][0]["evidence_coverage_ratio"] == 0.5
    assert result.result["evidence_coverage_ratio"] == 0.5


async def test_recovered_job_retains_evidence_but_never_replays_reserved_round_or_model():
    first_store = Store()
    source, evidence = _record("first", "first.example.org", "a")
    body = _request(max_search_rounds=3, max_pages=3, max_llm_tokens=3500, require_evidence=True)
    first = _claim(body)
    first_search = Search(
        first_store,
        [
            _response(Mode.EVIDENCE, [source], [evidence], attempted=1),
            _response(Mode.EVIDENCE, [source], [evidence], attempted=1),
            _response(Mode.EVIDENCE, [source], [evidence], attempted=1),
        ],
    )
    await ResearchOrchestrator(first_search, first_store).run(first)
    ((_, saved),) = [
        entry
        for entry in first_store.checkpoints
        if entry[0].stage == "researching" and entry[0].completed == 1
    ]
    interrupted = JobProgress(
        stage="running",
        completed=1,
        total=3,
        research_budget={
            "search_rounds": 2,
            "pages_charged": 3,
            "image_metadata_charged": 0,
            "llm_tokens_reserved": 1800,
        },
    )
    recovered = Claim(
        first.job_id,
        first.tenant_id,
        uuid4(),
        JobKind.RESEARCH,
        first.payload,
        first.deadline_at,
        interrupted,
        saved,
    )
    store = Store()
    search = Search(store, [_response(Mode.STANDARD, [source])])

    class UnusedModel:
        async def prepare(self, *_):
            pytest.fail("model reservation cannot be reused after an interruption")

    pricing = ResearchPricing(
        currency="USD", input_per_million_tokens=2, output_per_million_tokens=10
    )
    outcome = await ResearchOrchestrator(search, store, UnusedModel(), pricing=pricing).run(
        recovered
    )
    result = ResearchResult.model_validate(outcome.result)
    assert outcome.status == JobStatus.PARTIAL
    assert len(search.calls) == 1
    assert search.calls[0][0].max_pages == 0 and search.calls[0][2].endswith(".3")
    assert [step.status for step in result.rounds] == ["completed", "interrupted", "completed"]
    assert result.rounds[1].elapsed_ms is result.rounds[1].source_count is None
    assert result.usage.pages_attempted is None and result.usage.pages_charged == 3
    assert result.usage.llm_tokens is None and result.usage.llm_tokens_reserved == 1800
    assert not result.usage.llm_usage_known and result.usage.estimated_cost is None
    assert result.usage.cost_currency == "USD"
    assert result.evidence_records[0].evidence_id == evidence.evidence_id
    assert "MODEL_INTERRUPTED" in result.warnings


async def test_reclaimed_inflight_round_has_unknown_metrics_without_replaying_it():
    store = Store()
    source, evidence = _record("inflight", "example.org", "a")
    body = _request(max_search_rounds=2, max_pages=4, require_evidence=True)
    original = _claim(body)
    successful = _response(Mode.EVIDENCE, [source], [evidence], attempted=1)
    await ResearchOrchestrator(Search(store, [successful, successful]), store).run(original)
    progress, snapshot = next(
        entry
        for entry in store.checkpoints
        if entry[0].stage == "searching" and entry[0].completed == 1
    )
    reclaimed = Claim(
        original.job_id,
        original.tenant_id,
        uuid4(),
        JobKind.RESEARCH,
        original.payload,
        original.deadline_at,
        progress,
        snapshot,
    )
    resume_store = Store()
    search = Search(resume_store, [])
    outcome = await ResearchOrchestrator(search, resume_store).run(reclaimed)
    assert outcome.status == JobStatus.PARTIAL and not search.calls
    result = ResearchResult.model_validate(outcome.result)
    first, interrupted = result.rounds
    assert first.status == "completed" and first.elapsed_ms is not None
    assert interrupted.status == "interrupted" and interrupted.number == 2
    assert interrupted.elapsed_ms is interrupted.result_count is interrupted.source_count is None
    assert interrupted.step == snapshot["rounds"][1]["step"]
    assert result.usage.search_rounds == 2
    assert "RESEARCH_ROUND_INTERRUPTED" in result.warnings


async def test_cache_hit_does_not_consume_page_attempts_or_report_new_fetches():
    store = Store()
    result, evidence = _record("cache", "cache.example.org", "b")
    search = Search(
        store,
        [
            _response(Mode.EVIDENCE, [result], [evidence], attempted=2, cached=True),
        ],
    )
    outcome = await ResearchOrchestrator(search, store).run(_claim(_request(max_search_rounds=1)))
    assert outcome.status == JobStatus.COMPLETED
    usage = outcome.result["usage"]
    assert usage["pages_charged"] == usage["pages_fetched"] == usage["pages_attempted"] == 0
    assert outcome.result["evidence_records"][0]["evidence_id"] == "evidence-cache"


async def test_lease_loss_does_not_dispatch_search():
    store = Store(live=False)
    search = Search(store, [])
    outcome = await ResearchOrchestrator(search, store).run(_claim(_request()))
    assert outcome.status == JobStatus.CANCELLED
    assert not search.calls


async def test_deadline_keeps_finished_evidence_and_charges_unobserved_attempts():
    store = Store()
    source, evidence = _record("a", "first.example.org", "a")
    search = Search(store, [_response(Mode.EVIDENCE, [source], [evidence], attempted=1), 2.0])
    outcome = await ResearchOrchestrator(search, store).run(
        _claim(
            _request(max_search_rounds=3, max_pages=4, require_evidence=True),
            milliseconds=300,
        )
    )
    assert outcome.status == JobStatus.PARTIAL
    assert outcome.result["evidence_records"][0]["evidence_id"] == "evidence-a"
    assert outcome.result["usage"]["pages_charged"] == 4
    assert outcome.result["usage"]["pages_attempted"] is None
    assert outcome.pages_fetched == 1
    assert "TIME_BUDGET_EXCEEDED" in outcome.result["warnings"]
    assert len(search.calls) == 2


async def test_model_usage_is_reported_and_invalid_references_do_not_become_findings():
    store = Store()
    source, evidence = _record("valid", "first.example.org", "a")
    search = Search(store, [_response(Mode.EVIDENCE, [source], [evidence], attempted=1)])

    class Model:
        async def prepare(self, prompt, available, deadline):
            assert "evidence-valid" in prompt and available == 3000
            _, before = store.checkpoints[-1]
            assert before["usage"]["llm_usage_known"] is True
            assert before["usage"]["llm_tokens_reserved"] == 0
            assert Decimal(before["usage"]["estimated_cost"]) == 0
            return ModelPlan(500, 200, {})

        async def generate(self, plan, deadline):
            progress, snapshot = store.checkpoints[-1]
            assert progress.research_budget["llm_tokens_reserved"] == plan.reservation == 700
            assert snapshot["usage"]["llm_tokens_reserved"] == plan.reservation
            assert snapshot["usage"]["llm_usage_known"] is False
            assert snapshot["usage"]["llm_tokens"] is None
            assert snapshot["usage"]["estimated_cost"] is None
            return ModelGeneration(
                json.dumps(
                    {
                        "findings": [
                            {
                                "claim": "Candidate interpretation.",
                                "evidence_refs": ["evidence-valid"],
                            },
                            {"claim": "Invented citation.", "evidence_refs": ["missing"]},
                        ]
                    }
                ),
                490,
                80,
            )

    pricing = ResearchPricing(
        currency="USD", input_per_million_tokens="2.5", output_per_million_tokens=10
    )
    outcome = await ResearchOrchestrator(search, store, Model(), pricing=pricing).run(
        _claim(
            _request(max_search_rounds=1, max_llm_tokens=3000),
        )
    )
    assert outcome.status == JobStatus.COMPLETED
    assert outcome.llm_tokens == outcome.result["usage"]["llm_tokens"] == 570
    assert outcome.result["usage"]["llm_tokens_reserved"] == 700
    assert outcome.result["usage"]["llm_usage_known"] is True
    assert Decimal(outcome.result["usage"]["estimated_cost"]) == Decimal("0.002025")
    assert outcome.result["usage"]["cost_currency"] == "USD"
    findings = outcome.result["model_findings"]
    assert len(findings) == 1 and findings[0]["verification_status"] == "model_synthesis_unverified"


async def test_recovered_model_checkpoint_keeps_unknown_usage_without_generating_again():
    store = Store()
    source, evidence = _record("valid", "first.example.org", "a")
    search = Search(store, [_response(Mode.EVIDENCE, [source], [evidence], attempted=1)])
    generating = asyncio.Event()

    class Model:
        async def prepare(self, *_):
            return ModelPlan(500, 200, {})

        async def generate(self, *_):
            generating.set()
            await asyncio.Event().wait()

    pricing = ResearchPricing(
        currency="USD", input_per_million_tokens="2.5", output_per_million_tokens=10
    )
    claim = _claim(_request(max_search_rounds=1, max_llm_tokens=3000))
    work = asyncio.create_task(
        ResearchOrchestrator(search, store, Model(), pricing=pricing).run(claim)
    )
    try:
        await asyncio.wait_for(generating.wait(), 5)
        progress, snapshot = store.checkpoints[-1]
    finally:
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)

    class UnusedModel:
        async def prepare(self, *_):
            pytest.fail("model reservation cannot be reused after an interruption")

    recovered = replace(claim, progress=progress, checkpoint=snapshot)
    resumed_store = Store()
    resumed_search = Search(resumed_store, [])
    outcome = await ResearchOrchestrator(
        resumed_search, resumed_store, UnusedModel(), pricing=pricing
    ).run(recovered)
    assert outcome.status == JobStatus.PARTIAL
    assert outcome.result["warnings"] == ["MODEL_INTERRUPTED"]
    assert not resumed_search.calls and not resumed_store.checkpoints
    assert outcome.result["evidence_records"] == snapshot["evidence_records"]
    assert outcome.result["usage"] == snapshot["usage"]
    assert outcome.result["usage"]["llm_tokens_reserved"] == 700
    assert outcome.result["usage"]["llm_usage_known"] is False
    assert outcome.result["usage"]["estimated_cost"] is None


@pytest.mark.parametrize("untrusted_limit", [True, False])
async def test_model_reservation_and_reported_usage_are_independently_checked(untrusted_limit):
    store = Store()
    source, evidence = _record("valid", "first.example.org", "a")
    search = Search(store, [_response(Mode.EVIDENCE, [source], [evidence], attempted=1)])

    class Model:
        async def prepare(self, prompt, available, deadline):
            return ModelPlan(available if untrusted_limit else 100, 50, {})

        async def generate(self, plan, deadline):
            assert not untrusted_limit, "an oversized plan must never be sent for generation"
            return ModelGeneration(
                '{"findings":[{"claim":"Untrusted quota.","evidence_refs":["evidence-valid"]}]}',
                100,
                51,
            )

    outcome = await ResearchOrchestrator(search, store, Model()).run(
        _claim(_request(max_search_rounds=1, max_llm_tokens=3000))
    )
    assert outcome.status == JobStatus.PARTIAL
    assert not outcome.result["model_findings"]
    if untrusted_limit:
        assert outcome.result["usage"]["llm_tokens_reserved"] == outcome.llm_tokens == 0
        assert "MODEL_TOKEN_BUDGET_INSUFFICIENT" in outcome.result["warnings"]
    else:
        assert outcome.result["usage"]["llm_tokens_reserved"] == 150 and outcome.llm_tokens == 151
        assert outcome.result["usage"]["llm_usage_known"] is True
        assert outcome.result["usage"]["estimated_cost"] is None
        assert "MODEL_BUDGET_VIOLATION" in outcome.result["warnings"]


@pytest.mark.parametrize(("currency", "rate"), [("usd", "1"), ("USD", "-1"), ("USD", "NaN")])
def test_invalid_research_pricing_is_rejected(currency, rate):
    with pytest.raises(ValidationError):
        ResearchPricing(
            currency=currency, input_per_million_tokens=rate, output_per_million_tokens=0
        )


async def test_openai_compatible_requires_exact_preflight_before_generation():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        payload = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer mocked-token"
        assert request.url.host == "model.example.org"
        if request.url.path.endswith("/input_tokens"):
            assert payload == {"model": "test-model", "input": "evidence"}
            return httpx.Response(200, json={"input_tokens": 75})
        assert payload["max_output_tokens"] == 125 and payload["store"] is False
        assert payload["stream"] is False and "tools" not in payload
        return httpx.Response(
            200,
            json={
                "usage": {"input_tokens": 75, "output_tokens": 70},
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "result"}]}
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        model = create_research_model(
            client,
            backend="openai",
            base_url="https://model.example.org/v1/",
            model="test-model",
            api_key="mocked-token",
        )
        deadline = asyncio.get_running_loop().time() + 1
        assert await model.prepare("evidence", 100, deadline) is None
        plan = await model.prepare("evidence", 200, deadline)
        result = await model.generate(plan, deadline)
    assert calls == ["/v1/responses/input_tokens", "/v1/responses/input_tokens", "/v1/responses"]
    assert result.text == "result" and result.input_tokens + result.output_tokens == 145


async def test_ollama_uses_raw_prompt_and_bounded_generation_without_redirects():
    def handler(request):
        assert str(request.url) == "http://ollama:11434/api/generate"
        payload = json.loads(request.content)
        assert payload["raw"] is True and payload["stream"] is False
        assert payload["options"]["num_predict"] == 500 - 64 - len("证据".encode())
        assert payload["options"]["num_ctx"] == 500
        return httpx.Response(302, headers={"location": "https://outside.example.org"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        model = create_research_model(
            client, backend="ollama", base_url="http://ollama:11434", model="local"
        )
        deadline = asyncio.get_running_loop().time() + 1
        plan = await model.prepare("证据", 500, deadline)
        assert plan.reservation == 500
        with pytest.raises(ResearchModelError, match="MODEL_UNAVAILABLE"):
            await model.generate(plan, deadline)
