from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from multisearch.admin_api import router as admin_router
from multisearch.contracts import Modality, Mode, Role, SearchRequest, Source
from multisearch.errors import ApiError, install_errors
from multisearch.ops_api import router
from multisearch.ops_store import OpsStore
from multisearch.routing_policy import (
    DEFAULT_ROUTING,
    RoutingRules,
    cache_epoch_signature,
    cache_ttl,
    resolve_mode,
    search_limits,
)
from multisearch.sessions import SESSION_COOKIE, SessionIdentity


@pytest.mark.parametrize(
    "payload",
    [
        {"budgets": {"quick": {"max_latency_ms": 4001, "max_results": 10}}},
        {"budgets": {"quick": {"max_latency_ms": 3999, "max_results": 10}}},
        {"budgets": {"quick": {"max_latency_ms": 4000, "max_results": 11}}},
        {"budgets": {"local": {"max_latency_ms": 2001, "max_results": 30}}},
        {"auto_quick_max_latency_ms": True},
        {"quick_fallback": 1},
        {"cache_ttl_seconds": {"negative": 121}},
        {"cache_ttl_seconds": {"academic": 86_401}},
    ],
)
def test_routing_policy_is_bounded(payload):
    with pytest.raises(ValidationError):
        RoutingRules.model_validate(payload)


def test_routing_defaults_and_policy_apply_to_preview():
    rules = RoutingRules.model_validate(
        {
            "budgets": {"quick": {"max_latency_ms": 1400, "max_results": 6}},
            "auto_quick_max_latency_ms": 1200,
            "cache_ttl_seconds": {"web": 250, "news": 10, "image": 100, "negative": 0},
        }
    )
    request = SearchRequest(
        query="demo", max_latency_ms=1100, source_types=[Source.WEB, Source.NEWS]
    )
    assert resolve_mode(request, rules) == Mode.QUICK
    assert resolve_mode(request.model_copy(update={"max_latency_ms": 1201}), rules) == Mode.STANDARD
    assert resolve_mode(request.model_copy(update={"mode": Mode.LOCAL}), rules) == Mode.LOCAL
    assert search_limits(rules, Mode.QUICK) == (1400, 6)
    assert cache_ttl(rules, request, empty=False) == 10
    assert cache_ttl(rules, request, empty=True) == 0


def test_cache_scope_epochs_invalidate_mixed_requests_without_touching_other_kinds():
    request = SearchRequest(
        query="demo",
        source_types=[Source.WEB, Source.NEWS],
        modalities=[Modality.TEXT, Modality.IMAGE],
    )
    base = cache_epoch_signature(request, {})
    assert base == (("all", 0), ("image", 0), ("news", 0), ("web", 0))
    for scope in ("all", "web", "news", "image"):
        assert cache_epoch_signature(request, {scope: 1}) != base
    assert cache_epoch_signature(request, {"code": 1, "local": 1, "academic": 1}) == base


def _actor(role: Role) -> SessionIdentity:
    return SessionIdentity(
        uuid4(),
        "test",
        uuid4(),
        "operator@example.org",
        role,
        uuid4(),
        datetime.now(UTC) + timedelta(hours=8),
        "a" * 64,
    )


def _app(actor: SessionIdentity, store) -> FastAPI:
    class Sessions:
        async def authenticate(self, raw):
            if raw != "session-only":
                raise ApiError(401, "AUTH_REQUIRED", "需要登录")
            return actor

    app = FastAPI()
    install_errors(app)
    app.state.identity = SimpleNamespace(sessions=Sessions())
    app.state.ops = store
    app.state.search = SimpleNamespace(routing=DEFAULT_ROUTING)
    app.include_router(router)
    return app


@pytest.mark.parametrize("role", list(Role))
async def test_request_trace_reuses_scoped_request_permissions(role):
    actor = _actor(role)
    usage_id, foreign_tenant = uuid4(), uuid4()
    trace_id = "a" * 32
    expected_tenant = None if role == Role.PLATFORM_ADMIN else actor.tenant_id

    class Reports:
        async def requests(self, scope, limit, cursor, **filters):
            assert scope.tenant == expected_tenant and scope.owner is None
            assert filters["trace_id"] == trace_id
            return {"items": [], "next_cursor": None}

        async def request_detail(self, scope, identifier):
            assert scope.tenant == expected_tenant and scope.owner is None
            assert identifier == usage_id
            return {"id": usage_id, "trace_id": trace_id, "provider_runs": []}

    app = _app(actor, None)
    app.state.reports = Reports()
    app.include_router(admin_router)
    path = "/api/v1/admin/requests"
    async with AsyncClient(transport=ASGITransport(app), base_url="http://127.0.0.1") as client:
        unauthenticated = await client.get(
            f"{path}/{usage_id}", headers={"Authorization": "Bearer search-key"}
        )
        assert unauthenticated.status_code == 401
        client.cookies.set(SESSION_COOKIE, "session-only")
        listed = await client.get(path, params={"trace_id": trace_id})
        detail = await client.get(f"{path}/{usage_id}")
        if role not in {Role.PLATFORM_ADMIN, Role.OPERATIONS_ADMIN}:
            assert listed.status_code == detail.status_code == 403
        else:
            assert listed.status_code == detail.status_code == 200
            assert detail.json() == {"id": str(usage_id), "trace_id": trace_id, "provider_runs": []}
        if role == Role.OPERATIONS_ADMIN:
            cross_tenant = await client.get(
                path, params={"trace_id": trace_id, "tenant_id": str(foreign_tenant)}
            )
            assert cross_tenant.status_code == 403


