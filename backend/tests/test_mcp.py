import json
from base64 import b64decode, b64encode
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import httpx2
import pytest
from fastapi import FastAPI, Request
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from multisearch.contracts import (
    ImageAttribution,
    ImageSearchResult,
    Modality,
    Mode,
    ProviderRun,
    SearchBudget,
    SearchRequest,
    SearchResponse,
    Source,
    TextSearchResult,
)
from multisearch.errors import ApiError, install_errors
from multisearch.identity import MachineIdentity
from multisearch.job_contracts import (
    FetchRequest,
    JobAccepted,
    JobKind,
    JobProgress,
    JobStatus,
    JobView,
)
from multisearch.mcp_api import MCPDeferredResult, MCPJobs, create_mcp, create_mcp_app
from multisearch.mcp_results import MCPResultPage, MCPResultReference

BASE_URL = "http://127.0.0.1:8765"
WIRE_LIMIT = 16_384
PAYLOAD_MARKER = "fixture-private-snapshot"


class _ScopedResults:
    page_size = 6000

    def __init__(self):
        self.snapshots: dict[tuple, bytes] = {}
        self.write_error: ApiError | None = None

    async def put(self, identity, payload):
        if self.write_error:
            raise self.write_error
        result_id = uuid4().hex + uuid4().hex
        encoded = b64encode(payload)
        self.snapshots[(identity.tenant_id, identity.key_id, result_id)] = encoded
        return MCPResultReference(
            result_id=result_id,
            page_count=(len(encoded) + self.page_size - 1) // self.page_size,
            payload_bytes=len(payload),
            expires_in_seconds=30,
        )

    async def get(self, identity, result_id, page):
        encoded = self.snapshots.get((identity.tenant_id, identity.key_id, result_id))
        if encoded is None:
            raise ApiError(404, "MCP_RESULT_NOT_FOUND", "MCP 结果不存在或已过期")
        page_count = (len(encoded) + self.page_size - 1) // self.page_size
        if page >= page_count:
            raise ApiError(422, "MCP_RESULT_PAGE_OUT_OF_RANGE", "MCP 结果页码超出范围")
        return MCPResultPage(
            result_id=result_id,
            page=page,
            page_count=page_count,
            next_page=page + 1 if page + 1 < page_count else None,
            data_base64=encoded[page * self.page_size : (page + 1) * self.page_size].decode(
                "ascii"
            ),
        )


async def _reassemble(client, reference):
    assert reference.fetch_tool == "get_result_page" and reference.encoding == "base64"
    chunks = []
    for index in range(reference.page_count):
        result = await client.call_tool(
            reference.fetch_tool, {"result_id": reference.result_id, "page": index}
        )
        assert not result.is_error
        assert json.loads(result.content[0].text) == result.structured_content
        page = MCPResultPage.model_validate(result.structured_content)
        assert page.result_id == reference.result_id and page.page == index
        assert page.page_count == reference.page_count
        assert page.next_page == (index + 1 if index + 1 < reference.page_count else None)
        chunks.append(b64decode(page.data_base64, validate=True))
    payload = b"".join(chunks)
    assert len(payload) == reference.payload_bytes
    return payload


@pytest.fixture
def server():
    return _server()


