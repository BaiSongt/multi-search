import json
import logging
from collections import Counter
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from multisearch.contracts import Modality, Mode
from multisearch.observability import FetchMetrics, Telemetry, install_observability


class FakeDatabase:
    def __init__(self, fail=False):
        self.connection = Mock()
        self.connection.execute = AsyncMock()
        self.connection.scalar = AsyncMock(
            side_effect=OSError("database password should not leak") if fail else None,
            return_value={
                "usage": [
                    {"mode": "quick", "status": "completed", "n": 3},
                    {"mode": "private-query-value", "status": "completed", "n": 1},
                    {"mode": "other-private-query", "status": "completed", "n": 2},
                ],
                "providers": [{"provider_id": "ddgs", "status": "timeout", "n": 2}],
                "images": [{"rights_status": "unknown", "n": 3, "missing": 1}],
                "fetches": [{"content_type": "text/html", "status": "completed", "n": 2}],
                "jobs": [{"kind": "fetch", "status": "queued", "n": 1, "oldest_seconds": 12}],
                "cache_hits": 1,
                "samples": {"usage": 6, "providers": 2, "images": 3, "jobs": 1},
            },
        )

    @asynccontextmanager
    async def begin(self):
        yield self.connection


class FakeRedis:
    def __init__(self):
        self.hashes: Counter = Counter()

    @asynccontextmanager
    async def pipeline(self, **_kwargs):
        pipe = Mock()

        async def execute():
            if pipe.hmget.called:
                key, fields = pipe.hmget.call_args.args
                return [{"used_memory": 1024}, 1, 0, [self.hashes.get((key, f)) for f in fields]]
            for call in pipe.hincrby.call_args_list:
                key, field, count = call.args
                self.hashes[key, field] += count

        pipe.execute = execute
        yield pipe


def make_app(*, token="", allow_loopback=False, fail=False):
    app = FastAPI()
    app.state.db = FakeDatabase(fail)
    app.state.redis = FakeRedis()
    app.state.search = Mock(
        provider_status=lambda: [
            {
                "provider_id": "ddgs",
                "status": "degraded",
                "pair_health": [{"status": "circuit_open"}],
            },
            {"provider_id": "private-query-label", "status": "healthy"},
        ]
    )
    install_observability(app, token=token, allow_loopback=allow_loopback)

    @app.get("/api/v1/jobs/{job_id}")
    async def get_job(job_id: str):
        return {"status": "ok"}

    return app


async def test_metrics_require_dedicated_token_and_use_bounded_gauges():
    token = "A" * 48
    app = make_app(token=token)
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("192.0.2.10", 1000)), base_url="http://localhost"
    ) as client:
        assert (await client.get("/metrics")).status_code == 401
        assert (
            await client.get("/metrics", headers={"Authorization": "Bearer msk_wrong"})
        ).status_code == 401
        assert app.state.db.connection.scalar.await_count == 0
        response = await client.get("/metrics", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Content-Type"] == "text/plain; version=0.0.4; charset=utf-8"
        assert response.headers["X-Trace-ID"]
        payload = response.text
        assert 'multisearch_metrics_dependency_up{dependency="postgres"} 1' in payload
        assert (
            'multisearch_stored_search_requests_window{mode="unknown",status="completed"} 3'
            in payload
        )
        assert 'multisearch_jobs_active{mode="fetch",status="queued"} 1' in payload
        assert 'multisearch_circuit_open{provider="ddgs"} 1' in payload
        assert 'multisearch_worker_ready{mode="fetch"} 1' in payload
        assert "private-query" not in payload and token not in payload
        again = await client.get("/metrics", headers={"Authorization": f"Bearer {token}"})
        assert again.status_code == 200
        assert app.state.db.connection.scalar.await_count == 1


async def test_metrics_loopback_rejects_forwarded_peer_and_reports_partial_failure():
    app = make_app(allow_loopback=True, fail=True)
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 9999)), base_url="http://localhost"
    ) as client:
        assert (
            await client.get("/metrics", headers={"X-Forwarded-For": "127.0.0.1"})
        ).status_code == 404
        response = await client.get("/metrics")
    assert response.status_code == 200
    assert 'multisearch_metrics_dependency_up{dependency="postgres"} 0' in response.text
    assert 'multisearch_metrics_dependency_up{dependency="redis"} 1' in response.text
    assert "database password" not in response.text


async def test_fetch_metrics_publish_to_shared_counter_and_keep_labels_bounded():
    app = make_app(allow_loopback=True)
    async with (
        FetchMetrics(app.state.redis).publishing() as api,
        FetchMetrics(app.state.redis).publishing() as worker,
    ):
        api.record("text/html", "completed")
        worker.record("text/html", "completed")
        worker.record("application/pdf", "ocr_pending")
        worker.record("private-server-header", "failed")
        worker.record(None, "blocked")
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 9999)), base_url="http://localhost"
    ) as client:
        response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.text.count("# TYPE multisearch_fetch_total counter") == 1
    for content_type, status, count in [
        ("text/html", "completed", 2),
        ("application/pdf", "ocr_pending", 1),
        ("unknown", "failed", 1),
        ("unknown", "blocked", 1),
    ]:
        assert (
            f'multisearch_fetch_total{{content_type="{content_type}",status="{status}"}} {count}'
            in response.text
        )
    assert "private-server-header" not in response.text


async def test_transport_trace_does_not_log_path_query_or_supplied_ids(caplog):
    app = make_app()
    logger = "uvicorn.error.multisearch.telemetry"
    caplog.set_level(logging.INFO, logger=logger)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.get(
            "/api/v1/jobs/secret-tenant-name?query=private-value",
            headers={"X-Trace-ID": "caller-secret", "Authorization": "Bearer secret-key"},
        )
    assert response.status_code == 200
    telemetry = [record.message for record in caplog.records if record.name == logger]
    assert len(telemetry) == 1
    logged = json.loads(telemetry[0])
    assert logged["route"] == "job" and logged["trace_id"] == response.headers["X-Trace-ID"]
    assert "secret" not in telemetry[0] and "private-value" not in telemetry[0]


def test_search_histogram_and_auth_reject_high_cardinality_labels():
    recorder = Telemetry()
    recorder.record_search_failure(Mode.QUICK, "failed", "RATE_LIMITED", [Modality.TEXT], 100)
    recorder.record_search_failure(Mode.QUICK, "failed", "USAGE_UNAVAILABLE", [Modality.TEXT], 250)
    recorder.record_auth("success", ["secret-scope"])
    payload = "\n".join(recorder.process_lines())
    assert (
        'multisearch_request_duration_seconds_bucket{mode="quick",status="failed",le="0.2"} 1'
        in payload
    )
    assert (
        'multisearch_request_duration_seconds_bucket{mode="quick",status="failed",le="+Inf"} 2'
        in payload
    )
    assert 'multisearch_request_duration_seconds_count{mode="quick",status="failed"} 2' in payload
    assert 'multisearch_quota_denials_total{quota_type="rate_limit",modality="text"} 1' in payload
    assert 'multisearch_api_key_auth_total{status="success",scope="unknown"} 1' in payload
    assert "secret-scope" not in payload