@pytest.mark.parametrize("role", list(Role))
async def test_provider_and_quota_reports_enforce_their_distinct_scopes(role, monkeypatch):
    origin = "http://127.0.0.1:3000"
    monkeypatch.setenv("MULTISEARCH_PUBLIC_CONSOLE_URL", origin)
    actor = _actor(role)
    tenant_id = uuid4()
    calls = []

    class Reports:
        async def providers(self, tenant):
            calls.append(("providers", tenant))
            return {"sample": {"runs": 3}}

        async def quota_usage(self, tenant, limit, cursor):
            calls.append(("quota_usage", tenant, limit, cursor))
            return {"keys": {"items": [{"key_mask": "msk_abcd…1234"}], "next_cursor": None}}

    app = _app(actor, None)
    app.state.reports = Reports()
    app.state.search.provider_status = lambda: [{"provider_id": "sample"}]
    app.include_router(admin_router)
    quota_path = f"/api/v1/admin/tenants/{tenant_id}/quota-usage"
    async with AsyncClient(transport=ASGITransport(app), base_url="http://127.0.0.1") as client:
        for path in (quota_path, "/api/v1/admin/providers"):
            assert (
                await client.get(path, headers={"Authorization": "Bearer search-key"})
            ).status_code == 401
        client.cookies.set(SESSION_COOKIE, "session-only")
        providers = await client.get("/api/v1/admin/providers")
        quota = await client.get(quota_path, params={"limit": 1, "cursor": "test-cursor"})
        if role == Role.PLATFORM_ADMIN:
            assert providers.status_code == quota.status_code == 200
            assert providers.json()["providers"][0]["usage"] == {"runs": 3}
            assert quota.json()["keys"]["items"] == [{"key_mask": "msk_abcd…1234"}]
            assert calls == [("providers", None), ("quota_usage", tenant_id, 1, "test-cursor")]
        elif role in {Role.OPERATIONS_ADMIN, Role.AUDITOR}:
            assert providers.status_code == 200 and quota.status_code == 403
            assert calls == [("providers", actor.tenant_id)]
            forbidden = await client.patch(
                "/api/v1/admin/providers/sample",
                json={"enabled": False},
                headers={"Origin": origin, "X-CSRF-Token": actor.csrf_token},
            )
            assert forbidden.status_code == 403
            assert forbidden.json()["message"] == "当前角色无权执行此操作"
        else:
            assert providers.status_code == quota.status_code == 403
            assert not calls


@pytest.mark.parametrize("role", [Role.DEVELOPER, Role.AUDITOR, Role.OPERATIONS_ADMIN])
async def test_team_permissions_are_enforced_before_queries(role, monkeypatch):
    monkeypatch.delenv("MULTISEARCH_PUBLIC_CONSOLE_URL", raising=False)
    actor = _actor(role)
    origin = "http://127.0.0.1:3000"
    app = _app(actor, OpsStore(None, None, None, "unit-only"))
    async with AsyncClient(transport=ASGITransport(app), base_url=origin) as client:
        assert (await client.get("/api/v1/team/members")).status_code == 401
        client.cookies.set(SESSION_COOKIE, "session-only")
        assert (await client.get("/api/v1/team/members")).status_code == 403
        assert (await client.get(f"/api/v1/admin/tenants/{uuid4()}/members")).status_code == 403


async def test_tenant_admin_cannot_grant_platform_roles_or_cross_tenants(monkeypatch):
    monkeypatch.delenv("MULTISEARCH_PUBLIC_CONSOLE_URL", raising=False)
    actor = _actor(Role.TENANT_ADMIN)
    origin = "http://127.0.0.1:3000"
    app = _app(actor, OpsStore(None, None, None, "unit-only"))
    async with AsyncClient(transport=ASGITransport(app), base_url=origin) as client:
        client.cookies.set(SESSION_COOKIE, "session-only")
        data = {"email": "member@example.org", "role": "platform_admin"}
        headers = {"Origin": origin, "X-CSRF-Token": actor.csrf_token}
        assert (
            await client.post("/api/v1/team/invitations", json=data, headers=headers)
        ).status_code == 403
        data["role"] = "developer"
        assert (
            await client.post(
                f"/api/v1/admin/tenants/{uuid4()}/invitations",
                json=data,
                headers=headers,
            )
        ).status_code == 403
        assert (
            await client.post("/api/v1/team/invitations", json=data, headers={"Origin": origin})
        ).status_code == 403