def _server(jobs=None, extra_keys=None, result_store=None):
    keys = {
        "mixed": MachineIdentity(
            uuid4(), uuid4(), uuid4(), frozenset({"search:text", "search:image"})
        ),
        "text": MachineIdentity(uuid4(), uuid4(), uuid4(), frozenset({"search:text"})),
    }
    keys.update(extra_keys or {})
    calls: list[tuple[SearchRequest, MachineIdentity, SearchResponse]] = []

    async def authenticate(key: str, request: Request):
        if key not in keys:
            raise ApiError(401, "AUTH_REQUIRED", "需要有效的 API Key")
        return keys[key]

    async def search(request: SearchRequest, identity: MachineIdentity, request_id: str):
        identity.require_search(request.modalities)
        if request.query == "fixture-provider-failure":
            raise ApiError(
                502,
                "PROVIDER_UNAVAILABLE",
                "当前没有可用的检索来源",
                retryable=True,
                provider_runs=[
                    ProviderRun(
                        provider_id="searxng",
                        source_type=Source.WEB,
                        modality=Modality.TEXT,
                        status="rate_limited",
                        elapsed_ms=7,
                    )
                ],
            )
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
        hits = [hit]
        if request.query == "fixture-large":
            hits = [
                hit.model_copy(
                    update={
                        "result_id": f"fixture-result-{index}",
                        "title": '大结果🙂\t"\\' * 700,
                        "snippet": PAYLOAD_MARKER,
                    }
                )
                for index in range(4)
            ]
        response = SearchResponse(
            request_id=request_id,
            status="completed",
            mode_requested=request.mode,
            mode_executed=Mode.STANDARD,
            partial=False,
            elapsed_ms=1,
            result_count=len(hits),
            results=hits,
            budget=SearchBudget(
                max_latency_ms=1_000, max_results=len(hits), max_image_results=1, search_rounds=1
            ),
        )
        calls.append((request, identity, response))
        return response

    app = FastAPI()
    install_errors(app)
    mcp = create_mcp(search, version="0.1.0", jobs=jobs, result_store=result_store)
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

        unavailable = await client.call_tool("multi_search", {"query": "fixture-provider-failure"})
        assert unavailable.is_error
        assert unavailable.structured_content["code"] == "PROVIDER_UNAVAILABLE"
        assert unavailable.structured_content["provider_runs"][0]["status"] == "rate_limited"
        assert "fixture-provider-failure" not in unavailable.model_dump_json()


@pytest.mark.parametrize("mode, tool", [("auto", "multi_search"), ("legacy", "image_search")])
async def test_official_client_delivers_bounded_unicode_search_pages(mode, tool):
    store = _ScopedResults()
    app, mcp, _, calls = _server(result_store=lambda: store)
    wire = []

    async def capture_tool_response(response):
        if json.loads(response.request.content).get("method") == "tools/call":
            assert response.headers["cache-control"] == "no-store"
            wire.append(await response.aread())

    async with (
        mcp.session_manager.run(),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            headers={"Authorization": "Bearer mixed"},
            event_hooks={"response": [capture_tool_response]},
        ) as http,
        Client(streamable_http_client(f"{BASE_URL}/mcp", http_client=http), mode=mode) as client,
    ):
        tools = {entry.name: entry for entry in (await client.list_tools()).tools}
        assert set(tools) == {"multi_search", "image_search", "get_result_page"}
        assert tools["get_result_page"].output_schema == MCPResultPage.model_json_schema()
        for name in ("multi_search", "image_search"):
            schema = tools[name].output_schema
            assert schema["type"] == "object"
            assert {entry["$ref"] for entry in schema["anyOf"]} == {
                "#/$defs/SearchResponse",
                "#/$defs/MCPDeferredResult",
            }

        small = await client.call_tool(tool, {"query": "fixture-small"})
        assert not small.is_error
        assert small.structured_content == calls[-1][2].model_dump(mode="json")
        assert json.loads(small.content[0].text) == small.structured_content

        result = await client.call_tool(
            tool, {"query": "fixture-large", "request_id": "large-search"}
        )
        assert not result.is_error
        preview = MCPDeferredResult.model_validate_json(result.content[0].text)
        assert preview.model_dump(mode="json") == result.structured_content
        assert preview.delivery == "deferred" and preview.kind == "search"
        assert preview.request_id == "large-search" and preview.result_count == 4
        assert len(preview.preview) == 3 and all(len(hit.title) == 160 for hit in preview.preview)
        if tool == "image_search":
            assert all(
                hit.kind == "image" and hit.rights_status == "unknown" for hit in preview.preview
            )
        else:
            assert all(hit.kind == "text" for hit in preview.preview)
        assert preview.result_ref.page_count > 1
        assert PAYLOAD_MARKER.encode() not in wire[-1]
        expected = calls[-1][2].model_dump_json().encode()
        assert await _reassemble(client, preview.result_ref) == expected

    assert len(wire) == 2 + preview.result_ref.page_count
    assert all(len(envelope) <= WIRE_LIMIT for envelope in wire)


