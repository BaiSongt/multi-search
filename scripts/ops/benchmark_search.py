"""使用公开检索样本测量 L1/L2，并生成可供人工复核的报告。"""

import argparse
import json
import math
import os
import re
import subprocess
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from hashlib import sha256
from ipaddress import ip_address
from pathlib import Path
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[2]
CASES = Path(__file__).with_name("benchmark_cases.jsonl")
MAX_RESPONSE_BYTES = 2_000_000
_MISSING_DATE_WARNING = re.compile(r"[a-z0-9_]+:publication_date_missing:([1-9][0-9]{0,8})")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_url(raw: str) -> str:
    try:
        parsed = urlsplit(raw)
        if (
            parsed.scheme in {"http", "https"}
            and ip_address(parsed.hostname or "").is_loopback
            and not parsed.username
            and not parsed.password
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
        ):
            return raw.rstrip("/")
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("仅允许回环 API URL")


def open_response(request: Request, timeout: float):
    return build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=timeout)


def parse_time_range(value: object) -> tuple[date, date] | None:
    if value is None:
        return None
    if isinstance(value, dict) and value.keys() == {"from", "to"}:
        start, end = value["from"], value["to"]
        if all(
            isinstance(day, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", day)
            for day in (start, end)
        ):
            first, last = date.fromisoformat(start), date.fromisoformat(end)
            if first <= last:
                return first, last
    raise ValueError("time_range 须为按顺序排列的 ISO 日期 from 与 to")


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(UTC) if parsed.tzinfo is not None else None
    except (OverflowError, ValueError):
        return None


def publication_status(result: dict, window: tuple[date, date]) -> str:
    raw = result.get("published_at")
    if raw is None:
        return "missing_published_at"
    published = _timestamp(raw)
    if published is None:
        return "invalid_published_at"
    return "within_range" if window[0] <= published.date() <= window[1] else "out_of_range"


def run_case(base: str, key: str, mode: str, case: dict) -> dict:
    payload = {
        "query": case["query"],
        "mode": mode,
        "source_types": [case["source_type"]],
        "modalities": [case["modality"]],
        "language": case["language"],
        "region": case["region"],
        "cache_policy": "bypass",
        "max_results": 10,
    }
    for name in ("image_options", "time_range"):
        if case.get(name) is not None:
            payload[name] = case[name]
    request = Request(
        f"{base}/api/v1/search",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    started = monotonic()
    status = 0
    trace_id = None
    data = {}
    try:
        try:
            response = open_response(request, 20)
        except HTTPError as error:
            response = error
        with response:
            status = response.status
            trace = response.headers.get("X-Trace-ID", "")
            trace_id = trace if re.fullmatch(r"[0-9a-f]{32}", trace) else None
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError("响应超出报告工具上限")
            parsed = json.loads(body)
            if not isinstance(parsed, dict):
                raise ValueError("响应不符合 JSON 对象契约")
            data = parsed
    except (URLError, OSError, ValueError):
        data = {"code": "INVALID_RESPONSE" if status else "TRANSPORT_ERROR"}

    samples = []
    for item in data.get("results", [])[:10] if status == 200 else []:
        image = item.get("image") or {}
        provider_ids = item.get("provider_ids")
        providers = (
            sorted({value.strip() for value in provider_ids if isinstance(value, str)} - {""})
            if isinstance(provider_ids, list)
            else []
        )
        samples.append(
            {
                "result_id": item.get("result_id"),
                "provider_ids": providers,
                "kind": item.get("kind"),
                "title": item.get("title"),
                "url": item.get("url"),
                "canonical_url": item.get("canonical_url"),
                "doi": item.get("doi"),
                "arxiv_id": item.get("arxiv_id"),
                "version": item.get("version"),
                "published_at": item.get("published_at"),
                "retrieved_at": item.get("retrieved_at"),
                "original_url": image.get("original_url"),
                "source_page_url": image.get("source_page_url"),
                "rights_status": image.get("rights_status"),
                "rights_evidence_url": image.get("rights_evidence_url"),
            }
        )
    runs = data.get("provider_runs") or []
    return {
        "case_id": case["id"],
        "category": case["category"],
        "modality": case["modality"],
        "mode": mode,
        "query": case["query"],
        "language": case["language"],
        "region": case["region"],
        "source_type": case["source_type"],
        **{
            name: case[name]
            for name in ("query_kind", "reference_url", "time_range")
            if name in case
        },
        "provider_missing_publication_date_candidates": sum(
            int(match[1])
            for warning in data.get("warnings") or ()
            if isinstance(warning, str) and (match := _MISSING_DATE_WARNING.fullmatch(warning))
        ),
        "trace_id": trace_id,
        "http_status": status,
        "code": data.get("code"),
        "retry_after_ms": data.get("retry_after_ms"),
        "partial": bool(data.get("partial")),
        "wall_ms": round((monotonic() - started) * 1000, 3),
        "provider_runs": len(runs),
        "provider_outcomes": [
            {
                name: run[name]
                for name in (
                    "provider_id",
                    "status",
                    "elapsed_ms",
                    "attempts",
                    "count",
                    "reason_codes",
                )
                if name in run
            }
            for run in runs
        ],
        "provider_rate_limited_runs": sum(run["status"] == "rate_limited" for run in runs),
        "results": samples,
    }


def percentile(values: list[float], fraction: float) -> float | None:
    return sorted(values)[math.ceil(fraction * len(values)) - 1] if values else None


def temporal_summary(records: list[dict], publication_decisions: dict) -> dict | None:
    filtered = [row for row in records if row.get("time_range") is not None]
    if not filtered:
        return None
    observed = Counter()
    successful = empty = reviewed = incorrect = equal_to_retrieved = 0
    for row in filtered:
        window = parse_time_range(row["time_range"])
        if row["http_status"] != 200 or row["code"] is not None:
            continue
        successful += 1
        empty += not row["results"]
        judgments = publication_decisions.get(f"{row['case_id']}:{row['mode']}")
        for index, result in enumerate(row["results"]):
            status = publication_status(result, window)
            observed[status] += 1
            published = _timestamp(result.get("published_at"))
            equal_to_retrieved += published is not None and published == _timestamp(
                result.get("retrieved_at")
            )
            if status == "within_range" and judgments is not None:
                reviewed += judgments[index] is not None
                incorrect += judgments[index] is False
    returned = sum(observed.values())
    within = observed["within_range"]
    observable_errors = returned - within
    return {
        "filtered_requests": len(filtered),
        "successful_filtered_requests": successful,
        "successful_empty_filtered_requests": empty,
        "provider_missing_publication_date_candidates": sum(
            row.get("provider_missing_publication_date_candidates", 0) for row in filtered
        ),
        "returned": returned,
        "within_range": within,
        "out_of_range": observed["out_of_range"],
        "missing_published_at": observed["missing_published_at"],
        "invalid_published_at": observed["invalid_published_at"],
        "published_at_equals_retrieved_at": equal_to_retrieved,
        "observable_error_ratio": observable_errors / returned if returned else None,
        "publication_reviewed": reviewed,
        "publication_incorrect": incorrect,
        "publication_review_coverage": reviewed / within if within else None,
        "temporal_error_ratio": (
            (observable_errors + incorrect) / returned if returned and reviewed == within else None
        ),
    }


def summaries(rows: list[dict], review: dict, publication_decisions: dict | None = None) -> dict:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[f"{row['mode']}:{row['modality']}"].append(row)
    result = {}
    for group, records in sorted(grouped.items()):
        successes = [row for row in records if row["http_status"] == 200 and row["code"] is None]
        images = [item for row in successes for item in row["results"] if item["kind"] == "image"]
        n = sum(len(row["results"]) for row in successes)
        reviewed = valid = 0
        for row in successes:
            raw = review.get(f"{row['case_id']}:{row['mode']}")
            if raw is None:
                continue
            if (
                not isinstance(raw, list)
                or len(raw) != len(row["results"])
                or any(value is not None and type(value) is not bool for value in raw)
            ):
                raise ValueError(f"评审数据与结果条数不匹配：{row['case_id']} / {row['mode']}")
            reviewed += sum(value is not None for value in raw)
            valid += sum(value is True for value in raw)
        latencies = [row["wall_ms"] for row in successes]
        runs = sum(row["provider_runs"] for row in records)
        provider_statuses = defaultdict(Counter)
        provider_reason_counts = defaultdict(Counter)
        attempted_reasons = defaultdict(Counter)
        skipped_reasons = defaultdict(Counter)
        for row in records:
            for run in row.get("provider_outcomes", []):
                provider_statuses[run["provider_id"]][run["status"]] += 1
                if reasons := run.get("reason_codes"):
                    provider_reason_counts[run["provider_id"]].update(reasons)
                    attempts = run.get("attempts")
                    if type(attempts) is int and attempts == 0:
                        skipped_reasons[run["provider_id"]].update(reasons)
                    elif type(attempts) is int and attempts > 0:
                        attempted_reasons[run["provider_id"]].update(reasons)
        result[group] = {
            "requests": len(records),
            "http_statuses": dict(Counter(row["http_status"] for row in records)),
            "partial": sum(row["partial"] for row in successes),
            "p50_success_wall_ms": percentile(latencies, 0.5),
            "p95_success_wall_ms": percentile(latencies, 0.95),
            "http_429_ratio": sum(row["http_status"] == 429 for row in records) / len(records),
            "provider_rate_limited_ratio": (
                sum(row["provider_rate_limited_runs"] for row in records) / runs if runs else None
            ),
            "provider_statuses": dict(provider_statuses),
            "provider_reason_counts": dict(provider_reason_counts),
            "provider_attempted_reason_counts": dict(attempted_reasons),
            "provider_skipped_reason_counts": dict(skipped_reasons),
            "upstream_failures_without_runs": sum(
                row["code"] in {"PROVIDER_UNAVAILABLE", "TIME_BUDGET_EXCEEDED"}
                and not row["provider_runs"]
                for row in records
            ),
            "image_source_completeness": (
                sum(bool(item["original_url"] and item["source_page_url"]) for item in images)
                / len(images)
                if images
                else None
            ),
            "image_verified_license_ratio": (
                sum(item["rights_status"] == "verified_license" for item in images) / len(images)
                if images
                else None
            ),
            "top10_returned": n,
            "top10_reviewed": reviewed,
            "top10_valid": valid,
            "review_coverage": reviewed / n if n else None,
            "top10_valid_ratio": valid / n if n and reviewed == n else None,
        }
        if temporal := temporal_summary(records, publication_decisions or {}):
            result[group]["temporality"] = temporal
    return result


def validate_criteria(criteria: object, expected: dict, submitted: dict) -> None:
    if not isinstance(criteria, dict) or criteria.keys() != expected.keys():
        raise ValueError("逐项评审的样本与原始报告不匹配")
    derived = {}
    for key, entries in criteria.items():
        if not isinstance(entries, list) or len(entries) != len(expected[key]):
            raise ValueError(f"逐项评审的候选数量与原始报告不匹配：{key}")
        results = []
        for checks in entries:
            if (
                not isinstance(checks, list)
                or len(checks) != 3
                or any(check is not None and type(check) is not bool for check in checks)
            ):
                raise ValueError(f"逐项评审须包含相关性、可访问性和内容匹配三项：{key}")
            results.append(False if False in checks else True if None not in checks else None)
        derived[key] = results
    if derived != submitted:
        raise ValueError("候选判定与逐项评审不一致")


def review_report(path: Path, review: Path | None) -> dict:
    original = path.read_bytes()
    report = json.loads(original)
    if not isinstance(report, dict) or not isinstance(report.get("samples"), list):
        raise ValueError("报告缺少 samples 数组")
    digest = sha256(original).hexdigest()
    rows = report["samples"]
    labels = {
        f"{row['case_id']}:{row['mode']}": [None] * len(row["results"])
        for row in rows
        if row["http_status"] == 200 and row["code"] is None and row["results"]
    }
    publication_reviewable = {}
    for row in rows:
        key = f"{row['case_id']}:{row['mode']}"
        if key in labels and (window := parse_time_range(row.get("time_range"))) is not None:
            positions = [
                publication_status(item, window) == "within_range" for item in row["results"]
            ]
            if any(positions):
                publication_reviewable[key] = positions
    if review is None:
        return {
            "report_sha256": digest,
            "labels": labels,
            **(
                {
                    "publication_decisions": {
                        key: [None] * len(positions)
                        for key, positions in publication_reviewable.items()
                    }
                }
                if publication_reviewable
                else {}
            ),
        }
    review_bytes = review.read_bytes()
    judgments = json.loads(review_bytes.decode("utf-8-sig"))
    if not isinstance(judgments, dict) or judgments.get("report_sha256") != digest:
        raise ValueError("评审与当前基准报告的 SHA-256 不匹配")
    submitted = judgments.get("labels")
    if not isinstance(submitted, dict) or submitted.keys() - labels.keys():
        raise ValueError("评审包含不存在的样本键")
    if "criteria" in judgments:
        validate_criteria(judgments["criteria"], labels, submitted)
    publication_decisions = judgments.get("publication_decisions", {})
    if (
        not isinstance(publication_decisions, dict)
        or publication_decisions.keys() - publication_reviewable.keys()
    ):
        raise ValueError("发布日期评审包含不适用的样本键")
    for key, decisions in publication_decisions.items():
        if (
            not isinstance(decisions, list)
            or len(decisions) != len(publication_reviewable[key])
            or any(
                value is not None and (type(value) is not bool or not reviewable)
                for value, reviewable in zip(decisions, publication_reviewable[key], strict=True)
            )
        ):
            raise ValueError(f"发布日期评审与候选顺序或时效状态不匹配：{key}")
    report.pop("query_review", None)
    if "query_decisions" in judgments:
        decisions = judgments["query_decisions"]
        if (
            not isinstance(decisions, dict)
            or decisions.keys() != {row["case_id"] for row in rows}
            or any(value is not None and type(value) is not bool for value in decisions.values())
        ):
            raise ValueError("查询审定的样本或判定与原始报告不匹配")
        report["query_review"] = {
            "approved": sum(value is True for value in decisions.values()),
            "rejected": sum(value is False for value in decisions.values()),
            "pending": sum(value is None for value in decisions.values()),
            "decisions": decisions,
        }
    return {
        **report,
        "reviewed_report_sha256": digest,
        "review_sha256": sha256(review_bytes).hexdigest(),
        "summary": summaries(rows, submitted, publication_decisions),
    }


def write_output(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def container_stats(project: str | None) -> list[dict]:
    compose = ["docker", "compose"]
    if project:
        compose += ["--project-name", project]
    output = subprocess.check_output([*compose, "ps", "-q"], cwd=ROOT, timeout=10, text=True)
    containers = output.split()
    if not containers:
        raise ValueError("Compose 项目未运行容器")
    stats = subprocess.check_output(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}", *containers],
        cwd=ROOT,
        timeout=20,
        text=True,
    )
    return [
        {key: row.get(key) for key in ("Name", "CPUPerc", "MemUsage", "MemPerc", "PIDs")}
        for row in map(json.loads, stats.splitlines())
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", type=local_url, default="http://127.0.0.1:8765")
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--report", type=Path, help="离线处理已保存的原始报告")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--review-template", action="store_true", help="从原始报告生成评审模板")
    actions.add_argument("--review", type=Path, help="与原始报告绑定的人工评审 JSON")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("quick", "standard"), action="append")
    parser.add_argument("--case-id", action="append")
    parser.add_argument("--case-limit", type=int)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--submit-interval", type=float, default=1.1)
    parser.add_argument("--compose-stats", action="store_true")
    parser.add_argument("--project")
    options = parser.parse_args()
    if options.output.exists():
        parser.error("输出路径已存在")
    review_action = options.review_template or options.review is not None
    if bool(options.report) != review_action:
        parser.error("--report 需要和 --review-template 或 --review 一起使用")
    if options.report:
        try:
            result = review_report(options.report, options.review)
            write_output(options.output, result)
        except (OSError, ValueError, KeyError, TypeError) as error:
            parser.error(str(error))
        if not options.review_template:
            print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
        return
    if not 1 <= options.concurrency <= 20 or not 0 <= options.submit_interval <= 60:
        parser.error("concurrency 需要 1–20，submit-interval 需要 0–60 秒")
    if options.case_limit is not None and options.case_limit < 1:
        parser.error("case-limit 至少为 1")
    keys = [
        value.strip()
        for value in os.getenv("MULTISEARCH_BENCHMARK_KEYS", "").split(",")
        if value.strip()
    ]
    if not keys:
        parser.error("通过 MULTISEARCH_BENCHMARK_KEYS 提供一个或多个测试 API Key")
    cases = [
        json.loads(line)
        for line in options.cases.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    if options.case_id:
        selected = set(options.case_id)
        cases = [case for case in cases if case["id"] in selected]
        if {case["id"] for case in cases} != selected:
            parser.error("指定的 case ID 在样本中不存在")
    cases = cases[: options.case_limit]
    if not cases or len({case["id"] for case in cases}) != len(cases):
        parser.error("样本不能为空且 case ID 必须唯一")
    for case in cases:
        try:
            parse_time_range(case.get("time_range"))
        except ValueError as error:
            parser.error(f"{case['id']}：{error}")
    modes = list(dict.fromkeys(options.mode or ("quick", "standard")))
    started = datetime.now(UTC)
    before = container_stats(options.project) if options.compose_stats else None
    tasks = [(mode, case) for case in cases for mode in modes]
    with ThreadPoolExecutor(max_workers=options.concurrency) as pool:
        futures = []
        for index, (mode, case) in enumerate(tasks):
            if index:
                sleep(options.submit_interval)
            futures.append(
                pool.submit(run_case, options.api_url, keys[index % len(keys)], mode, case)
            )
        rows = [future.result() for future in futures]
    after = container_stats(options.project) if options.compose_stats else None
    report = {
        "started_at": started.isoformat(),
        "completed_at": datetime.now(UTC).isoformat(),
        "concurrency": options.concurrency,
        "submit_interval_s": options.submit_interval,
        "container_resources_before": before,
        "container_resources_after": after,
        "summary": summaries(rows, {}),
        "samples": rows,
    }
    write_output(options.output, report)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
