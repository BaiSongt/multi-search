"""在启用公开学术来源和研究 Worker 的 Compose API 容器内运行端到端验收。"""

import asyncio
from uuid import UUID, uuid4

import httpx
import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from multisearch.identity import IdentityStore
from multisearch.settings import Settings, secret

BASE = "http://127.0.0.1:8765"
TERMINAL = {"completed", "partial", "failed", "cancelled"}


async def main():
    config = Settings.from_env()
    db = create_async_engine(config.database_url, hide_parameters=True)
    store = IdentityStore(db, config.key_pepper)
    keys = []
    try:
        for _ in range(2):
            tenant = f"research-probe-{uuid4().hex[:12]}"
            keys.append(
                await store.issue(
                    tenant, f"{tenant}@example.invalid", ["research", "search:text"], 1
                )
            )
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
            mcp_state = await client.call_tool("get_search_job", {"job_id": accepted["job_id"]})
            assert (
                not mcp_state.is_error and mcp_state.structured_content["status"] == state["status"]
            )
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
        print(
            f"REST 与 MCP 共用研究任务：{state['status']}，"
            f"{len(state['result']['results'])} 条候选，"
            f"{len(state['result']['evidence_records'])} 条正文证据，追踪、单次计量与指标鉴权通过"
        )
    finally:
        for key in keys:
            await store.revoke(UUID(hex=key[4:].split(".", 1)[0]))
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
