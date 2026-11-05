"""用启用抓取的 Compose 验证授权公开 URL 的实际正文证据。"""

import argparse
import asyncio
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from ipaddress import ip_address
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from multisearch.evidence_contracts import EvidenceRecord
from multisearch.identity import IdentityStore
from multisearch.job_contracts import JobAccepted, JobView
from multisearch.safe_fetch import FetchError, _public_ip, _resolve, _target
from multisearch.settings import Settings

BASE = "http://multisearch-api:8765"
WORKER_VERSION = Path("/app/VERSION")
TERMINAL = {"completed", "partial", "failed", "cancelled"}
LOCATORS = {
    "static_html": "html_text",
    "crawl4ai_static": "html_text",
    "pypdf": "pdf_text",
    "browser_dom": "rendered_html_text",
}
DNS_MESSAGES = {
    "ip_literal": "入口使用公开 IP 字面量，请检查重定向和域名策略",
    "lookup_failed": "Worker 入口域名 DNS 查询失败",
    "no_answers": "Worker 入口域名 DNS 没有返回地址",
    "non_public": "Worker 入口域名 DNS 返回至少一个非公网地址，地址策略拒绝连接",
    "public": "Worker 入口域名 DNS 返回公网地址，请检查出站连接、重定向和域名策略",
}
JOB_REASONS = {
    "FETCH_ATTEMPT_INTERRUPTED",
    "FETCH_BODY_TOO_LARGE",
    "FETCH_CONTENT_UNSUPPORTED",
    "FETCH_NOT_FOUND",
    "FETCH_NO_EXCERPT",
    "FETCH_PDF_ENCRYPTED",
    "FETCH_PDF_INVALID",
    "FETCH_PDF_LIMIT",
    "FETCH_PDF_UNAVAILABLE",
    "FETCH_RATE_LIMITED",
    "FETCH_REDIRECT_INVALID",
    "FETCH_REDIRECT_LIMIT",
    "FETCH_RENDER_POLICY_UNSUPPORTED",
    "FETCH_RENDER_UNAVAILABLE",
    "FETCH_RESOURCE_CORS_DENIED",
    "FETCH_RESOURCE_POLICY_UNSUPPORTED",
    "FETCH_RESOURCE_REDIRECT",
    "FETCH_ROBOTS_DENIED",
    "FETCH_ROBOTS_UNAVAILABLE",
    "FETCH_SITE_RESTRICTED",
    "FETCH_TARGET_BLOCKED",
    "FETCH_UNAVAILABLE",
    "JOB_CANCELLED",
    "JOB_FAILED",
    "JOB_PRINCIPAL_INACTIVE",
    "MAX_PAGES_REACHED",
    "OCR_PENDING",
    "TIME_BUDGET_EXCEEDED",
}


@dataclass
class ProbeReport:
    expected_method: str
    schema_version: int = 1
    checked_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    result: str = "failed"
    target_url_sha256: str | None = None
    api_version: str | None = None
    worker_version: str | None = None
    job_id: str | None = None
    trace_id: str | None = None
    job_status: str | None = None
    job_attempts: int | None = None
    submit_http_status: int | None = None
    evidence_id: str | None = None
    extraction_method: str | None = None
    locator: dict[str, str | int | None] | None = None
    content_sha256: str | None = None
    usage_status: str = "not_checked"
    reason_codes: list[str] = field(default_factory=list)
    dns_status: str = "not_checked"


class ProbeFailure(Exception):
    pass


def release_version(value: object) -> str | None:
    if isinstance(value, str) and len(value) <= 80:
        if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.+-]+)?", value):
            return value
    return None


async def diagnose_dns(url: str) -> str:
    target = _target(url)
    try:
        ip_address(target.host)
    except ValueError:
        pass
    else:
        return "ip_literal"
    try:
        async with asyncio.timeout(3):
            addresses = set(await _resolve(target.host, target.port))
    except (OSError, TimeoutError):
        return "lookup_failed"
    if not addresses:
        return "no_answers"
    for address in addresses:
        try:
            _public_ip(address)
        except FetchError:
            return "non_public"
    return "public"


async def api_version(api: httpx.AsyncClient) -> str | None:
    try:
        response = await api.get("/health/live")
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError, RecursionError):
        return None
    return release_version(payload.get("version")) if isinstance(payload, dict) else None


def job_reasons(job: JobView) -> list[str]:
    result = job.result or {}
    failures = result.get("failures", [])
    warnings = result.get("warnings", [])
    codes = (
        [item.get("code") for item in failures if isinstance(item, dict)]
        if isinstance(failures, list)
        else []
    )
    if isinstance(warnings, list):
        codes.extend(warnings)
    if job.error:
        codes.append(job.error.code)
    return list(
        dict.fromkeys(
            code if isinstance(code, str) and code in JOB_REASONS else "PROBE_UNKNOWN_JOB_REASON"
            for code in codes
        )
    )


