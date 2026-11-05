"""离线核对学术基准的 DOI、arXiv 标识符和明确指定的版本。"""

import argparse
import json
import re
from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from urllib.parse import unquote, urlsplit

CASES = Path(__file__).with_name("academic_cases.jsonl")
MODES = ("quick", "standard")
KINDS = ("title", "doi", "arxiv_id")
CASE_FIELDS = (
    "category",
    "query",
    "query_kind",
    "language",
    "region",
    "source_type",
    "modality",
    "reference_url",
)
DOI = re.compile(r"10\.[0-9]{4,9}/[^\s\x00-\x1f\x7f]+")
ARXIV = re.compile(r"(\d{4}\.\d{4,5}|[a-z][a-z0-9.-]{0,40}/\d{7})(v[1-9]\d{0,5})?", re.ASCII)
VERSION = re.compile(r"v[1-9][0-9]{0,5}")


@dataclass(frozen=True)
class Identifier:
    kind: str
    value: str
    version: str | None = None


def reference_path(value: str) -> tuple[str, str] | None:
    try:
        url = urlsplit(value)
        if (
            url.scheme in {"http", "https"}
            and url.hostname
            and url.username is None
            and url.password is None
            and url.port is None
            and not url.query
            and not url.fragment
        ):
            return url.hostname, unquote(url.path, errors="strict")
    except ValueError:
        pass
    return None


def identifier(value: object, kind: str, version: str | None = None) -> Identifier | None:
    if not isinstance(value, str) or len(value) > 1024:
        return None
    value = value.strip().casefold()
    if value.startswith(("http://", "https://")):
        if not (location := reference_path(value)):
            return None
        host, path = location
        if kind == "doi" and host in {"doi.org", "dx.doi.org"}:
            value = path.removeprefix("/").casefold()
        elif (
            kind == "arxiv_id"
            and host in {"arxiv.org", "www.arxiv.org"}
            and path.startswith("/abs/")
        ):
            value = path.removeprefix("/abs/").casefold()
        else:
            return None
    if kind == "doi":
        value = value.removeprefix("doi:").strip()
        return Identifier(kind, value) if DOI.fullmatch(value) else None

    match = ARXIV.fullmatch(value.removeprefix("arxiv:").strip())
    if not match or (version is not None and not VERSION.fullmatch(version)):
        return None
    base, embedded_version = match.groups()
    if version and embedded_version and version != embedded_version:
        return None
    return Identifier(kind, base, version or embedded_version)


