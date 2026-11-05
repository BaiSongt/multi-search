from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from multisearch.auth import SearchKey
from multisearch.contracts import Modality, SearchRequest
from multisearch.errors import ApiError, install_errors
from multisearch.identity import MachineIdentity, key_hash


@pytest.mark.parametrize(
    "invalid",
    [
        {"query": " "},
        {"source_types": []},
        {"modalities": ["text", "text"]},
        {"source_types": ["image"]},
        {"image_options": {"license_policy": "verified_only"}},
        {"time_range": {"from": "2026-02-01", "to": "2026-01-01"}},
        {"max_results": 31},
        {"domain_allowlist": ["https://example.org/path"]},
    ],
)
def test_invalid_contract(invalid):
    with pytest.raises(ValidationError):
        SearchRequest.model_validate({"query": "example", **invalid})


def test_mixed_search_contract():
    result = SearchRequest.model_validate(
        {
            "query": " 示例 ",
            "modalities": ["image", "text"],
            "source_types": ["web"],
            "image_options": {"min_width": 1000},
            "time_range": {"from": "2025-01-01", "to": "2026-01-01"},
        }
    )
    assert result.query == "示例"
    assert result.image_options.min_width == 1000


async def test_machine_permissions_and_validation_redaction():
    app = FastAPI()
    install_errors(app)
    key = MachineIdentity(uuid4(), uuid4(), uuid4(), frozenset({"search:text"}))

    class TestIdentity:
        async def authenticate(self, raw, *, peer):
            if raw == "valid":
                return key
            raise ApiError(401, "AUTH_REQUIRED", "需要有效的 API Key")

    app.state.identity = TestIdentity()

    @app.post("/api/v1/search")
    async def search(body: SearchRequest, identity: SearchKey):
        identity.require_search(body.modalities)
        return {"tenant": identity.tenant_id}

    async def chunks():
        yield b"0" * 30_000
        yield b"0" * 3000

    route = "/api/v1/search"
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        assert (await client.post(route, json={"query": "x"})).status_code == 401
        headers = {"Authorization": "Bearer valid"}
        invalid = await client.post(route, json={"query": ["sensitive"]}, headers=headers)
        assert invalid.status_code == 422
        assert "sensitive" not in invalid.text
        assert invalid.json()["request_id"] == invalid.headers["X-Request-ID"]
        oversize = await client.post(route, content=chunks(), headers=headers)
        assert oversize.status_code == 413
        assert oversize.json()["request_id"] == oversize.headers["X-Request-ID"]
        forbidden = await client.post(
            route, json={"query": "x", "modalities": ["image"]}, headers=headers
        )
        assert forbidden.status_code == 403
        success = await client.post(route, json={"query": "x"}, headers=headers)
        assert success.json() == {"tenant": str(key.tenant_id)}
    key.require_search([Modality.TEXT])


def test_key_hash_uses_pepper():
    assert len(key_hash("test", "secret")) == 32
    assert key_hash("test", "secret") != key_hash("test", "other")
