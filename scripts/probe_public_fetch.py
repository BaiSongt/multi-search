"""用启用抓取的 Compose 验证授权公开 URL 的实际正文证据。"""

import argparse
import asyncio
import re
from ipaddress import ip_address
from uuid import UUID, uuid4

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from multisearch.evidence_contracts import EvidenceRecord
from multisearch.identity import IdentityStore
from multisearch.safe_fetch import FetchError, _public_ip, _resolve, _target
from multisearch.settings import Settings

BASE = "http://multisearch-api:8765"
TERMINAL = {"completed", "partial", "failed", "cancelled"}
LOCATORS = {
    "static_html": "html_text",
    "crawl4ai_static": "html_text",
    "pypdf": "pdf_text",
    "browser_dom": "rendered_html_text",
}


async def diagnose_dns(url: str) -> str:
    target = _target(url)
    try:
        ip_address(target.host)
    except ValueError:
        pass
    else:
        return "入口使用公开 IP 字面量，请检查重定向和域名策略"
    try:
        async with asyncio.timeout(3):
            addresses = set(await _resolve(target.host, target.port))
    except (OSError, TimeoutError):
        return "Worker 入口域名 DNS 查询失败"
    if not addresses:
        return "Worker 入口域名 DNS 没有返回地址"
    for address in addresses:
        try:
            _public_ip(address)
        except FetchError:
            return "Worker 入口域名 DNS 返回至少一个非公网地址，地址策略拒绝连接"
    return "Worker 入口域名 DNS 返回公网地址，请检查重定向和域名策略"


async def main(url: str, method: str) -> None:
    origin = _target(url).url
    config = Settings.from_env()
    db = create_async_engine(config.database_url, hide_parameters=True)
    identities = IdentityStore(db, config.key_pepper)
    try:
        tenant = f"public-fetch-probe-{uuid4().hex[:12]}"
        key = await identities.issue(tenant, f"{tenant}@example.invalid", ["fetch"], 1)
        try:
            async with httpx.AsyncClient(
                base_url=BASE,
                headers={"Authorization": f"Bearer {key}"},
                trust_env=False,
                timeout=5,
            ) as api:
                record, job_id, trace_id = await fetch_evidence(api, origin, method)
            async with db.connect() as connection:
                row = (
                    await connection.execute(
                        text("""
                            SELECT e.record, u.pages_fetched, u.trace_id
                            FROM usage_events AS u JOIN evidence_records AS e
                                ON e.usage_id = u.id AND e.tenant_id = u.tenant_id
                            WHERE u.id = :job_id AND u.operation = 'fetch'
                        """),
                        {"job_id": job_id},
                    )
                ).one()
                assert row.trace_id == trace_id and row.pages_fetched == 1
                assert EvidenceRecord.model_validate(row.record) == record
            print(f"公开正文验收通过 · {method} · {record.evidence_id} · 单次计量与持久证据一致")
        finally:
            await identities.revoke(UUID(hex=key[4:].split(".", 1)[0]))
    finally:
        await db.dispose()


async def fetch_evidence(
    api: httpx.AsyncClient, url: str, method: str
) -> tuple[EvidenceRecord, UUID, str]:
    payload = {
        "url": url,
        "max_pages": 1,
        "max_latency_ms": 25_000,
        "idempotency_key": f"public-fetch-{uuid4().hex[:12]}",
    }
    if method == "browser_dom":
        payload["extraction_mode"] = "dynamic"
    response = await api.post("/api/v1/fetch", json=payload)
    if response.status_code != 202:
        raise SystemExit(f"抓取任务入队失败，HTTP {response.status_code}")
    accepted = response.json()
    trace_id = accepted["trace_id"]
    assert trace_id and trace_id == response.headers.get("X-Trace-ID")
    replay = await api.post("/api/v1/fetch", json=payload)
    assert replay.status_code == 202 and replay.json()["job_id"] == accepted["job_id"]
    async with asyncio.timeout(32):
        while True:
            polled = await api.get(accepted["status_url"])
            polled.raise_for_status()
            state = polled.json()
            if state["status"] in TERMINAL:
                break
            await asyncio.sleep(0.4)
    if state["status"] != "completed":
        result = state.get("result") or {}
        codes = [item["code"] for item in result.get("failures", [])]
        codes.extend(result.get("warnings", []))
        if state["error"]:
            codes.append(state["error"]["code"])
        details = [state["status"], *dict.fromkeys(codes)]
        if "FETCH_TARGET_BLOCKED" in codes:
            details.append(await diagnose_dns(url))
        raise SystemExit("公开正文验收未通过 · " + " · ".join(details))
    assert state["trace_id"] == trace_id and state["progress"]["pages_fetched"] == 1
    (raw,) = state["result"]["evidence_records"]
    record = EvidenceRecord.model_validate(raw)
    assert record.origin_url == url and record.extraction_method == method
    assert record.locator.kind == LOCATORS[method] and record.evidence_text.strip()
    assert record.locator.text_offset_end - record.locator.text_offset_start == len(
        record.evidence_text
    )
    assert re.fullmatch(r"[0-9a-f]{64}", record.content_sha256)
    evidence = await api.get(f"/api/v1/evidence/{record.evidence_id}")
    evidence.raise_for_status()
    assert EvidenceRecord.model_validate(evidence.json()) == record
    return record, UUID(accepted["job_id"]), trace_id


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="在启用 fetch 配置的 multisearch-worker 容器中运行公开正文验收。",
        epilog=(
            "HTML 示例 https://www.rfc-editor.org/rfc/rfc8785.html。"
            "PDF 示例 https://www.rfc-editor.org/rfc/rfc8785.pdf，指定 --method pypdf。"
        ),
    )
    parser.add_argument("url", help="已确认可抓取和留存摘要的公开 HTTP(S) URL")
    parser.add_argument("--method", choices=tuple(LOCATORS), default="crawl4ai_static")
    args = parser.parse_args()
    try:
        asyncio.run(main(args.url, args.method))
    except FetchError as error:
        parser.error(error.code)
