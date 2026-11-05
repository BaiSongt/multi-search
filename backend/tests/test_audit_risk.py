from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from multisearch.admin_api import router
from multisearch.contracts import Role
from multisearch.errors import install_errors
from multisearch.sessions import SESSION_COOKIE, SessionIdentity


@pytest.mark.parametrize("role", list(Role))
async def test_audit_risk_scope_and_minimal_response(role):
    now = datetime.now(UTC)
    actor = SessionIdentity(
        uuid4(),
        "audit",
        uuid4(),
        "auditor@example.invalid",
        role,
        uuid4(),
        now + timedelta(hours=1),
        "a" * 64,
    )
    foreign = uuid4()
    calls = []
    row = {
        "event_id": str(2**53 + 1),
        "tenant_id": actor.tenant_id,
        "occurred_at": now,
        "code": "privileged_access_change",
        "details": {"private": "must not appear"},
    }

    class Sessions:
        async def authenticate(self, raw):
            assert raw == "audit-fixture"
            return actor

    class Database:
        @asynccontextmanager
        async def connect(self):
            yield self

        async def execute(self, _, params):
            calls.append(params)
            rows = [
                {**row, "event_id": str(int(row["event_id"]) - offset)}
                for offset in range(params["limit"])
            ]
            return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: rows))

    app = FastAPI()
    install_errors(app)
    app.include_router(router)
    app.state.identity = SimpleNamespace(sessions=Sessions())
    app.state.db = Database()
    path = "/api/v1/admin/audit/risks"
    async with AsyncClient(transport=ASGITransport(app), base_url="http://127.0.0.1") as client:
        assert (
            await client.get(path, headers={"Authorization": "Bearer fixture"})
        ).status_code == 401
        client.cookies.set(SESSION_COOKIE, "audit-fixture")
        response = await client.get(path)
        other = await client.get(path, params={"tenant_id": str(foreign)})
    if role == Role.DEVELOPER:
        assert response.status_code == other.status_code == 403
        assert not calls
        return
    assert response.status_code == 200
    data = response.json()
    assert data["window_days"] == 7 and data["truncated"] is True
    assert len(data["alerts"]) == 20
    assert set(data["alerts"][0]) == {"event_id", "tenant_id", "occurred_at", "code"}
    assert data["alerts"][0]["event_id"] == row["event_id"]
    assert calls[0] == {
        "tenant": None if role == Role.PLATFORM_ADMIN else actor.tenant_id,
        "days": 7,
        "limit": 21,
    }
    if role == Role.PLATFORM_ADMIN:
        assert other.status_code == 200 and calls[1]["tenant"] == foreign
    else:
        assert other.status_code == 403 and len(calls) == 1
