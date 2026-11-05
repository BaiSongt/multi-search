from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from multisearch.contracts import Role
from multisearch.errors import ApiError, install_errors
from multisearch.management_identity import router
from multisearch.sessions import SESSION_COOKIE, SessionIdentity


async def test_session_management_requires_cookie_origin_and_csrf(monkeypatch):
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
    management_id = uuid4()
    revoked = []

    class Sessions:
        async def authenticate(self, raw):
            assert raw == "test-session-cookie"
            return actor

        async def list_sessions(self, identity, limit, cursor):
            assert identity == actor and limit == 1 and cursor is None
            return [
                {
                    "id": management_id,
                    "created_at": now,
                    "expires_at": actor.expires_at,
                    "current": False,
                    "secret_hash": "not-exposed",
                }
            ], None

        async def revoke_session(self, identity, session_id):
            assert identity == actor
            if session_id != management_id:
                raise ApiError(404, "SESSION_NOT_FOUND", "会话不存在")
            revoked.append(session_id)

    app = FastAPI()
    install_errors(app)
    app.include_router(router)
    app.state.identity = SimpleNamespace(sessions=Sessions())
    async with AsyncClient(transport=ASGITransport(app), base_url=origin) as client:
        path = "/api/v1/auth/sessions"
        target = f"{path}/{management_id}/revoke"
        bearer = {"Authorization": "Bearer machine-key"}
        assert (await client.get(path, headers=bearer)).status_code == 401
        assert (await client.post(target, headers=bearer)).status_code == 401

        client.cookies.set(SESSION_COOKIE, "test-session-cookie")
        reply = await client.get(path, params={"limit": 1})
        assert reply.status_code == 200 and reply.headers["cache-control"] == "no-store"
        listed = reply.json()
        assert listed["next_cursor"] is None
        (entry,) = listed["sessions"]
        assert set(entry) == {"id", "created_at", "expires_at", "current"}
        assert entry["id"] == str(management_id) and entry["current"] is False
        assert (await client.get(path, params={"limit": 51})).status_code == 422
        assert (await client.get(path, params={"cursor": "invalid"})).status_code == 422

        headers = {"Origin": origin, "X-CSRF-Token": actor.csrf_token}
        assert (await client.post(target, headers={"Origin": origin})).status_code == 403
        assert (
            await client.post(target, headers={**headers, "Origin": f"{origin}.evil.org"})
        ).status_code == 403
        missing = await client.post(f"{path}/{uuid4()}/revoke", headers=headers)
        assert missing.status_code == 404 and missing.json()["code"] == "SESSION_NOT_FOUND"
        reply = await client.post(target, headers=headers)
        assert reply.status_code == 204 and not reply.content
        assert "set-cookie" not in reply.headers
        assert revoked == [management_id]
