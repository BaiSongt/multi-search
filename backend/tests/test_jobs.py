import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from multisearch.auth import require_machine
from multisearch.dynamic_renderer import HEARTBEAT_KEY
from multisearch.errors import install_errors
from multisearch.evidence_contracts import EvidenceLocator, EvidenceRecord
from multisearch.identity import MachineIdentity
from multisearch.job_api import router
from multisearch.job_application import JobApplication
from multisearch.job_contracts import FetchRequest, JobKind, JobStatus
from multisearch.job_store import Claim
from multisearch.pdf_parser import OCRPending
from multisearch.safe_fetch import FetchError
from multisearch.worker import Worker


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"url": "https://example.org", "urls": ["https://example.org"]},
        {"urls": ["https://example.org"] * 2},
        {"max_pages": 21},
    ],
)
def test_fetch_contract_rejects_ambiguous_or_unbounded_input(data):
    with pytest.raises(ValueError):
        FetchRequest.model_validate(data)


def claim(**payload):
    return Claim(
        uuid4(), uuid4(), uuid4(), JobKind.FETCH, payload, datetime.now(UTC) + timedelta(seconds=30)
    )


async def test_batch_fetch_preserves_verified_excerpt_with_bounded_partial_results():
    class Store:
        def __init__(self):
            self.completed = []

        async def progress(self, job, progress, *, checkpoint):
            self.completed.append(progress.completed)
            return True

    class Evidence:
        def __init__(self):
            self.calls = []

        async def extract(self, url, **kwargs):
            self.calls.append(url)
            if url.endswith("invalid"):
                raise FetchError(422, "SSRF_BLOCKED", "无法抓取来源")
            return EvidenceRecord(
                evidence_id="ev_" + "1" * 32,
                origin_url=url,
                canonical_url=url,
                retrieved_at=datetime.now(UTC),
                content_type="text/plain",
                evidence_text="fact",
                locator=EvidenceLocator(kind="plain_text", text_offset_start=0, text_offset_end=4),
                content_sha256="0" * 64,
                extraction_method="plain_text",
            )

    evidence = Evidence()
    store = Store()
    worker = Worker(store, evidence)
    targets = [
        "https://example.org/good",
        "https://example.org/invalid",
        "https://example.org/skip",
    ]
    outcome = await worker.fetch(claim(urls=targets, max_pages=2))
    assert outcome.status == JobStatus.PARTIAL and outcome.pages_fetched == 1
    assert outcome.result["evidence_records"][0]["evidence_text"] == "fact"
    assert outcome.result["failures"][0]["index"] == 1
    assert outcome.result["warnings"] == ["MAX_PAGES_REACHED"]
    assert evidence.calls == targets[:2] and store.completed == [1, 1, 2, 2]


async def test_fetch_keeps_ocr_pending_after_checkpoint_and_does_not_refetch():
    class Store:
        def __init__(self):
            self.saved = None

        async def progress(self, _job, progress, *, checkpoint):
            self.saved = (progress.model_copy(), deepcopy(checkpoint))
            return True

    class Evidence:
        calls = 0

        async def extract(self, *_args, **_kwargs):
            self.calls += 1
            raise OCRPending()

    store, evidence = Store(), Evidence()
    worker = Worker(store, evidence)
    request = claim(url="https://example.org/scanned.pdf")
    initial = await worker.fetch(request)
    progress, checkpoint = store.saved
    recovered = await worker.fetch(replace(request, progress=progress, checkpoint=checkpoint))

    assert recovered == initial and evidence.calls == 1
    assert (
        initial.status == JobStatus.PARTIAL and initial.error is None and initial.pages_fetched == 0
    )
    assert initial.result == {
        "evidence_records": [],
        "warnings": ["OCR_PENDING"],
        "failures": [],
        "ocr_pending": [0],
    }


async def test_worker_cancellation_stops_inflight_handler_and_finishes_cancelled(monkeypatch):
    started = asyncio.Event()
    closed = asyncio.Event()

    class Store:
        async def finish(self, job, outcome):
            assert closed.is_set()
            assert outcome.status == JobStatus.CANCELLED

    async def fetch(job):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    async def cancelled(job):
        await started.wait()

    worker = Worker(Store(), None)
    worker.handlers[JobKind.FETCH] = fetch
    monkeypatch.setattr(worker, "_watch", cancelled)
    await worker.execute(claim(url="https://example.org"))


async def test_fetch_enforces_scope_network_and_worker_gate():
    app = FastAPI()
    install_errors(app)
    app.include_router(router)
    identity = MachineIdentity(uuid4(), uuid4(), uuid4(), frozenset({"search:text"}))

    async def auth():
        return identity

    class NoWorker:
        async def exists(self, key):
            return 0

    app.dependency_overrides[require_machine] = auth
    app.state.redis = NoWorker()
    app.state.job_application = JobApplication(None, app.state.redis, None, enable_research=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = {"url": "https://example.org"}
        assert (await client.post("/api/v1/fetch", json=body)).status_code == 403
        identity = MachineIdentity(uuid4(), uuid4(), uuid4(), frozenset({"fetch"}))
        private = await client.post("/api/v1/fetch", json={**body, "network_scope": "offline"})
        assert private.status_code == 422
        pending = await client.post("/api/v1/fetch", json=body)
        assert pending.status_code == 503 and pending.json()["code"] == "WORKER_UNAVAILABLE"


async def test_dynamic_fetch_requires_approved_hosts_and_its_own_worker_readiness():
    app = FastAPI()
    install_errors(app)
    app.include_router(router)
    identity = MachineIdentity(uuid4(), uuid4(), uuid4(), frozenset({"fetch"}))

    async def auth():
        return identity

    app.dependency_overrides[require_machine] = auth
    ready = {"multisearch:worker:fetch"}
    redis = SimpleNamespace(exists=AsyncMock(side_effect=lambda key: key in ready))
    store = SimpleNamespace(
        create=AsyncMock(
            return_value=SimpleNamespace(job_id=uuid4(), status=JobStatus.QUEUED, trace_id=None)
        )
    )
    settings = SimpleNamespace(dynamic_fetch_domains=frozenset())
    search = SimpleNamespace(settings=settings)
    app.state.job_application = JobApplication(store, redis, search, enable_research=False)
    static = {"url": "https://example.org"}
    dynamic = {**static, "extraction_mode": "dynamic"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        denied = await client.post("/api/v1/fetch", json=dynamic)
        assert denied.status_code == 422 and denied.json()["code"] == "MODE_UNAVAILABLE"
        settings.dynamic_fetch_domains = frozenset({"example.org"})
        unauthorized = await client.post(
            "/api/v1/fetch", json={**dynamic, "url": "https://sub.example.org/"}
        )
        assert (
            unauthorized.status_code == 403
            and unauthorized.json()["code"] == "FETCH_TARGET_BLOCKED"
        )
        no_renderer = await client.post("/api/v1/fetch", json=dynamic)
        assert no_renderer.status_code == 503 and no_renderer.json()["code"] == "WORKER_UNAVAILABLE"
        store.create.assert_not_called()

        ready.add(HEARTBEAT_KEY)
        assert (await client.post("/api/v1/fetch", json=dynamic)).status_code == 202
        assert store.create.call_args.args[2]["extraction_mode"] == "dynamic"
        for payload in (static, {**static, "extraction_mode": "static"}):
            assert (await client.post("/api/v1/fetch", json=payload)).status_code == 202
            assert "extraction_mode" not in store.create.call_args.args[2]
