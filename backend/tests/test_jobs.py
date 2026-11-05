import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from multisearch.auth import require_machine
from multisearch.errors import install_errors
from multisearch.evidence_contracts import EvidenceLocator, EvidenceRecord
from multisearch.identity import MachineIdentity
from multisearch.job_api import router
from multisearch.job_application import JobApplication
from multisearch.job_contracts import FetchRequest, JobKind, JobStatus
from multisearch.job_store import Claim
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
