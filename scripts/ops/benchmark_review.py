"""从固定公网基准报告生成可离线使用的人工评审页面。"""

import argparse
import base64
import json
from hashlib import sha256
from pathlib import Path

from benchmark_search import parse_time_range, publication_status

TEMPLATE = Path(__file__).with_suffix(".html")
QUERY_FIELDS = (
    "query",
    "category",
    "modality",
    "language",
    "region",
    "source_type",
    "reference_url",
)
RESULT_FIELDS = (
    "title",
    "url",
    "published_at",
    "retrieved_at",
    "source_page_url",
    "original_url",
    "rights_status",
    "rights_evidence_url",
)


def review_data(raw_report: bytes) -> dict:
    report = json.loads(raw_report)
    rows = report.get("samples") if isinstance(report, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("原始报告须包含至少一条检索样本")
    samples = []
    keys = set()
    queries = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("检索样本须为对象")
        case_id, mode = row.get("case_id"), row.get("mode")
        if not isinstance(case_id, str) or not case_id.strip() or ":" in case_id:
            raise ValueError("检索样本编号无效")
        if mode not in ("quick", "standard"):
            raise ValueError(f"检索样本 {case_id} 的模式无效")
        key = f"{case_id}:{mode}"
        if key in keys:
            raise ValueError(f"重复检索样本 {key}")
        keys.add(key)
        query = {field: row.get(field) for field in QUERY_FIELDS}
        if (
            not isinstance(query["query"], str)
            or not query["query"].strip()
            or query["modality"] not in ("text", "image")
            or any(value is not None and not isinstance(value, str) for value in query.values())
        ):
            raise ValueError(f"检索样本 {key} 的查询字段无效")
        time_range = row.get("time_range")
        window = parse_time_range(time_range)
        if window is not None:
            query["time_range"] = time_range
        if queries.setdefault(case_id, query) != query:
            raise ValueError(f"样本 {case_id} 在两种模式中的查询条件不一致")
        status, code, results = row.get("http_status"), row.get("code"), row.get("results")
        if (
            type(status) is not int
            or not (status == 0 or 100 <= status <= 599)
            or "code" not in row
            or (code is not None and not isinstance(code, str))
            or not isinstance(results, list)
            or len(results) > 10
            or ((status != 200 or code is not None) and results)
        ):
            raise ValueError(f"检索样本 {key} 的返回数据无效")
        candidates = []
        for result in results:
            if not isinstance(result, dict) or result.get("kind") != query["modality"]:
                raise ValueError(f"检索样本 {key} 的候选模态不匹配")
            candidate = {field: result.get(field) for field in RESULT_FIELDS}
            for field in ("published_at", "retrieved_at"):
                if not isinstance(candidate[field], str):
                    candidate[field] = None
            if any(
                value is not None and not isinstance(value, str) for value in candidate.values()
            ):
                raise ValueError(f"检索样本 {key} 的候选字段无效")
            if window is not None:
                candidate["publication_status"] = publication_status(result, window)
            candidates.append(candidate)
        samples.append(
            {
                "key": key,
                "case_id": case_id,
                "mode": mode,
                "http_status": status,
                "code": code,
                "results": candidates,
            }
        )
    return {"report_sha256": sha256(raw_report).hexdigest(), "queries": queries, "samples": samples}


def render(raw_report: bytes) -> str:
    data = json.dumps(review_data(raw_report), ensure_ascii=True).replace("<", "\\u003c")
    template = TEMPLATE.read_text(encoding="utf-8")
    javascript = template.partition("<script>")[2].partition("</script>")[0]
    script_hash = base64.b64encode(sha256(javascript.encode("utf-8")).digest()).decode("ascii")
    return template.replace("{{SCRIPT_HASH}}", script_hash).replace("{{REPORT_DATA}}", data)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True, help="原始公网基准报告 JSON")
    parser.add_argument("--output", type=Path, required=True, help="新的离线评审 HTML 文件")
    options = parser.parse_args()
    try:
        html = render(options.report.read_bytes())
        options.output.parent.mkdir(parents=True, exist_ok=True)
        with options.output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(html)
    except (OSError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