async def test_routing_preview_and_conflict_state_do_not_advance_without_publish(monkeypatch):
    monkeypatch.delenv("MULTISEARCH_PUBLIC_CONSOLE_URL", raising=False)
    actor = _actor(Role.PLATFORM_ADMIN)
    origin = "http://127.0.0.1:3000"

    class TestStore:
        async def active_routing(self):
            return DEFAULT_ROUTING

        async def publish_routing(
            self, identity, expected_version, reason, *, policy=None, rollback_of=None
        ):
            assert identity == actor and policy is not None and rollback_of is None
            if expected_version != 0:
                raise ApiError(409, "ROUTING_VERSION_CONFLICT", "路由版本已变更")
            return DEFAULT_ROUTING.model_copy(
                update={"version": 1, "policy": policy, "reason": reason}
            )

    app = _app(actor, TestStore())
    async with AsyncClient(transport=ASGITransport(app), base_url=origin) as client:
        client.cookies.set(SESSION_COOKIE, "session-only")
        headers = {"Origin": origin, "X-CSRF-Token": actor.csrf_token}
        policy = {
            "budgets": {"quick": {"max_latency_ms": 1800, "max_results": 4}},
            "auto_quick_max_latency_ms": 1700,
        }
        preview = await client.post(
            "/api/v1/admin/routing/test",
            headers=headers,
            json={"policy": policy, "request": {"max_latency_ms": 1600, "max_results": 5}},
        )
        assert preview.status_code == 200
        assert preview.json()["mode"] == "quick"
        assert preview.json()["max_latency_ms"] == 1600
        assert preview.json()["max_results"] == 4
        assert {"field": "budgets.quick.max_results", "before": 10, "after": 4} in preview.json()[
            "changes"
        ]
        assert app.state.search.routing.version == 0
        conflict = await client.post(
            "/api/v1/admin/routing/publish",
            headers=headers,
            json={
                "policy": policy,
                "expected_version": 1,
                "reason": "stale",
            },
        )
        assert conflict.status_code == 409
        assert app.state.search.routing.version == 0
        publish = await client.post(
            "/api/v1/admin/routing/publish",
            headers=headers,
            json={
                "policy": policy,
                "expected_version": 0,
                "reason": "new policy",
            },
        )
        assert publish.status_code == 200 and app.state.search.routing.version == 1


async def test_cache_scope_for_ops_and_explicit_confirmation(monkeypatch):
    monkeypatch.delenv("MULTISEARCH_PUBLIC_CONSOLE_URL", raising=False)
    actor = _actor(Role.OPERATIONS_ADMIN)
    origin = "http://127.0.0.1:3000"
    app = _app(actor, OpsStore(None, None, None, "unit-only"))
    async with AsyncClient(transport=ASGITransport(app), base_url=origin) as client:
        client.cookies.set(SESSION_COOKIE, "session-only")
        assert (
            await client.get("/api/v1/admin/cache", params={"tenant_id": str(uuid4())})
        ).status_code == 403
        headers = {"Origin": origin, "X-CSRF-Token": actor.csrf_token}
        for confirmation in (False, 1, "true"):
            response = await client.post(
                "/api/v1/admin/cache/invalidate",
                headers=headers,
                json={"type": "web", "confirm": confirmation},
            )
            assert response.status_code == 422
        assert (
            await client.post(
                "/api/v1/admin/cache/invalidate",
                headers=headers,
                json={"tenant_id": str(uuid4()), "type": "web", "confirm": True},
            )
        ).status_code == 403


async def test_cache_storage_scan_is_tenant_scoped_and_bounded():
    tenant_id = uuid4()
    namespace = f"multisearch:search:v1:{tenant_id}:"

    class Pipeline:
        def __init__(self):
            self.keys = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        def memory_usage(self, key, samples):
            assert key.startswith(namespace) and samples == 1
            self.keys.append(key)

        async def execute(self):
            assert len(self.keys) == 500
            return [11] * len(self.keys)

    class TestRedis:
        async def scan(self, cursor, match, count):
            assert cursor == 0 and match == namespace + "*" and count == 100
            return 0, [namespace + str(index) for index in range(501)]

        def pipeline(self, transaction):
            assert not transaction
            return Pipeline()

    store = OpsStore(None, TestRedis(), None, "unit-only")
    assert await store._cache_storage(tenant_id) == {
        "keys": 500,
        "bytes": 5500,
        "complete": False,
        "available": True,
    }
