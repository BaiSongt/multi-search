from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from multisearch.contracts import Role
from multisearch.errors import ApiError, install_errors
from multisearch.management_identity import router
from multisearch.sessions import SESSION_COOKIE, SessionIdentity


async def test_switch_requires_session_origin_and_csrf(monkeypatch):
    origin = "https://console.example.org"
    monkeypatch.setenv("MULTISEARCH_PUBLIC_CONSOLE_URL", origin)
    actor = SessionIdentity(
        uuid4(),
        "source",
        uuid4(),
        "user@example.org",
        Role.DEVELOPER,
        uuid4(),
        datetime.now(UTC) + timedelta(hours=2),
        "a" * 64,
    )
    switched = replace(
        actor,
        tenant_id=uuid4(),
        tenant="target",
        role=Role.TENANT_ADMIN,
        session_id=uuid4(),
        csrf_token="b" * 64,
    )
    calls = []

    class Sessions:
        async def authenticate(self, raw):
            assert raw == "old-cookie"
            return actor

        async def list_memberships(self, identity, limit, cursor):
            assert identity == actor and limit == 1 and cursor == "after"
            return [
                {
                    "tenant_id": switched.tenant_id,
                    "tenant": switched.tenant,
                    "role": switched.role,
                    "private_value": "not-returned",
                }
            ], None

        async def switch(self, identity, target):
            assert identity == actor
            calls.append(target)
            if target != switched.tenant_id:
                raise ApiError(404, "MEMBERSHIP_NOT_FOUND", "可切换的租户身份不存在")
            return "new-cookie", switched

    app = FastAPI()
    install_errors(app)
    app.include_router(router)
    app.state.identity = SimpleNamespace(sessions=Sessions())
    target = "/api/v1/auth/switch"
    memberships = "/api/v1/auth/memberships"
    body = {"tenant_id": str(switched.tenant_id)}
    bearer = {"Authorization": "Bearer machine-key"}
    async with AsyncClient(transport=ASGITransport(app), base_url=origin) as client:
        assert (await client.get(memberships, headers=bearer)).status_code == 401
        assert (await client.post(target, json=body, headers=bearer)).status_code == 401
        client.cookies.set(SESSION_COOKIE, "old-cookie")
        assert (await client.get(memberships, params={"cursor": "../"})).status_code == 422
        listed = await client.get(memberships, params={"limit": 1, "cursor": "after"})
        assert listed.status_code == 200 and listed.headers["cache-control"] == "no-store"
        assert listed.json() == {
            "memberships": [{**body, "tenant": "target", "role": "tenant_admin"}],
            "next_cursor": None,
        }
        headers = {"Origin": origin, "X-CSRF-Token": actor.csrf_token}
        assert (await client.post(target, json=body, headers={"Origin": origin})).status_code == 403
        assert (
            await client.post(target, json=body, headers={**headers, "Origin": "https://other.org"})
        ).status_code == 403
        assert (
            await client.post(target, json={**body, "role": "platform_admin"}, headers=headers)
        ).status_code == 422
        assert calls == []
        foreign = uuid4()
        denied = await client.post(target, json={"tenant_id": str(foreign)}, headers=headers)
        assert denied.status_code == 404 and denied.json()["code"] == "MEMBERSHIP_NOT_FOUND"
        assert "set-cookie" not in denied.headers
        response = await client.post(target, json=body, headers=headers)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert calls == [foreign, switched.tenant_id]
        assert response.json()["role"] == "tenant_admin"
        assert response.json()["csrf_token"] == switched.csrf_token
        assert response.json()["user_id"] == str(actor.user_id)
        assert f"{SESSION_COOKIE}=new-cookie" in response.headers["set-cookie"]
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "Secure" in response.headers["set-cookie"]