def load_cases(data: bytes) -> dict[str, tuple[dict, Identifier]]:
    cases = {}
    for number, line in enumerate(data.decode("utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        case = json.loads(line)
        if not isinstance(case, dict) or any(
            not isinstance(case.get(field), str) or not case[field].strip()
            for field in ("id", *CASE_FIELDS)
        ):
            raise ValueError(f"第 {number} 行样本字段无效")
        case_id = case["id"]
        if case_id in cases:
            raise ValueError(f"重复的样本编号 {case_id}")
        if case["source_type"] != "academic" or case["modality"] != "text":
            raise ValueError(f"样本 {case_id} 须为学术文字检索")
        kind = case["query_kind"]
        url = case["reference_url"]
        expected = (
            (identifier(url, "doi") or identifier(url, "arxiv_id")) if reference_path(url) else None
        )
        if expected is None or kind not in KINDS:
            raise ValueError(f"样本 {case_id} 的参考标识符或查询类型无效")
        if kind != "title" and identifier(case["query"], kind) != expected:
            raise ValueError(f"样本 {case_id} 的查询标识符与参考文献不一致")
        cases[case_id] = case, expected
    if not cases:
        raise ValueError("学术样本为空")
    return cases


def matches(expected: Identifier, result: dict) -> bool:
    version = result.get("version") if expected.kind == "arxiv_id" else None
    candidate = identifier(result.get(expected.kind), expected.kind, version)
    return (
        candidate is not None
        and candidate.value == expected.value
        and (expected.version is None or candidate.version == expected.version)
    )


def evaluate(report: bytes, corpus: bytes, modes: tuple[str, ...]) -> dict:
    cases = load_cases(corpus)
    document = json.loads(report)
    rows = document.get("samples") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        raise ValueError("报告缺少 samples 数组")
    entries = {}
    for row in rows:
        if not isinstance(row, dict) or not all(
            isinstance(row.get(field), str) for field in ("case_id", "mode")
        ):
            raise ValueError("报告样本缺少编号或模式")
        case_id, mode = row["case_id"], row["mode"]
        key = (mode, case_id)
        label = f"{mode}/{case_id}"
        if case_id not in cases or mode not in modes:
            raise ValueError(f"报告包含不在评估范围的样本 {label}")
        if key in entries:
            raise ValueError(f"报告含有重复样本 {label}")
        case, expected = cases[case_id]
        if mismatches := [field for field in CASE_FIELDS if row.get(field) != case[field]]:
            raise ValueError(f"报告样本 {label} 与语料不匹配（{', '.join(mismatches)}）")
        status, code, results = (
            row.get("http_status"),
            row.get("code"),
            row.get("results"),
        )
        if (
            type(status) is not int
            or not (status == 0 or 100 <= status <= 599)
            or "code" not in row
            or (code is not None and not isinstance(code, str))
        ):
            raise ValueError(f"报告样本 {label} 的 HTTP 状态无效")
        if (
            not isinstance(results, list)
            or len(results) > 10
            or any(
                not isinstance(result, dict)
                or result.get("kind") != "text"
                or any(
                    result.get(field) is not None and not isinstance(result[field], str)
                    for field in ("doi", "arxiv_id", "version")
                )
                for result in results
            )
        ):
            raise ValueError(f"报告样本 {label} 的候选数据无效")
        if (status != 200 or code is not None) and results:
            raise ValueError(f"报告样本 {label} 的失败响应包含候选")
        rank = next(
            (rank for rank, result in enumerate(results, 1) if matches(expected, result)),
            None,
        )
        entries[key] = {
            "case_id": case_id,
            "query_kind": case["query_kind"],
            "reference_url": case["reference_url"],
            "http_status": status,
            "code": code,
            "candidate_count": len(results),
            "rank": rank,
        }

    if missing := {(mode, case_id) for mode in modes for case_id in cases} - entries.keys():
        raise ValueError(
            "报告缺少样本 " + ", ".join(f"{mode}/{case_id}" for mode, case_id in sorted(missing))
        )
    return {
        "report_sha256": sha256(report).hexdigest(),
        "corpus_sha256": sha256(corpus).hexdigest(),
        "modes": {mode: summarize([entries[mode, case_id] for case_id in cases]) for mode in modes},
    }


def summarize(rows: list[dict]) -> dict:
    counts = Counter(row["query_kind"] for row in rows)
    hits = Counter(row["query_kind"] for row in rows if row["rank"] is not None)
    return {
        "cases": len(rows),
        "hits_at_1": sum(row["rank"] == 1 for row in rows),
        "hits_at_10": hits.total(),
        "by_query_kind": {
            kind: {"cases": counts[kind], "hits_at_10": hits[kind]} for kind in KINDS
        },
        "identifier_misses": [
            row["case_id"] for row in rows if row["query_kind"] != "title" and row["rank"] is None
        ],
        "samples": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="学术检索基准原始 JSON 报告")
    parser.add_argument("--cases", type=Path, default=CASES, help="用于生成报告的学术样本 JSONL")
    parser.add_argument(
        "--mode",
        choices=MODES,
        action="append",
        help="声明报告包含的模式，默认要求两种",
    )
    parser.add_argument(
        "--fail-on-identifier-miss",
        action="store_true",
        help="DOI 或 arXiv ID 查询漏检时退出 1",
    )
    options = parser.parse_args()
    modes = tuple(dict.fromkeys(options.mode or MODES))
    try:
        result = evaluate(options.report.read_bytes(), options.cases.read_bytes(), modes)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(
        options.fail_on_identifier_miss
        and any(report["identifier_misses"] for report in result["modes"].values())
    )


if __name__ == "__main__":
    raise SystemExit(main())