async def fetch_evidence(
    api: httpx.AsyncClient, url: str, report: ProbeReport
) -> tuple[EvidenceRecord, JobView]:
    method = report.expected_method
    payload = {
        "url": url,
        "max_pages": 1,
        "max_latency_ms": 25_000,
        "idempotency_key": f"public-fetch-{uuid4().hex[:12]}",
    }
    if method == "browser_dom":
        payload["extraction_mode"] = "dynamic"
    response = await api.post("/api/v1/fetch", json=payload)
    report.submit_http_status = response.status_code
    if response.status_code != 202:
        raise ProbeFailure("PROBE_ENQUEUE_FAILED")
    accepted = JobAccepted.model_validate(response.json())
    report.job_id = str(accepted.job_id)
    trace_id = accepted.trace_id
    if not trace_id or not re.fullmatch(r"[0-9a-f]{32}", trace_id):
        raise ProbeFailure("PROBE_TRACE_MISMATCH")
    report.trace_id = trace_id
    if trace_id != response.headers.get("X-Trace-ID"):
        raise ProbeFailure("PROBE_TRACE_MISMATCH")
    replay = await api.post("/api/v1/fetch", json=payload)
    if (
        replay.status_code != 202
        or JobAccepted.model_validate(replay.json()).job_id != accepted.job_id
    ):
        raise ProbeFailure("PROBE_REPLAY_MISMATCH")
    async with asyncio.timeout(32):
        while True:
            polled = await api.get(f"/api/v1/jobs/{accepted.job_id}")
            polled.raise_for_status()
            job = JobView.model_validate(polled.json())
            report.job_status = job.status
            report.job_attempts = job.attempts
            if job.status in TERMINAL:
                break
            await asyncio.sleep(0.4)
    if job.job_id != accepted.job_id or job.trace_id != trace_id:
        raise ProbeFailure("PROBE_TRACE_MISMATCH")
    if job.status != "completed":
        report.reason_codes.extend(job_reasons(job))
        if {"FETCH_TARGET_BLOCKED", "FETCH_UNAVAILABLE"}.intersection(report.reason_codes):
            report.dns_status = await diagnose_dns(url)
        raise ProbeFailure("PROBE_JOB_NOT_COMPLETED")
    records = (job.result or {}).get("evidence_records")
    if job.progress.pages_fetched != 1 or not isinstance(records, list) or len(records) != 1:
        raise ProbeFailure("PROBE_EVIDENCE_INVALID")
    record = EvidenceRecord.model_validate(records[0])
    report.extraction_method = record.extraction_method
    locator = record.locator
    if (
        record.origin_url != url
        or not re.fullmatch(r"ev_[0-9a-f]{32}", record.evidence_id)
        or not re.fullmatch(r"[0-9a-f]{64}", record.content_sha256)
        or not record.evidence_text.strip()
        or locator.text_offset_end - locator.text_offset_start != len(record.evidence_text)
    ):
        raise ProbeFailure("PROBE_EVIDENCE_INVALID")
    report.evidence_id = record.evidence_id
    report.content_sha256 = record.content_sha256
    report.locator = locator.model_dump(exclude={"section"})
    if record.extraction_method != method or locator.kind != LOCATORS[method]:
        raise ProbeFailure("PROBE_EXTRACTION_MISMATCH")
    evidence = await api.get(f"/api/v1/evidence/{record.evidence_id}")
    evidence.raise_for_status()
    if EvidenceRecord.model_validate(evidence.json()) != record:
        raise ProbeFailure("PROBE_EVIDENCE_MISMATCH")
    return record, job


async def verify_usage(
    db: AsyncEngine, record: EvidenceRecord, job: JobView, report: ProbeReport
) -> None:
    report.usage_status = "failed"
    async with db.connect() as connection:
        row = (
            await connection.execute(
                text("""
                    SELECT e.record, u.pages_fetched, u.trace_id,
                        (SELECT count(*) FROM usage_events AS x
                         WHERE x.tenant_id = u.tenant_id AND x.operation = 'fetch') AS usage_count
                    FROM usage_events AS u JOIN evidence_records AS e
                        ON e.usage_id = u.id AND e.tenant_id = u.tenant_id
                    WHERE u.id = :job_id AND u.tenant_id = :tenant_id AND u.operation = 'fetch'
                """),
                {"job_id": job.job_id, "tenant_id": job.tenant_id},
            )
        ).one_or_none()
    if (
        row is None
        or row.trace_id != report.trace_id
        or row.pages_fetched != 1
        or row.usage_count != 1
        or EvidenceRecord.model_validate(row.record) != record
    ):
        raise ProbeFailure("PROBE_USAGE_MISMATCH")
    report.usage_status = "passed"


