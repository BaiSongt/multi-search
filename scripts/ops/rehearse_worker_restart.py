"""在专用 Compose 项目中重启 Worker，验证一个实际运行任务可追踪。"""

import argparse
import json
import os
import re
import subprocess
from time import monotonic, sleep
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request
from uuid import UUID

from benchmark_search import ROOT, local_url, open_response

TERMINAL = {"completed", "partial", "failed", "cancelled"}


def api(base: str, key: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        base + path,
        data=data,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with open_response(request, 10) as response:
            document = json.load(response)
    except HTTPError as error:
        raise RuntimeError(f"任务接口返回 HTTP {error.code}") from None
    return document


def https_url(raw: str) -> str:
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or len(raw) > 2048
    ):
        raise argparse.ArgumentTypeError("使用你获准抓取的公开 HTTPS 网址")
    return raw


def compose_text(command: list[str], *args: str) -> str:
    return subprocess.check_output([*command, *args], cwd=ROOT, text=True, timeout=40).strip()


def worker_started(container: str) -> str:
    return compose_text(["docker", "inspect", "--format", "{{.State.StartedAt}}"], container)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project", required=True, help="必须使用 multisearch-drill- 前缀的专用项目"
    )
    parser.add_argument("--api-url", required=True, type=local_url)
    workload = parser.add_mutually_exclusive_group(required=True)
    workload.add_argument("--url", action="append", type=https_url)
    workload.add_argument("--research-query", help="用公开学术研究任务演练恢复")
    parser.add_argument("--job-timeout-ms", type=int, default=120_000)
    args = parser.parse_args()
    if not re.fullmatch(r"multisearch-drill-[a-z0-9][a-z0-9-]{0,30}", args.project):
        parser.error("只允许重启 multisearch-drill-* 专用项目")
    if args.url and (not 2 <= len(args.url) <= 20 or len(set(args.url)) != len(args.url)):
        parser.error("提供 2–20 个互不相同的公开测试网址")
    if not 30_000 <= args.job_timeout_ms <= 300_000:
        parser.error("job-timeout-ms 需要 30000–300000")
    key = os.getenv("MULTISEARCH_RECOVERY_KEY", "")
    if not key:
        parser.error("通过 MULTISEARCH_RECOVERY_KEY 提供当前任务的临时 Key")

    kind = "research" if args.research_query else "fetch"
    compose = ["docker", "compose", "--project-name", args.project, "--profile", kind]
    binding = compose_text(compose, "port", "multisearch-api", "8765").splitlines()
    target = urlsplit(args.api_url)
    if binding != [f"127.0.0.1:{target.port}"] or target.hostname != "127.0.0.1":
        parser.error("API URL 端口与所选 Compose 演练项目不匹配")
    worker = compose_text(compose, "ps", "-q", "multisearch-worker")
    if not re.fullmatch(r"[a-f0-9]{12,64}", worker):
        raise RuntimeError("演练项目需要正好一个运行中的 Worker")
    started_before = worker_started(worker)
    if api(args.api_url, key, "/api/v1/capabilities").get(kind) != "available":
        raise RuntimeError("所选任务的 Worker 尚未就绪")

    payload = (
        {
            "query": args.research_query,
            "source_types": ["academic"],
            "max_search_rounds": 5,
            "max_pages": 0,
            "cache_policy": "bypass",
            "max_latency_ms": args.job_timeout_ms,
        }
        if args.research_query
        else {"urls": args.url, "max_pages": len(args.url), "max_latency_ms": args.job_timeout_ms}
    )
    accepted = api(
        args.api_url,
        key,
        f"/api/v1/{kind}",
        payload,
    )
    job_id = str(UUID(accepted["job_id"]))
    path = f"/api/v1/jobs/{job_id}"
    deadline = monotonic() + args.job_timeout_ms / 1000 + 20
    while monotonic() < deadline:
        before = api(args.api_url, key, path)
        if before["status"] == "running":
            break
        if before["status"] in TERMINAL:
            raise RuntimeError("任务过快结束，未观察到正在运行的租约")
        sleep(0.15)
    else:
        raise RuntimeError("在截止时间内未观察到运行任务")
    subprocess.run(
        [*compose, "restart", "--timeout", "1", "multisearch-worker"],
        cwd=ROOT,
        timeout=40,
        check=True,
    )
    if worker_started(worker) == started_before:
        raise RuntimeError("未验证到 Worker 容器重启")

    while monotonic() < deadline:
        after = api(args.api_url, key, path)
        if after["job_id"] != job_id:
            raise RuntimeError("任务标识在重启后发生变化")
        if after["status"] in TERMINAL:
            break
        sleep(0.5)
    else:
        raise RuntimeError("重启后未观察到任务终态")
    code = (after.get("error") or {}).get("code")
    if after["attempts"] <= before["attempts"] and code not in {
        "WORKER_INTERRUPTED",
        "TIME_BUDGET_EXCEEDED",
    }:
        raise RuntimeError("任务没有重新领取或明确中断，无法证明此次重启覆盖了恢复逻辑")
    print(
        json.dumps(
            {
                "worker_restart_confirmed": True,
                "job_id": job_id,
                "attempts_before": before["attempts"],
                "attempts_after": after["attempts"],
                "status": after["status"],
                "code": code,
                "result_record_count": len((after.get("result") or {}).get("evidence_records", [])),
            }
        )
    )


if __name__ == "__main__":
    main()