async def test_result_pages_require_original_key_and_hide_expired_references():
    store = _ScopedResults()
    app, mcp, keys, _ = _server(result_store=lambda: store)
    keys["same-tenant"] = replace(keys["mixed"], key_id=uuid4())
    keys["other-tenant"] = replace(keys["mixed"], tenant_id=uuid4())
    async with (
        mcp.session_manager.run(),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), headers={"Authorization": "Bearer mixed"}
        ) as http,
        Client(streamable_http_client(f"{BASE_URL}/mcp", http_client=http)) as client,
    ):
        result = await client.call_tool("multi_search", {"query": "fixture-large"})
        reference = MCPDeferredResult.model_validate(result.structured_content).result_ref
        args = {"result_id": reference.result_id, "page": 0}
        assert not (await client.call_tool("get_result_page", args)).is_error
        beyond = await client.call_tool("get_result_page", {**args, "page": reference.page_count})
        assert (
            beyond.is_error and beyond.structured_content["code"] == "MCP_RESULT_PAGE_OUT_OF_RANGE"
        )
        for invalid in (
            {**args, "page": -1},
            {**args, "page": True},
            {**args, "result_id": "invalid-ref"},
        ):
            rejected = await client.call_tool("get_result_page", invalid)
            assert rejected.is_error and rejected.structured_content["code"] == "INVALID_REQUEST"

        rejections = []
        for key in ("same-tenant", "other-tenant", "mixed"):
            if key == "mixed":
                store.snapshots.clear()
            http.headers["Authorization"] = f"Bearer {key}"
            denied = await client.call_tool("get_result_page", args)
            assert (
                denied.is_error and json.loads(denied.content[0].text) == denied.structured_content
            )
            assert denied.structured_content["code"] == "MCP_RESULT_NOT_FOUND"
            assert "data_base64" not in denied.structured_content
            rejections.append(
                {
                    name: value
                    for name, value in denied.structured_content.items()
                    if name != "request_id"
                }
            )
        assert rejections[0] == rejections[1] == rejections[2]


@pytest.mark.parametrize(
    "status, code", [(503, "MCP_RESULT_STORE_UNAVAILABLE"), (413, "MCP_RESULT_TOO_LARGE")]
)
async def test_deferred_search_failure_never_releases_full_payload(status, code):
    store = _ScopedResults()
    store.write_error = ApiError(status, code, "fixture snapshot failure")
    app, mcp, _, _ = _server(result_store=lambda: store)
    async with (
        mcp.session_manager.run(),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), headers={"Authorization": "Bearer mixed"}
        ) as http,
        Client(streamable_http_client(f"{BASE_URL}/mcp", http_client=http)) as client,
    ):
        result = await client.call_tool(
            "multi_search", {"query": "fixture-large", "request_id": "delivery-failed"}
        )
        assert result.is_error
        assert result.structured_content["code"] == code
        assert result.structured_content["request_id"] == "delivery-failed"
        assert (
            "results" not in result.structured_content
            and "result_ref" not in result.structured_content
        )
        assert PAYLOAD_MARKER not in result.model_dump_json()


async def test_no_result_store_cannot_release_an_overlong_payload(server):
    app, mcp, _, _ = server
    async with (
        mcp.session_manager.run(),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), headers={"Authorization": "Bearer mixed"}
        ) as http,
        Client(streamable_http_client(f"{BASE_URL}/mcp", http_client=http)) as client,
    ):
        result = await client.call_tool("multi_search", {"query": "fixture-large"})
        assert (
            result.is_error and result.structured_content["code"] == "MCP_RESULT_STORE_UNAVAILABLE"
        )
        assert PAYLOAD_MARKER not in result.model_dump_json()


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


