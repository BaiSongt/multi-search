from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from multisearch.auth import console_origin
from multisearch.contracts import Role
from multisearch.errors import ApiError, install_errors
from multisearch.identity import KeyView
from multisearch.management_identity import KeyCreate, router
from multisearch.sessions import (
    SESSION_COOKIE,
    SessionIdentity,
    _hash_password,
    _verify_password,
)


def test_password_hash_has_unique_salt_and_checks_candidate():
    password = "identity-test-only"
    first, second = _hash_password(password), _hash_password(password)
    assert first != second
    assert password not in first
    assert _verify_password(password, first)
    assert not _verify_password(password + "!", first)


@pytest.mark.parametrize(
    "changes",
    [
        {"scopes": ["search:image", "search:image"]},
        {"scopes": ["write"]},
        {"ttl_hours": 0},
        {"limits": {"requests_per_minute": True}},
        {"limits": {"unknown": 2}},
        {"ip_allowlist": ["*"]},
        {"ip_allowlist": ["192.0.2.10/24"]},
        {"ip_allowlist": ["192.0.2.0/255.255.255.0"]},
        {"ip_allowlist": ["fe80::1%eth0"]},
        {"ip_allowlist": ["192.0.2.1", "::ffff:192.0.2.1"]},
        {"ip_allowlist": [f"192.0.2.{index}" for index in range(33)]},
        {"ip_allowlist": [1234]},
        {"ip_allowlist": "192.0.2.1"},
        {"ip_allowlist": None},
    ],
)
def test_key_contract_rejects_invalid_scopes_quotas_and_networks(changes):
    with pytest.raises(ValidationError):
        KeyCreate.model_validate({"name": "client", "scopes": ["search:text"], **changes})


def test_key_contract_normalizes_ip_networks_and_defaults_to_unrestricted():
    base = {"name": "client", "scopes": ["search:text"]}
    assert KeyCreate.model_validate(base).ip_allowlist == []
    value = KeyCreate.model_validate(
        {
            **base,
            "ip_allowlist": [
                " 192.0.2.1 ",
                "2001:0DB8::3",
                "::FFFF:198.51.100.0/120",
                "2001:db8::/64",
            ],
        }
    )
    assert value.ip_allowlist == [
        "192.0.2.1/32",
        "2001:db8::3/128",
        "198.51.100.0/24",
        "2001:db8::/64",
    ]


def test_console_origin_requires_tls_outside_loopback(monkeypatch):
    monkeypatch.setenv("MULTISEARCH_PUBLIC_CONSOLE_URL", "http://console.example.org")
    with pytest.raises(ValueError):
        console_origin()
    monkeypatch.setenv("MULTISEARCH_PUBLIC_CONSOLE_URL", "https://console.example.org:443/")
    assert console_origin() == ("https://console.example.org", True)


