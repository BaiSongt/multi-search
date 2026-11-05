from datetime import UTC, datetime
from uuid import uuid4

import httpx2
import pytest
from fastapi import FastAPI
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from multisearch.contracts import (
    ImageAttribution,
    ImageSearchResult,
    Modality,
    Mode,
    SearchBudget,
    SearchRequest,
    SearchResponse,
    Source,
    TextSearchResult,
)
from multisearch.errors import ApiError, install_errors
from multisearch.identity import MachineIdentity
from multisearch.mcp_api import create_mcp, create_mcp_app

BASE_URL = "http://127.0.0.1:8765"


@pytest.fixture
def server():
    keys = {
        "mixed": MachineIdentity(
            uuid4(), uuid4(), uuid4(), frozenset({"search:text", "search:image"})
        ),
        "text": MachineIdentity(uuid4(), uuid4(), uuid4(), frozenset({"search:text"})),
    }
    calls: list[tuple[SearchRequest, MachineIdentity, SearchResponse]] = []

    async def authenticate(key: str):
        if key not in keys:
            raise ApiError(401, "AUTH_REQUIRED", "需要有效的 API Key")
        return keys[key]

    async def search(request: SearchRequest, identity: MachineIdentity, request_id: str):
        identity.require_search(request.modalities)
        common = {
            "result_id": "fixture-result",
            "title": "结果示例",
            "source_type": Source.WEB,
            "url": "https://example.org/source",
            "canonical_url": "https://example.org/source",
            "provider_ids": ["test"],
            "retrieved_at": datetime(2026, 11, 5, tzinfo=UTC),
        }
        hit = (
            ImageSearchResult(
                **common,
                image=ImageAttribution(
                    original_url="https://example.org/image.png",
                    thumbnail_url="https://example.org/thumb.png",
                    source_page_url="https://example.org/source",
                ),
            )
            if request.modalities == [Modality.IMAGE]
            else TextSearchResult(**common)
        )
        response = SearchResponse(
            request_id=request_id,
            status="completed",
            mode_requested=request.mode,
            mode_executed=Mode.STANDARD,
            partial=False,
            elapsed_ms=1,
            result_count=1,
            results=[hit],
            budget=SearchBudget(
                max_latency_ms=1_000, max_results=1, max_image_results=1, search_rounds=1
            ),
        )
        calls.append((request, identity, response))
        return response

    app = FastAPI()
    install_errors(app)
    mcp = create_mcp(search, version="0.1.0")
    app.router.add_route("/mcp", create_mcp_app(mcp, authenticate), include_in_schema=False)
    return app, mcp, keys, calls


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_official_client_streamable_http_contract(server, mode):
    app, mcp, keys, calls = server
    async with (
        mcp.session_manager.run(),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), headers={"Authorization": "Bearer mixed"}
        ) as http,
        Client(streamable_http_client(f"{BASE_URL}/mcp", http_client=http), mode=mode) as client,
    ):
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert set(tools) == {"multi_search", "image_search"}
        assert "modalities" in tools["multi_search"].input_schema["properties"]
        assert "modalities" not in tools["image_search"].input_schema["properties"]
        assert "不代表获得使用授权" in tools["image_search"].description
        assert tools["multi_search"].output_schema == SearchResponse.model_json_schema()
        text = await client.call_tool(
            "multi_search", {"query": " NASA ", "mode": "quick", "request_id": "search-one"}
        )
        assert not text.is_error
        request, identity, rest_response = calls[-1]
        assert request.query == "NASA" and request.mode == Mode.QUICK
        assert identity == keys["mixed"]
        assert text.structured_content == rest_response.model_dump(mode="json")
        assert text.structured_content["request_id"] == "search-one"

        image = await client.call_tool(
            "image_search", {"query": "NASA", "image_options": {"min_width": 100}}
        )
        assert not image.is_error
        assert calls[-1][0].modalities == [Modality.IMAGE]
        attribution = image.structured_content["results"][0]["image"]
        assert attribution["source_page_url"] == "https://example.org/source"
        assert attribution["original_url"] == "https://example.org/image.png"
        assert attribution["thumbnail_url"] == "https://example.org/thumb.png"
        assert attribution["rights_status"] == "unknown"


async def test_streamable_http_scope_and_validation(server):
    app, mcp, keys, calls = server
    async with (
        mcp.session_manager.run(),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), headers={"Authorization": "Bearer text"}
        ) as http,
        Client(streamable_http_client(f"{BASE_URL}/mcp", http_client=http)) as client,
    ):
        denied = await client.call_tool("image_search", {"query": "NASA"})
        assert denied.is_error and denied.structured_content["code"] == "FORBIDDEN"
        invalid = await client.call_tool("multi_search", {"query": ["confidential-rejected-input"]})
        assert invalid.is_error and invalid.structured_content["code"] == "INVALID_REQUEST"
        assert "confidential-rejected-input" not in invalid.model_dump_json()
        wrong_alias = await client.call_tool(
            "image_search", {"query": "NASA", "modalities": ["text"]}
        )
        assert wrong_alias.is_error and wrong_alias.structured_content["code"] == "INVALID_REQUEST"
        assert not calls
        allowed = await client.call_tool("multi_search", {"query": "NASA"})
        assert not allowed.is_error
        assert calls[-1][1].tenant_id == keys["text"].tenant_id


async def test_streamable_http_auth_host_origin_and_body_limit(server):
    app, mcp, keys, calls = server
    async with (
        mcp.session_manager.run(),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE_URL) as http,
    ):
        envelope = {
            "jsonrpc": "2.0",
            "method": "initialize",
            "id": 1,
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "security-test", "version": "1"},
            },
        }
        headers = {"Accept": "application/json, text/event-stream"}
        response = await http.post("/mcp", json=envelope, headers=headers)
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json()["request_id"] == response.headers["X-Request-ID"]

        headers["Authorization"] = "Bearer mixed"
        assert (
            await http.post("/mcp", json=envelope, headers={**headers, "Host": "evil.example"})
        ).status_code == 421
        assert (
            await http.post(
                "/mcp", json=envelope, headers={**headers, "Origin": "https://evil.example"}
            )
        ).status_code == 403
        assert (
            await http.post("/mcp", json={"data": "a" * 33_000}, headers=headers)
        ).status_code == 413
        assert (await http.post("/mcp", json=envelope, headers=headers)).status_code == 200

        del keys["mixed"]
        tool_call = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 2,
            "params": {"name": "multi_search", "arguments": {"query": "NASA"}},
        }
        assert (await http.post("/mcp", json=tool_call, headers=headers)).status_code == 401
        assert not calls
