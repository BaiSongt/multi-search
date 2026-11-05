import json
import subprocess
import sys
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ops" / "benchmark_search.py"
REVIEW_PAGE = SCRIPT.with_name("benchmark_review.py")


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


def test_review_workspace_and_detailed_labels_keep_pending_and_empty_distinct(tmp_path):
    original, review, output, page = (
        tmp_path / name
        for name in (
            "original.json",
            "labels.json",
            "reviewed.json",
            "workspace.html",
        )
    )
    title = "</script><img src=https://not-a-subresource.example/image.png>"
    base = {
        "case_id": "q001",
        "mode": "quick",
        "modality": "text",
        "query": "sample query",
        "category": "general",
        "http_status": 200,
        "code": None,
        "partial": False,
        "wall_ms": 10,
        "provider_runs": 0,
        "provider_rate_limited_runs": 0,
    }
    rows = [
        {
            **base,
            "results": [{"kind": "text", "title": title}, {"kind": "text", "title": "pending"}],
        },
        {**base, "mode": "standard", "results": []},
        {
            **base,
            "case_id": "q002",
            "http_status": 502,
            "code": "PROVIDER_UNAVAILABLE",
            "results": [],
        },
    ]
    source = json.dumps({"samples": rows}).encode()
    original.write_bytes(source)
    digest = sha256(source).hexdigest()
    generated = subprocess.run(
        [sys.executable, str(REVIEW_PAGE), "--report", str(original), "--output", str(page)],
        capture_output=True,
        timeout=10,
    )
    assert generated.returncode == 0, generated.stderr
    markup = page.read_text(encoding="utf-8")
    embedded = markup.split('<script id="report-data" type="application/json">', 1)[1].split(
        "</script>", 1
    )[0]
    document = json.loads(embedded)
    assert document["report_sha256"] == digest
    assert document["samples"][0]["results"][0]["title"] == title
    assert title not in markup
    assert "default-src 'none'" in markup
    assert 'element.rel = "noopener noreferrer"' in markup

    judgments = {
        "report_sha256": digest,
        "labels": {"q001:quick": [False, None]},
        "criteria": {"q001:quick": [[None, False, None], [True, None, True]]},
        "query_decisions": {"q001": True, "q002": None},
    }

    def score(destination):
        review.write_text(json.dumps(judgments), encoding="utf-8")
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--report",
                str(original),
                "--review",
                str(review),
                "--output",
                str(destination),
            ],
            capture_output=True,
            timeout=10,
        )

    assert score(output).returncode == 0
    reviewed = json.loads(output.read_bytes())
    assert reviewed["review_sha256"] == sha256(review.read_bytes()).hexdigest()
    assert reviewed["query_review"] == {
        "approved": 1,
        "rejected": 0,
        "pending": 1,
        "decisions": judgments["query_decisions"],
    }
    quick = reviewed["summary"]["quick:text"]
    assert quick["requests"] == 2 and quick["top10_returned"] == 2 and quick["top10_reviewed"] == 1
    assert quick["review_coverage"] == 0.5 and quick["top10_valid_ratio"] is None
    empty = reviewed["summary"]["standard:text"]
    assert empty["top10_returned"] == 0
    assert empty["review_coverage"] is None and empty["top10_valid_ratio"] is None

    judgments["labels"]["q001:quick"][1] = True
    assert score(tmp_path / "inconsistent.json").returncode == 2
    judgments["labels"]["q001:quick"][1] = None
    del judgments["query_decisions"]["q002"]
    assert score(tmp_path / "incomplete-query-decision-keys.json").returncode == 2


