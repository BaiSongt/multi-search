import json
import sys
from contextlib import asynccontextmanager
from dataclasses import asdict
from hashlib import sha256
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "probe_public_fetch.py"
URL = "https://example.org/path?secret=target-query-value"
JOB_ID = "11111111-1111-4111-8111-111111111111"
TENANT_ID = "22222222-2222-4222-8222-222222222222"
KEY_ID = "33333333-3333-4333-8333-333333333333"
TRACE_ID = "a" * 32
EVIDENCE_ID = "ev_" + "b" * 32
STAMP = "2026-01-01T00:00:00Z"
EXCERPT = "sensitive fixture excerpt"


@pytest.fixture
def probe(monkeypatch):
    spec = spec_from_file_location("public_fetch_probe_test", SCRIPT)
    module = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("blocked", "usage_count", "api_version", "outcome"),
    [
        (False, 1, "0.3.0-alpha.16", None),
        (False, 2, "0.3.0-alpha.16", "PROBE_USAGE_MISMATCH"),
        (False, 1, "0.3.0-alpha.15", "PROBE_VERSION_MISMATCH"),
        (True, 0, "0.3.0-alpha.16", "FETCH_TARGET_BLOCKED"),
    ],
)
async def test_probe_reports_versions_trace_evidence_and_usage_without_content(
    probe, monkeypatch, tmp_path, blocked, usage_count, api_version, outcome
):
    version_file = tmp_path / "VERSION"
    version_file.write_text("0.3.0-alpha.16\n", encoding="utf-8")
    monkeypatch.setattr(probe, "WORKER_VERSION", version_file)
    monkeypatch.setattr(
        probe,
        "Settings",
        SimpleNamespace(
            from_env=lambda: SimpleNamespace(database_url="unused", key_pepper="pepper")
        ),
    )
    record = {
        "evidence_id": EVIDENCE_ID,
        "origin_url": URL,
        "canonical_url": URL,
        "retrieved_at": STAMP,
        "content_type": "application/pdf",
        "evidence_text": EXCERPT,
        "locator": {
            "kind": "pdf_text",
            "section": "sensitive section heading",
            "text_offset_start": 10,
            "text_offset_end": 10 + len(EXCERPT),
            "page_number": 1,
        },
        "content_sha256": "c" * 64,
        "extraction_method": "pypdf",
    }
    submitted = []
    calls = []

    def upstream(request):
        assert request.url.host == "multisearch-api"
        calls.append(request.url.path)
        if request.url.path == "/health/live":
            assert "Authorization" not in request.headers
            return httpx.Response(200, json={"version": api_version})
        assert request.headers["Authorization"] == f"Bearer msk_{UUID(KEY_ID).hex}.private-key"
        if request.url.path == "/api/v1/fetch":
            submitted.append(json.loads(request.content))
            return httpx.Response(
                202,
                headers={"X-Trace-ID": TRACE_ID},
                json={
                    "job_id": JOB_ID,
                    "trace_id": TRACE_ID,
                    "status": "queued",
                    "status_url": "https://elsewhere.example.org/?secret=untrusted-status-url",
                    "budget": {},
                },
            )
        if request.url.path == f"/api/v1/jobs/{JOB_ID}":
            error = {"code": "FETCH_TARGET_BLOCKED", "message": "raw diagnostic 198.18.0.1"}
            return httpx.Response(
                200,
                json={
                    "job_id": JOB_ID,
                    "trace_id": TRACE_ID,
                    "tenant_id": TENANT_ID,
                    "kind": "fetch",
                    "status": "failed" if blocked else "completed",
                    "created_at": STAMP,
                    "updated_at": STAMP,
                    "deadline_at": STAMP,
                    "attempts": 1,
                    "max_attempts": 2,
                    "progress": {
                        "stage": "completed",
                        "completed": 1,
                        "total": 1,
                        "pages_fetched": int(not blocked),
                    },
                    "result": {
                        "failures": [error],
                        "warnings": ["RAW_SECRET_DETAIL"],
                    }
                    if blocked
                    else {"evidence_records": [record]},
                    "error": error if blocked else None,
                },
            )
        assert request.url.path == f"/api/v1/evidence/{EVIDENCE_ID}" and not blocked
        return httpx.Response(200, json=record)

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(upstream), **kwargs),
    )

    class FakeDB:
        @asynccontextmanager
        async def connect(self):
            assert not blocked
            yield self

        async def execute(self, statement, values):
            assert "SELECT count(*) FROM usage_events" in str(statement)
            assert values == {"job_id": UUID(JOB_ID), "tenant_id": UUID(TENANT_ID)}
            row = SimpleNamespace(
                trace_id=TRACE_ID, pages_fetched=1, record=record, usage_count=usage_count
            )
            return SimpleNamespace(one_or_none=lambda: row)

        async def dispose(self):
            calls.append("disposed")

    class FakeIdentities:
        def __init__(self, _db, _pepper):
            pass

        async def issue(self, _tenant, _email, scopes, ttl):
            assert scopes == ["fetch"] and ttl == 1
            return f"msk_{UUID(KEY_ID).hex}.private-key"

        async def revoke(self, key_id):
            assert key_id == UUID(KEY_ID)
            calls.append("revoked")
            return True

    async def dns(host, port):
        assert blocked and (host, port) == ("example.org", 443)
        return ["93.184.216.34", "198.18.0.1"]

    monkeypatch.setattr(probe, "create_async_engine", lambda *_args, **_kwargs: FakeDB())
    monkeypatch.setattr(probe, "IdentityStore", FakeIdentities)
    monkeypatch.setattr(probe, "_resolve", dns)
    report = probe.ProbeReport("pypdf")
    await probe.main(URL, report, include_versions=True)

    assert len(submitted) == 2 and submitted[0] == submitted[1]
    assert calls[-2:] == ["revoked", "disposed"]
    assert report.api_version == api_version
    assert report.worker_version == "0.3.0-alpha.16"
    assert report.job_id == JOB_ID and report.trace_id == TRACE_ID
    assert report.job_attempts == 1
    assert report.target_url_sha256 == sha256(URL.encode()).hexdigest()
    assert report.result == ("failed" if outcome else "passed")
    if outcome:
        assert outcome in report.reason_codes
    else:
        assert report.reason_codes == []
    if blocked:
        assert report.dns_status == "non_public"
        assert report.usage_status == "not_checked"
        assert report.reason_codes == [
            "FETCH_TARGET_BLOCKED",
            "PROBE_UNKNOWN_JOB_REASON",
            "PROBE_JOB_NOT_COMPLETED",
        ]
    else:
        assert report.dns_status == "not_checked"
        assert report.usage_status == ("passed" if usage_count == 1 else "failed")
        assert report.evidence_id == EVIDENCE_ID
        assert report.extraction_method == "pypdf"
        assert report.locator == {
            key: value for key, value in record["locator"].items() if key != "section"
        }
        assert report.content_sha256 == record["content_sha256"]
    rendered = json.dumps(asdict(report))
    for private in (
        URL,
        "target-query-value",
        EXCERPT,
        "sensitive section heading",
        "RAW_SECRET_DETAIL",
        "198.18.0.1",
        "private-key",
    ):
        assert private not in rendered