@pytest.fixture
def jobs_server():
    job_scopes = frozenset({"search:text", "search:image", "fetch", "research"})
    identities = {
        "jobs": MachineIdentity(uuid4(), uuid4(), uuid4(), job_scopes),
        "other": MachineIdentity(uuid4(), uuid4(), uuid4(), job_scopes),
    }
    identities["fetch-only"] = MachineIdentity(
        uuid4(), identities["jobs"].tenant_id, uuid4(), frozenset({"fetch"})
    )
    job_calls = []
    rows = {}
    store = _ScopedResults()
    now = datetime(2026, 11, 5, tzinfo=UTC)

    def create(kind, body, identity, request_id):
        if kind.value not in identity.scopes:
            raise ApiError(403, "FORBIDDEN", "API Key 缺少任务作用域")
        if kind == JobKind.RESEARCH:
            identity.require_search(body.modalities)
        job_id = uuid4()
        job_calls.append((kind, body, identity, request_id))
        rows[job_id] = JobView(
            job_id=job_id,
            tenant_id=identity.tenant_id,
            kind=kind,
            status=JobStatus.QUEUED,
            created_at=now,
            updated_at=now,
            deadline_at=now,
            attempts=0,
            max_attempts=2,
            progress=JobProgress(stage="queued", completed=0, total=1),
            result={"report": "x" * 20_000},
            error=None,
        )
        return JobAccepted(
            job_id=job_id,
            status=JobStatus.QUEUED,
            status_url=f"/api/v1/jobs/{job_id}",
            budget={"max_latency_ms": body.max_latency_ms or 60_000},
        )

    async def fetch(body, identity, request_id):
        return create(JobKind.FETCH, body, identity, request_id)

    async def research(body, identity, request_id):
        return create(JobKind.RESEARCH, body, identity, request_id)

    async def get(job_id, identity):
        if not identity.scopes & {"fetch", "research"}:
            raise ApiError(403, "FORBIDDEN", "API Key 缺少任务作用域")
        row = rows.get(job_id)
        if (
            row is None
            or row.tenant_id != identity.tenant_id
            or row.kind.value not in identity.scopes
        ):
            raise ApiError(404, "JOB_NOT_FOUND", "任务不存在")
        return row

    async def cancel(job_id, identity):
        row = await get(job_id, identity)
        row = row.model_copy(update={"status": JobStatus.CANCELLED})
        rows[job_id] = row
        return row

    app, mcp, _, search_calls = _server(
        MCPJobs(fetch, research, get, cancel), identities, result_store=lambda: store
    )
    return app, mcp, identities, job_calls, search_calls, store


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_official_client_discovers_and_runs_typed_job_tools(jobs_server, mode):
    app, mcp, identities, job_calls, search_calls, store = jobs_server
    correlation_ids = []

    async def remember_response(response):
        correlation_ids.append(response.headers.get("x-request-id"))

    async with (
        mcp.session_manager.run(),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            headers={"Authorization": "Bearer jobs"},
            event_hooks={"response": [remember_response]},
        ) as http,
        Client(streamable_http_client(f"{BASE_URL}/mcp", http_client=http), mode=mode) as client,
    ):
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert set(tools) == {
            "multi_search",
            "image_search",
            "web_fetch",
            "start_deep_research",
            "get_search_job",
            "cancel_search_job",
            "get_result_page",
        }
        assert tools["web_fetch"].input_schema == FetchRequest.model_json_schema()
        assert tools["web_fetch"].output_schema == JobAccepted.model_json_schema()
        assert tools["get_search_job"].input_schema["properties"]["job_id"]["format"] == "uuid"
        assert len(tools["get_search_job"].output_schema["anyOf"]) == 2
        assert tools["cancel_search_job"].output_schema == JobView.model_json_schema()
        assert len(tools["multi_search"].output_schema["anyOf"]) == 3
        assert tools["image_search"].output_schema["type"] == "object"

        fetched = await client.call_tool(
            "web_fetch",
            {
                "urls": ["https://example.org/first", "https://example.org/second"],
                "max_pages": 1,
                "request_id": "fetch-fixture",
                "idempotency_key": "fetch-once",
            },
        )
        assert not fetched.is_error
        accepted = JobAccepted.model_validate(fetched.structured_content)
        kind, body, identity, request_id = job_calls[-1]
        assert (
            kind == JobKind.FETCH
            and identity == identities["jobs"]
            and request_id == "fetch-fixture"
        )
        assert (
            body.targets() == ["https://example.org/first"] and body.idempotency_key == "fetch-once"
        )
        assert "fetch-fixture" in correlation_ids
        assert accepted.status_url == f"/api/v1/jobs/{accepted.job_id}"
        job_ref = {"job_id": str(accepted.job_id)}
        viewed = await client.call_tool("get_search_job", job_ref)
        assert not viewed.is_error
        deferred = MCPDeferredResult.model_validate(viewed.structured_content)
        assert deferred.kind == "job" and deferred.job_id == accepted.job_id
        assert "report" not in json.loads(viewed.content[0].text)
        full = JobView.model_validate_json(await _reassemble(client, deferred.result_ref))
        assert full.job_id == accepted.job_id and full.result == {"report": "x" * 20_000}
        store.write_error = ApiError(
            503, "MCP_RESULT_STORE_UNAVAILABLE", "fixture snapshot failure"
        )
        cancelled = await client.call_tool("cancel_search_job", job_ref)
        assert not cancelled.is_error and cancelled.structured_content["status"] == "cancelled"
        assert cancelled.structured_content["result"] is None
        store.write_error = None

        for tool, args, modalities in (
            ("start_deep_research", {"query": "water", "max_search_rounds": 2}, [Modality.TEXT]),
            ("multi_search", {"query": "water", "mode": "deep"}, [Modality.TEXT]),
            ("image_search", {"query": "water", "mode": "deep"}, [Modality.IMAGE]),
        ):
            queued = await client.call_tool(tool, args)
            assert (
                not queued.is_error and JobAccepted.model_validate(queued.structured_content).job_id
            )
            assert job_calls[-1][0] == JobKind.RESEARCH
            assert job_calls[-1][1].mode == Mode.DEEP and job_calls[-1][1].modalities == modalities
        assert not search_calls
        synchronous = await client.call_tool("multi_search", {"query": "water", "mode": "quick"})
        assert (
            not synchronous.is_error and synchronous.structured_content["mode_requested"] == "quick"
        )
        assert len(search_calls) == 1