def test_live_report_captures_failed_provider_runs(tmp_path, monkeypatch):
    received = []
    run = {
        "provider_id": "searxng",
        "status": "rate_limited",
        "elapsed_ms": 4,
        "attempts": 1,
        "count": 0,
        "reason_codes": ["upstream_rate_limited", "upstream_captcha"],
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
    assert summary["provider_reason_counts"] == {
        "searxng": {
            "upstream_rate_limited": 1,
            "upstream_captcha": 1,
        }
    }
    assert summary["upstream_failures_without_runs"] == 0


def test_temporal_benchmark_checks_utc_boundaries_and_reviews_publication_dates(
    tmp_path, monkeypatch
):
    window = {"from": "2025-10-01", "to": "2025-10-31"}
    published = [
        "2025-10-01T00:00:00Z",
        "2025-10-31T23:59:59Z",
        "2025-10-31T23:30:00-01:00",
        None,
        17,
    ]
    returned = [
        {"kind": "text", "published_at": stamp, "retrieved_at": "2025-10-01T00:00:00+00:00"}
        for stamp in published
    ]
    cases = tmp_path / "cases.jsonl"
    base = {
        "category": "news",
        "modality": "text",
        "source_type": "news",
        "language": "en",
        "region": "GB",
        "time_range": window,
    }
    cases.write_text(
        "\n".join(
            json.dumps({**base, "id": name, "query": name}) for name in ("full", "empty", "fail")
        ),
        encoding="utf-8",
    )
    received = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            query = request["query"]
            received[query] = request
            self.send_response(502 if query == "fail" else 200)
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "code": "PROVIDER_UNAVAILABLE" if query == "fail" else None,
                        "results": returned if query == "full" else [],
                        "warnings": ["searxng:publication_date_missing:3"]
                        if query == "empty"
                        else [],
                    }
                ).encode()
            )

    def run(*args, tool=SCRIPT):
        return subprocess.run(
            [sys.executable, str(tool), *map(str, args)], capture_output=True, timeout=10
        )

    monkeypatch.setenv("MULTISEARCH_BENCHMARK_KEYS", "fixture")
    original = tmp_path / "temporal.json"
    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = run(
                "--api-url",
                f"http://127.0.0.1:{server.server_port}",
                "--cases",
                cases,
                "--output",
                original,
                "--mode",
                "quick",
                "--submit-interval",
                0,
            )
            assert result.returncode == 0, result.stderr
        finally:
            server.shutdown()
            thread.join()

    assert set(received) == {"full", "empty", "fail"}
    assert all(request["time_range"] == window for request in received.values())
    report = json.loads(original.read_bytes())
    assert all(row["time_range"] == window for row in report["samples"])
    summary = report["summary"]["quick:text"]["temporality"]
    assert summary == {
        "filtered_requests": 3,
        "successful_filtered_requests": 2,
        "successful_empty_filtered_requests": 1,
        "provider_missing_publication_date_candidates": 3,
        "returned": 5,
        "within_range": 2,
        "out_of_range": 1,
        "missing_published_at": 1,
        "invalid_published_at": 1,
        "published_at_equals_retrieved_at": 1,
        "observable_error_ratio": 0.6,
        "publication_reviewed": 0,
        "publication_incorrect": 0,
        "publication_review_coverage": 0,
        "temporal_error_ratio": None,
    }

    page = tmp_path / "temporal.html"
    assert run("--report", original, "--output", page, tool=REVIEW_PAGE).returncode == 0
    markup = page.read_text(encoding="utf-8")
    embedded = markup.split('<script id="report-data" type="application/json">', 1)[1].split(
        "</script>", 1
    )[0]
    document = json.loads(embedded)
    assert document["queries"]["full"]["time_range"] == window
    assert [result["publication_status"] for result in document["samples"][0]["results"]] == [
        "within_range",
        "within_range",
        "out_of_range",
        "missing_published_at",
        "invalid_published_at",
    ]

    labels = tmp_path / "labels.json"
    assert run("--report", original, "--review-template", "--output", labels).returncode == 0
    review = json.loads(labels.read_bytes())
    assert review["publication_decisions"] == {"full:quick": [None] * 5}
    review["publication_decisions"]["full:quick"] = [True, False, None, None, None]
    labels.write_text(json.dumps(review), encoding="utf-8")
    reviewed = tmp_path / "scored.json"
    assert run("--report", original, "--review", labels, "--output", reviewed).returncode == 0
    scores = json.loads(reviewed.read_bytes())["summary"]["quick:text"]
    assert scores["temporality"]["publication_review_coverage"] == 1
    assert scores["temporality"]["publication_incorrect"] == 1
    assert scores["temporality"]["temporal_error_ratio"] == 0.8
    assert scores["top10_valid_ratio"] is None

    review["publication_decisions"]["full:quick"][2] = True
    labels.write_text(json.dumps(review), encoding="utf-8")
    invalid = tmp_path / "invalid.json"
    assert run("--report", original, "--review", labels, "--output", invalid).returncode == 2
    assert not invalid.exists()