async def test_browser_login_cookie_csrf_and_machine_separation(monkeypatch):
    origin = "https://console.example.org"
    monkeypatch.setenv("MULTISEARCH_PUBLIC_CONSOLE_URL", origin)
    now = datetime.now(UTC)
    actor = SessionIdentity(
        uuid4(),
        "test",
        uuid4(),
        "user@example.org",
        Role.DEVELOPER,
        uuid4(),
        now + timedelta(hours=8),
        "a" * 64,
    )
    rotated = replace(actor, session_id=uuid4(), csrf_token="b" * 64)
    test_key = KeyView(
        id=uuid4(),
        owner_id=actor.user_id,
        name="test",
        environment="test",
        prefix="msk_1234",
        suffix="9abc",
        scopes=["search:text"],
        limits={},
        ip_allowlist=["192.0.2.1/32"],
        created_at=now,
        expires_at=now + timedelta(hours=1),
        revoked_at=None,
        last_used_at=None,
        rotation_expires_at=None,
        status="active",
    )

    class TestSessions:
        cookies = {}

        async def login(self, tenant, email, password):
            assert (tenant, email, password) == ("test", "user@example.org", "identity-test-only")
            self.cookies = {"test-session": actor}
            return "test-session", actor

        async def authenticate(self, raw):
            if raw not in self.cookies:
                raise ApiError(401, "AUTH_REQUIRED", "需要有效的登录会话")
            return self.cookies[raw]

        async def refresh(self, identity):
            assert identity == actor
            self.cookies = {"refreshed-session": rotated}
            return "refreshed-session", rotated

        async def logout(self, identity):
            assert identity == rotated
            self.cookies = {}

    class TestIdentity:
        pepper = "test-pepper"
        sessions = TestSessions()

        async def create_key(
            self,
            identity,
            name,
            environment,
            scopes,
            ttl_hours,
            limits,
            max_concurrent,
            *,
            ip_allowlist,
        ):
            identity.require_key_manager()
            assert identity == actor
            assert (name, environment, scopes, ttl_hours, limits, max_concurrent) == (
                "test",
                "test",
                ["search:text"],
                1,
                {"requests_per_minute": 2},
                5,
            )
            assert ip_allowlist == ["192.0.2.1/32"]
            return test_key, "test-key-shown-once"

        async def list_keys(self, identity, limit, cursor):
            identity.require_key_manager()
            assert identity == actor and limit == 50 and cursor is None
            return [test_key], None

    class TestRedis:
        async def eval(self, *_):
            return 0

    app = FastAPI()
    install_errors(app)
    app.include_router(router)
    app.state.identity = TestIdentity()
    app.state.redis = TestRedis()
    app.state.settings = SimpleNamespace(max_concurrent=20)

    async with AsyncClient(transport=ASGITransport(app), base_url=origin) as client:
        reply = await client.get("/api/v1/me", headers={"Authorization": "Bearer machine-key"})
        assert reply.status_code == 401
        credentials = {
            "tenant": "test",
            "email": "User@example.org",
            "password": "identity-test-only",
        }
        login_url = "/api/v1/auth/login"
        wrong_origin = await client.post(
            login_url, json=credentials, headers={"Origin": f"{origin}.evil.org"}
        )
        assert wrong_origin.status_code == 403
        reply = await client.post(login_url, json=credentials, headers={"Origin": origin})
        assert reply.status_code == 200
        assert "test-session" not in reply.text
        cookie = reply.headers["set-cookie"]
        assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
        assert "Domain=" not in cookie
        assert reply.headers["cache-control"] == "no-store"

        payload = {
            "name": "test",
            "environment": "test",
            "scopes": ["search:text"],
            "ttl_hours": 1,
            "limits": {"requests_per_minute": 2},
            "ip_allowlist": ["192.0.2.1"],
        }
        without_csrf = await client.post("/api/v1/keys", json=payload, headers={"Origin": origin})
        assert without_csrf.status_code == 403
        headers = {"Origin": origin, "X-CSRF-Token": actor.csrf_token}
        created = await client.post("/api/v1/keys", json=payload, headers=headers)
        assert created.status_code == 201 and created.json()["secret"] == "test-key-shown-once"
        assert created.json()["key"]["ip_allowlist"] == ["192.0.2.1/32"]
        keys = await client.get("/api/v1/keys")
        assert keys.status_code == 200 and "test-key-shown-once" not in keys.text
        assert keys.json()["keys"][0]["ip_allowlist"] == ["192.0.2.1/32"]
        reply = await client.post("/api/v1/auth/refresh", headers=headers)
        assert reply.status_code == 200 and reply.json()["csrf_token"] == rotated.csrf_token
        stale = await client.post("/api/v1/auth/logout", headers=headers)
        assert stale.status_code == 403
        headers["X-CSRF-Token"] = rotated.csrf_token
        reply = await client.post("/api/v1/auth/logout", headers=headers)
        assert reply.status_code == 204
        assert SESSION_COOKIE not in client.cookies
        assert (await client.get("/api/v1/me")).status_code == 401
