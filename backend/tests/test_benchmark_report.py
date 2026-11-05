import json
import subprocess
import sys
from pathlib import Path

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
