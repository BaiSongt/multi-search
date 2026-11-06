"""在启用研究 Worker 的 Compose API 容器内运行 REST 与 MCP 端到端验收。"""

import argparse
import asyncio
import json
from base64 import b64decode
from datetime import datetime
from uuid import UUID, uuid4

import httpx
import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from multisearch.identity import IdentityStore
from multisearch.local_index import LocalDocument, LocalIndex
from multisearch.settings import Settings, secret

BASE = "http://127.0.0.1:8765"
TERMINAL = {"completed", "partial", "failed", "cancelled"}


async def job_payload(client: Client, job_id: str) -> dict:
    result = await client.call_tool("get_search_job", {"job_id": job_id})
    assert not result.is_error
    state = result.structured_content
    if state.get("delivery") != "deferred":
        return state
    reference = state["result_ref"]
    chunks = []
    for index in range(reference["page_count"]):
        part = await client.call_tool(
            reference["fetch_tool"], {"result_id": reference["result_id"], "page": index}
        )
        assert not part.is_error and part.structured_content["page"] == index
        chunks.append(b64decode(part.structured_content["data_base64"], validate=True))
    payload = b"".join(chunks)
    assert len(payload) == reference["payload_bytes"]
    return json.loads(payload)


async def main(*, local_date_window: bool):
    config = Settings.from_env()
    db = create_async_engine(config.database_url, hide_parameters=True)
    store = IdentityStore(db, config.key_pepper)
    keys = []
    tenants = [f"research-probe-{uuid4().hex[:12]}" for _ in range(2)]
    indexed = False
    try:
        for tenant in tenants:
            keys.append(
                await store.issue(
                    tenant, f"{tenant}@example.invalid", ["research", "search:text"], 1
                )
            )
        if local_date_window:
            dates = {
                "before": "2026-03-01T07:59:59+08:00",
                "start": "2026-03-01T08:00:00+08:00",
                "end": "2026-04-01T07:59:59+08:00",
                "after": "2026-04-01T08:00:00+08:00",
                "unknown": None,
            }
            documents = [
                LocalDocument(
                    id=label,
                    title=f"Datewindow {label}",
                    text="Datewindow research probe.",
                    published_at=datetime.fromisoformat(value) if value else None,
                )
                for label, value in dates.items()
            ]
            assert await LocalIndex(db).import_documents(tenants[0], documents) == len(documents)
            indexed = True
        headers = {"Authorization": "Bearer " + keys[0]}
        async with (
            httpx.AsyncClient(base_url=BASE, headers=headers, trust_env=False, timeout=20) as api,
            httpx2.AsyncClient(headers=headers, trust_env=False) as mcp_http,
            Client(streamable_http_client(BASE + "/mcp", http_client=mcp_http)) as client,
        ):
            assert (await api.get("/api/v1/capabilities")).json()["research"] == "available"
            assert (await api.get("/metrics")).status_code == 401
            payload = {
                "query": "attention transformer neural network",
                "source_types": ["academic"],
                "max_search_rounds": 2,
                "max_pages": 1,
                "max_latency_ms": 40_000,
                "idempotency_key": f"research-{uuid4().hex[:12]}",
            }
            if local_date_window:
                payload.update(
                    query="datewindow",
                    source_types=["local"],
                    network_scope="offline",
                    max_pages=0,
                    max_search_rounds=1,
                    time_range={"from": "2026-03-01", "to": "2026-03-31"},
                )
            first = await api.post("/api/v1/research", json=payload)
            assert first.status_code == 202 and first.headers.get("X-Trace-ID")
            accepted = first.json()
            assert accepted["trace_id"] == first.headers["X-Trace-ID"]
            second = await api.post("/api/v1/search", json={**payload, "mode": "deep"})
            assert second.status_code == 202 and second.json()["job_id"] == accepted["job_id"]
            assert second.json()["trace_id"] == accepted["trace_id"]
            tools = {item.name for item in (await client.list_tools()).tools}
            assert tools == {
                "multi_search",
                "image_search",
                "web_fetch",
                "start_deep_research",
                "get_search_job",
                "cancel_search_job",
                "get_result_page",
            }
            mcp = await client.call_tool("start_deep_research", payload)
            assert not mcp.is_error and mcp.structured_content["job_id"] == accepted["job_id"]
            assert mcp.structured_content["trace_id"] == accepted["trace_id"]
            denied = await api.get(
                accepted["status_url"], headers={"Authorization": "Bearer " + keys[1]}
            )
            assert denied.status_code == 404
            async with asyncio.timeout(45):
                while True:
                    state = (await api.get(accepted["status_url"])).json()
                    if state["status"] in TERMINAL:
                        break
                    await asyncio.sleep(0.4)
            assert state["status"] in {"completed", "partial"}, state.get("error")
            assert state["trace_id"] == accepted["trace_id"] and state["request_id"]
            assert state["result"]["results"] and state["result"]["usage"]["pages_charged"] <= 1
            if local_date_window:
                assert state["status"] == "completed"
                results = state["result"]["results"]
                assert {row["title"] for row in results} == {"Datewindow start", "Datewindow end"}
                assert all(row["source_type"] == "local" for row in results)
                assert state["result"]["usage"]["pages_charged"] == 0
            assert await job_payload(client, accepted["job_id"]) == state
            metrics = await api.get(
                "/metrics",
                headers={"Authorization": "Bearer " + secret("MULTISEARCH_METRICS_TOKEN")},
            )
            assert metrics.status_code == 200 and "multisearch_jobs_active" in metrics.text
            assert accepted["job_id"] not in metrics.text and keys[0] not in metrics.text
        async with db.connect() as connection:
            job_id = UUID(accepted["job_id"])
            row = (
                (
                    await connection.execute(
                        text("""
                SELECT research_jobs_started, trace_id, request_id, llm_usage_unknown,
                    llm_tokens_reserved, estimated_cost
                FROM usage_events WHERE id = :id AND operation = 'research'
            """),
                        {"id": job_id},
                    )
                )
                .mappings()
                .one()
            )
            assert row["research_jobs_started"] == 1 and row["trace_id"] == state["trace_id"]
            assert row["request_id"] == state["request_id"]
            assert not row["llm_usage_unknown"] and row["llm_tokens_reserved"] == 0
            assert row["estimated_cost"] == 0
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM provider_runs WHERE usage_id = :id"), {"id": job_id}
                )
                > 0
            )
            if local_date_window:
                stored = await connection.scalar(
                    text("SELECT payload FROM search_jobs WHERE id = :id"), {"id": job_id}
                )
                assert stored == {}, "完成任务应清除原始请求负载"
                print("研究日期范围通过：保留 UTC 首尾日，排除范围外和无发布日期的文档")
        print(
            f"REST 与 MCP 共用研究任务：{state['status']}，"
            f"{len(state['result']['results'])} 条候选，"
            f"{len(state['result']['evidence_records'])} 条正文证据，追踪、单次计量与指标鉴权通过"
        )
    finally:
        for key in keys:
            await store.revoke(UUID(hex=key[4:].split(".", 1)[0]))
        if indexed:
            async with db.begin() as connection:
                await connection.execute(
                    text("""
                        DELETE FROM local_documents
                        WHERE tenant_id = (SELECT id FROM tenants WHERE slug = :slug)
                    """),
                    {"slug": tenants[0]},
                )
        await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--local-date-window", action="store_true", help="验收本地研究的 UTC 日期范围"
    )
    asyncio.run(main(local_date_window=parser.parse_args().local_date_window))