async def test_probe_keeps_both_versions_on_configuration_failure(probe, monkeypatch, tmp_path):
    version_file = tmp_path / "VERSION"
    version_file.write_text("0.3.0-alpha.16\n", encoding="utf-8")
    monkeypatch.setattr(probe, "WORKER_VERSION", version_file)

    def invalid_settings():
        raise ValueError("private configuration detail")

    async def api_version(api):
        assert "Authorization" not in api.headers
        return "0.3.0-alpha.16"

    monkeypatch.setattr(probe.Settings, "from_env", invalid_settings)
    monkeypatch.setattr(probe, "api_version", api_version)
    report = probe.ProbeReport("pypdf")
    await probe.main(URL, report, include_versions=True)

    assert report.result == "failed"
    assert report.api_version == report.worker_version == "0.3.0-alpha.16"
    assert report.reason_codes == ["PROBE_CONFIGURATION_FAILED"]
    assert report.job_id is None and report.usage_status == "not_checked"


@pytest.mark.parametrize("json_mode", [False, True])
def test_probe_cli_keeps_human_output_and_json_exit_status(probe, monkeypatch, capsys, json_mode):
    async def run(url, report, *, include_versions):
        assert url == URL and include_versions == json_mode
        report.job_status = "failed"
        report.reason_codes.append("FETCH_TARGET_BLOCKED")
        report.dns_status = "non_public"

    monkeypatch.setattr(probe, "main", run)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), URL, *(["--json"] if json_mode else [])])
    if json_mode:
        assert probe.cli() == 1
        output = capsys.readouterr()
        assert json.loads(output.out)["dns_status"] == "non_public"
        assert not output.err
    else:
        with pytest.raises(SystemExit, match="公开正文验收未通过.*FETCH_TARGET_BLOCKED"):
            probe.cli()
        assert not capsys.readouterr().out


def test_probe_json_rejects_invalid_url_with_original_exit_code(probe, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "http://127.0.0.1/?secret=value", "--json"])
    assert probe.cli() == 2
    output = capsys.readouterr()
    assert json.loads(output.out)["reason_codes"] == ["FETCH_TARGET_BLOCKED"]
    assert "127.0.0.1" not in output.out and "secret" not in output.out and not output.err
