import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from multisearch.contracts import Role
from multisearch.errors import install_errors
from multisearch.ops_api import router
from multisearch.query_retention import QUERY_RETENTION_UPDATED
from multisearch.sessions import SESSION_COOKIE, SessionIdentity

_PATH = "/api/v1/admin/settings/query-retention"
_ORIGIN = "http://127.0.0.1:3000"


def _fixture(role: Role):
    now = datetime.now(UTC)
    actor = SessionIdentity(
        uuid4(),
        "test",
        uuid4(),
        "operator@example.invalid",
        role,
        uuid4(),
        now + timedelta(hours=1),
        "a" * 64,
    )
    other = uuid4()
    policies = {tenant: (0, 0, None) for tenant in (actor.tenant_id, other)}
    audits = []
    calls = []

    class Sessions:
        async def authenticate(self, raw):
            assert raw == "query-retention-fixture"
            return actor

        async def assert_actor(self, connection, session):
            assert session == actor
            calls.append("recheck")

    class Database:
        @asynccontextmanager
        async def connect(self):
            yield self

        @asynccontextmanager
        async def begin(self):
            yield self

        async def execute(self, sql, params):
            query = str(sql)
            tenant = params["tenant"]
            if "pg_advisory_xact_lock" in query:
                calls.append(("lock", tenant))
                return
            if "INSERT INTO tenant_query_retention" in query:
                calls.append("write")
                policies[tenant] = (params["hours"], params["version"], now)
                return
            if "INSERT INTO audit_events" in query:
                calls.append("audit")
                audits.append({**params, "details": json.loads(params["details"])})
                return
            assert "FROM tenants t LEFT JOIN tenant_query_retention" in query
            calls.append(("read", tenant))
            if tenant not in policies:
                row = None
            else:
                hours, version, updated_at = policies[tenant]
                row = {
                    "tenant_id": tenant,
                    "retention_hours": hours,
                    "version": version,
                    "updated_at": updated_at,
                    "changes": [
                        {**entry["details"], "occurred_at": now}
                        for entry in reversed(audits)
                        if entry["tenant"] == tenant
                    ][:10],
                }
            return SimpleNamespace(mappings=lambda: SimpleNamespace(one_or_none=lambda: row))

    app = FastAPI()
    install_errors(app)
    app.include_router(router)
    app.state.db = Database()
    app.state.identity = SimpleNamespace(sessions=Sessions())
    return app, actor, other, audits, calls


@pytest.mark.parametrize("role", list(Role))
async def test_query_retention_read_and_write_permissions(role, monkeypatch):
    monkeypatch.setenv("MULTISEARCH_PUBLIC_CONSOLE_URL", _ORIGIN)
    app, actor, other, audits, calls = _fixture(role)
    headers = {"Origin": _ORIGIN, "X-CSRF-Token": actor.csrf_token}
    async with AsyncClient(transport=ASGITransport(app), base_url="http://127.0.0.1") as client:
        machine = await client.get(_PATH, headers={"Authorization": "Bearer test-machine-key"})
        assert machine.status_code == 401
        client.cookies.set(SESSION_COOKIE, "query-retention-fixture")
        own = await client.get(_PATH)
        foreign = await client.get(_PATH, params={"tenant_id": str(other)})
        if role in {Role.PLATFORM_ADMIN, Role.OPERATIONS_ADMIN}:
            assert own.status_code == 200
            assert own.json() == {
                "tenant_id": str(actor.tenant_id),
                "retention_hours": 0,
                "version": 0,
                "updated_at": None,
                "changes": [],
            }
        else:
            assert own.status_code == 403
        assert foreign.status_code == (200 if role == Role.PLATFORM_ADMIN else 403)
        if role != Role.PLATFORM_ADMIN:
            denied = await client.patch(
                _PATH,
                headers=headers,
                json={
                    "tenant_id": str(actor.tenant_id),
                    "retention_hours": 2,
                    "expected_version": 0,
                    "confirm": True,
                },
            )
            assert denied.status_code == 403
    assert not audits
    assert all(isinstance(call, tuple) and call[0] == "read" for call in calls)


async def test_query_retention_confirm_compare_and_set_audits_only_policy_fields(monkeypatch):
    monkeypatch.setenv("MULTISEARCH_PUBLIC_CONSOLE_URL", _ORIGIN)
    app, actor, other, audits, calls = _fixture(Role.PLATFORM_ADMIN)
    body = {
        "tenant_id": str(other),
        "retention_hours": 24,
        "expected_version": 0,
        "confirm": True,
    }
    headers = {"Origin": _ORIGIN, "X-CSRF-Token": actor.csrf_token}
    async with AsyncClient(transport=ASGITransport(app), base_url="http://127.0.0.1") as client:
        client.cookies.set(SESSION_COOKIE, "query-retention-fixture")
        assert (
            await client.patch(_PATH, json=body, headers={"Origin": _ORIGIN})
        ).status_code == 403
        assert (
            await client.patch(_PATH, json=body, headers={**headers, "Origin": "http://evil.test"})
        ).status_code == 403
        for invalid in (
            {"confirm": False},
            {"confirm": 1},
            {"retention_hours": True},
            {"retention_hours": "24"},
            {"retention_hours": 25},
            {"expected_version": -1},
            {"query": "unexpected"},
        ):
            rejected = await client.patch(_PATH, json={**body, **invalid}, headers=headers)
            assert rejected.status_code == 422
        assert not calls and not audits

        changed = await client.patch(_PATH, json=body, headers=headers)
        assert changed.status_code == 200
        assert calls == [
            ("lock", other),
            "recheck",
            ("read", other),
            "write",
            "audit",
            ("read", other),
        ]
        policy = changed.json()
        assert policy["tenant_id"] == str(other)
        assert policy["retention_hours"] == 24 and policy["version"] == 1 and policy["updated_at"]
        assert set(policy["changes"][0]) == {
            "version",
            "before_hours",
            "after_hours",
            "occurred_at",
        }
        assert audits == [
            {
                "tenant": other,
                "actor": actor.user_id,
                "action": QUERY_RETENTION_UPDATED,
                "target": other,
                "actor_type": "session",
                "details": {"version": 1, "before_hours": 0, "after_hours": 24},
            }
        ]

        stale = await client.patch(_PATH, json={**body, "retention_hours": 0}, headers=headers)
        assert stale.status_code == 409 and stale.json()["code"] == "SETTINGS_VERSION_CONFLICT"
        same = await client.patch(_PATH, json={**body, "expected_version": 1}, headers=headers)
        assert same.status_code == 200 and same.json() == policy
        assert len(audits) == 1
        own = await client.get(_PATH)
        assert own.status_code == 200 and own.json()["retention_hours"] == 0
        missing = await client.get(_PATH, params={"tenant_id": str(uuid4())})
        assert missing.status_code == 404 and missing.json()["code"] == "TENANT_NOT_FOUND"