async def test_job_tool_validation_and_identity_isolation(jobs_server):
    app, mcp, _, job_calls, _, _ = jobs_server
    async with mcp.session_manager.run():
        async with (
            httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app), headers={"Authorization": "Bearer jobs"}
            ) as http,
            Client(streamable_http_client(f"{BASE_URL}/mcp", http_client=http)) as client,
        ):
            for tool, args in (
                ("web_fetch", {"url": "https://example.org", "urls": ["https://example.net"]}),
                ("start_deep_research", {"query": "water", "mode": "quick"}),
                ("get_search_job", {"job_id": "confidential-invalid-id"}),
            ):
                invalid = await client.call_tool(tool, args)
                assert invalid.is_error and invalid.structured_content["code"] == "INVALID_REQUEST"
                assert "confidential-invalid-id" not in invalid.model_dump_json()
            assert not job_calls
            queued = await client.call_tool("start_deep_research", {"query": "water"})
            job_ref = {"job_id": queued.structured_content["job_id"]}

        for key, code in (
            ("other", "JOB_NOT_FOUND"),
            ("fetch-only", "JOB_NOT_FOUND"),
            ("text", "FORBIDDEN"),
        ):
            async with (
                httpx2.AsyncClient(
                    transport=httpx2.ASGITransport(app=app),
                    headers={"Authorization": f"Bearer {key}"},
                ) as http,
                Client(streamable_http_client(f"{BASE_URL}/mcp", http_client=http)) as client,
            ):
                for name in ("get_search_job", "cancel_search_job"):
                    denied = await client.call_tool(name, job_ref)
                    assert denied.is_error and denied.structured_content["code"] == code
                if key in {"text", "fetch-only"}:
                    denied = await client.call_tool("start_deep_research", {"query": "water"})
                    assert denied.is_error and denied.structured_content["code"] == "FORBIDDEN"
