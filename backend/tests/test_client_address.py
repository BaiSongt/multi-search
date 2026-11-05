from ipaddress import ip_address
from types import SimpleNamespace
from uuid import uuid4

import httpx2
import pytest
from fastapi import FastAPI, Request

from multisearch.auth import SearchKey, authenticate_machine, machine_peer
from multisearch.errors import ApiError, install_errors
from multisearch.identity import MachineIdentity
from multisearch.mcp_api import create_mcp, create_mcp_app
from multisearch.settings import Settings

_ALLOWED = "198.51.100.24"
_INIT = {
    "jsonrpc": "2.0",
    "method": "initialize",
    "id": 1,
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "machine-address-test", "version": "1"},
    },
}


async def test_rest_and_mcp_enforce_the_same_verified_client_address():
    app = FastAPI()
    install_errors(app)
    app.state.settings = SimpleNamespace(trusted_proxies={ip_address("192.0.2.10")})
    identity = MachineIdentity(uuid4(), uuid4(), uuid4(), frozenset({"search:text"}))

    class Store:
        async def authenticate(self, raw, *, peer):
            if raw == "restricted" and peer != _ALLOWED:
                raise ApiError(401, "AUTH_REQUIRED", "需要有效的 API Key")
            return identity

    app.state.identity = Store()

    @app.get("/probe")
    async def read(key: SearchKey):
        return {"key": key.key_id}

    async def unused(*_):
        raise AssertionError("握手无需检索")

    async def mcp_auth(raw: str, request: Request):
        return await authenticate_machine(app.state.identity, raw, peer=machine_peer(request))

    mcp = create_mcp(unused, version="0.0.0")
    app.router.add_route("/mcp", create_mcp_app(mcp, mcp_auth))
    spoofed = [("X-Forwarded-For", _ALLOWED)]
    cases = [
        (_ALLOWED, [], 200),
        (f"::ffff:{_ALLOWED}", [], 200),
        ("192.0.2.11", spoofed, 401),
        ("192.0.2.11", [("Forwarded", f"for={_ALLOWED}"), ("X-Real-IP", _ALLOWED)], 401),
        ("192.0.2.10", spoofed, 200),
        ("192.0.2.10", [], 401),
        ("192.0.2.10", [("X-Forwarded-For", f"{_ALLOWED}, 192.0.2.11")], 401),
        ("192.0.2.10", [*spoofed, ("X-Forwarded-For", "192.0.2.11")], 401),
        ("192.0.2.10", [("X-Forwarded-For", f"{_ALLOWED}:443")], 401),
        ("192.0.2.10", [*spoofed, ("X-MultiSearch-Client-Unavailable", "1")], 401),
        (_ALLOWED, [("X-MultiSearch-Client-Unavailable", "1")], 401),
    ]
    async with mcp.session_manager.run():
        for peer, headers, status in cases:
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app, client=(peer, 12345)),
                base_url="http://127.0.0.1:8765",
                headers=[("Authorization", "Bearer restricted"), *headers],
            ) as http:
                rest = await http.get("/probe")
                protocol = await http.post(
                    "/mcp", headers={"Accept": "application/json, text/event-stream"}, json=_INIT
                )
                assert rest.status_code == protocol.status_code == status
                if status == 401:
                    assert rest.json()["code"] == protocol.json()["code"] == "AUTH_REQUIRED"

    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app, client=("192.0.2.10", 12345)),
        base_url="http://127.0.0.1:8765",
        headers={"Authorization": "Bearer unrestricted"},
    ) as http:
        assert (await http.get("/probe")).status_code == 200


def test_trusted_proxies_must_be_explicit_ip_addresses(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/example")
    monkeypatch.setenv("API_KEY_HASH_PEPPER", "x" * 32)
    monkeypatch.setenv("MULTISEARCH_TRUSTED_PROXIES", "192.0.2.10, ::FFFF:192.0.2.10, 2001:db8::1")
    assert Settings.from_env().trusted_proxies == {
        ip_address("192.0.2.10"),
        ip_address("2001:db8::1"),
    }
    for invalid in ("*", "192.0.2.0/24", "localhost", "192.0.2.10,", "::", "fe80::1%2", "ff00::1"):
        monkeypatch.setenv("MULTISEARCH_TRUSTED_PROXIES", invalid)
        with pytest.raises(ValueError, match="MULTISEARCH_TRUSTED_PROXIES"):
            Settings.from_env()