async def main(origin: str, report: ProbeReport, *, include_versions: bool) -> None:
    report.target_url_sha256 = sha256(origin.encode()).hexdigest()
    if include_versions:
        try:
            value = await asyncio.to_thread(WORKER_VERSION.read_text, encoding="utf-8")
            report.worker_version = release_version(value.strip())
        except (OSError, UnicodeError):
            pass
    db = None
    key = None
    try:
        async with httpx.AsyncClient(
            base_url=BASE,
            trust_env=False,
            follow_redirects=False,
            timeout=5,
        ) as api:
            if include_versions:
                report.api_version = await api_version(api)
            try:
                config = Settings.from_env()
            except (OSError, ValueError):
                raise ProbeFailure("PROBE_CONFIGURATION_FAILED") from None
            db = create_async_engine(config.database_url, hide_parameters=True)
            identities = IdentityStore(db, config.key_pepper)
            tenant = f"public-fetch-probe-{uuid4().hex[:12]}"
            key = await identities.issue(tenant, f"{tenant}@example.invalid", ["fetch"], 1)
            api.headers["Authorization"] = f"Bearer {key}"
            record, job = await fetch_evidence(api, origin, report)
        await verify_usage(db, record, job, report)
    except ProbeFailure as error:
        report.reason_codes.append(str(error))
    except (TimeoutError, httpx.TimeoutException):
        report.reason_codes.append("PROBE_TIMEOUT")
    except httpx.HTTPError:
        report.reason_codes.append("PROBE_API_UNAVAILABLE")
    except SQLAlchemyError:
        report.reason_codes.append("PROBE_DATABASE_FAILED")
    except (ValueError, RecursionError):
        report.reason_codes.append("PROBE_INVALID_DATA")
    except OSError:
        report.reason_codes.append("PROBE_RUNTIME_UNAVAILABLE")
    finally:
        if key is not None:
            try:
                if not await identities.revoke(UUID(hex=key[4:].split(".", 1)[0])):
                    report.reason_codes.append("PROBE_KEY_REVOKE_FAILED")
            except (OSError, SQLAlchemyError, ValueError):
                report.reason_codes.append("PROBE_KEY_REVOKE_FAILED")
        if db is not None:
            try:
                await db.dispose()
            except (OSError, SQLAlchemyError):
                report.reason_codes.append("PROBE_DATABASE_FAILED")
    if include_versions:
        if report.api_version is None or report.worker_version is None:
            report.reason_codes.append("PROBE_VERSION_UNAVAILABLE")
        elif report.api_version != report.worker_version:
            report.reason_codes.append("PROBE_VERSION_MISMATCH")
    if not report.reason_codes:
        report.result = "passed"


def cli() -> int:
    parser = argparse.ArgumentParser(
        description="在启用 fetch 配置的 multisearch-worker 容器中运行公开正文验收。",
        epilog=(
            "HTML 示例 https://www.rfc-editor.org/rfc/rfc8785.html。"
            "PDF 示例 https://www.rfc-editor.org/rfc/rfc8785.pdf，指定 --method pypdf。"
        ),
    )
    parser.add_argument("url", help="已确认可抓取和留存摘要的公开 HTTP(S) URL")
    parser.add_argument("--method", choices=tuple(LOCATORS), default="crawl4ai_static")
    parser.add_argument("--json", action="store_true", help="在标准输出写入脱敏 JSON 验收记录")
    args = parser.parse_args()
    report = ProbeReport(args.method)
    try:
        origin = _target(args.url).url
    except FetchError as error:
        if not args.json:
            parser.error(error.code)
        report.reason_codes.append("FETCH_TARGET_BLOCKED")
        print(json.dumps(asdict(report), ensure_ascii=True))
        return 2
    asyncio.run(main(origin, report, include_versions=args.json))
    if args.json:
        print(json.dumps(asdict(report), ensure_ascii=True))
    elif report.result == "passed":
        print(f"公开正文验收通过 · {args.method} · {report.evidence_id} · 单次计量与持久证据一致")
    elif report.reason_codes == ["PROBE_ENQUEUE_FAILED"]:
        raise SystemExit(f"抓取任务入队失败，HTTP {report.submit_http_status}")
    else:
        details = [report.job_status] if report.job_status else []
        details.extend(report.reason_codes)
        if report.dns_status in DNS_MESSAGES:
            details.append(DNS_MESSAGES[report.dns_status])
        raise SystemExit("公开正文验收未通过 · " + " · ".join(details))
    return 0 if report.result == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(cli())
