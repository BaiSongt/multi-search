import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ops" / "benchmark_search.py"


def test_offline_review_is_bound_to_original_report(tmp_path):
    original = tmp_path / "original.json"
    template = tmp_path / "labels.json"
    reviewed = tmp_path / "reviewed.json"
    results = [{"kind": "text", "result_id": "result-1"}, {"kind": "text", "result_id": "result-2"}]
    sample = {
        "case_id": "q001",
        "mode": "quick",
        "modality": "text",
        "http_status": 200,
        "code": None,
        "partial": False,
        "wall_ms": 10,
        "provider_runs": 1,
        "provider_rate_limited_runs": 0,
        "results": results,
    }
    original.write_text(json.dumps({"samples": [sample]}), encoding="utf-8")

    def run(*args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--report", str(original), *map(str, args)],
            capture_output=True,
            timeout=10,
        )

    assert run("--review-template", "--output", template).returncode == 0
    labels = json.loads(template.read_bytes())
    assert labels["labels"] == {"q001:quick": [None, None]}
    labels["labels"]["q001:quick"] = [True, False]
    template.write_text(json.dumps(labels), encoding="utf-8")
    assert run("--review", template, "--output", reviewed).returncode == 0
    report = json.loads(reviewed.read_bytes())
    assert report["summary"]["quick:text"]["top10_valid_ratio"] == 0.5
    assert report["samples"] == [sample]

    results.reverse()
    original.write_text(json.dumps({"samples": [sample]}), encoding="utf-8")
    rejected = tmp_path / "rejected.json"
    assert run("--review", template, "--output", rejected).returncode != 0
    assert not rejected.exists()


def test_live_report_captures_failed_provider_runs(tmp_path, monkeypatch):
    received = []
    run = {
        "provider_id": "searxng",
        "status": "rate_limited",
        "elapsed_ms": 4,
        "attempts": 1,
        "count": 0,
    }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(502)
            self.send_header("X-Trace-ID", "a" * 32)
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "code": "PROVIDER_UNAVAILABLE",
                        "provider_runs": [run],
                    }
                ).encode()
            )

    cases = tmp_path / "cases.jsonl"
    cases.write_text(
        json.dumps(
            {
                "id": "a001",
                "category": "academic",
                "query": "fixture",
                "modality": "text",
                "source_type": "academic",
                "region": "US",
                "language": "en",
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "report.json"
    monkeypatch.setenv("MULTISEARCH_BENCHMARK_KEYS", "fixture")
    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--api-url",
                    f"http://127.0.0.1:{server.server_port}",
                    "--cases",
                    str(cases),
                    "--output",
                    str(output),
                    "--mode",
                    "quick",
                ],
                capture_output=True,
                timeout=10,
            )
            assert result.returncode == 0, result.stderr
        finally:
            server.shutdown()
            thread.join()

    assert received[0]["source_types"] == ["academic"]
    assert "source_type" not in received[0]
    report = json.loads(output.read_bytes())
    assert report["samples"][0]["source_type"] == "academic"
    assert report["samples"][0]["trace_id"] == "a" * 32
    assert report["samples"][0]["provider_outcomes"] == [run]
    summary = report["summary"]["quick:text"]
    assert summary["provider_rate_limited_ratio"] == 1
    assert summary["upstream_failures_without_runs"] == 0
