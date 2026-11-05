import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime

import httpx
from redis.exceptions import ConnectionError as RedisConnectionError

from multisearch import health
from multisearch.dynamic_renderer import HEARTBEAT_KEY
from multisearch.main import create_app
from multisearch.settings import Settings


class Database:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls = 0

    @asynccontextmanager
    async def connect(self):
        self.calls += 1
        if self.error:
            raise self.error
        yield self

    async def execute(self, query):
        assert str(query) == "SELECT id FROM usage_events LIMIT 0"


class Redis:
    def __init__(self, workers=(), *, error: Exception | None = None):
        self.workers = set(workers)
        self.error = error
        self.keys = []

    @asynccontextmanager
    async def pipeline(self, *, transaction: bool):
        assert not transaction
        self.keys.clear()
        yield self

    def ping(self):
        pass

    def exists(self, key):
        self.keys.append(key)

    async def execute(self):
        if self.error:
            raise self.error
        return [True, *(key in self.workers for key in self.keys)]


SETTINGS = Settings(
    database_url="postgresql+asyncpg://fake:private-secret@private-db/db",
    redis_url="redis://private-cache",
    key_pepper="private-pepper",
    searxng_url="http://searxng:8080",
)


async def test_health_reports_fixed_signals_and_merges_concurrent_requests():
    database = Database()
    redis = Redis(workers={"multisearch:worker:fetch", HEARTBEAT_KEY})
    settings = replace(
        SETTINGS,
        enable_research=True,
        dynamic_fetch_domains=frozenset({"example.org"}),
        crawl4ai_url="http://crawl4ai:11235",
    )
    urls = []

    async def respond(request):
        urls.append(str(request.url))
        return httpx.Response(200 if request.url.path == "/healthz" else 503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        probe = health.DependencyHealth(database, redis, client, settings)
        first, second = await asyncio.gather(probe.snapshot(), probe.snapshot())
        assert first == second
        assert database.calls == 1
        assert first["checks"] == {
            "api": "healthy",
            "postgresql": "healthy",
            "redis": "healthy",
            "fetch_worker": "healthy",
            "research_worker": "unavailable",
            "dynamic_fetch_worker": "healthy",
            "searxng": "healthy",
            "crawl4ai": "unavailable",
        }
    assert sorted(urls) == ["http://crawl4ai:11235/health/ready", "http://searxng:8080/healthz"]


async def test_public_health_survives_database_and_redis_errors_and_redacts_details():
    database = Database(OSError("private-db: private-secret"))
    redis = Redis(error=RedisConnectionError("private-cache: private-secret"))
    settings = replace(SETTINGS, enable_research=True)
    app = create_app(settings)
    visited = []

    def redirect(request):
        visited.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://private-cache"})

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(redirect), follow_redirects=True) as client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as browser,
    ):
        app.state.dependency_health = health.DependencyHealth(database, redis, client, settings)
        response = await browser.get("/health/dependencies")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert "private" not in response.text
        payload = response.json()
        assert set(payload) == {"checked_at", "checks"}
        assert datetime.fromisoformat(payload["checked_at"]).utcoffset().total_seconds() == 0
        assert payload["checks"] == {
            "api": "healthy",
            "postgresql": "unavailable",
            "redis": "unavailable",
            "fetch_worker": "unknown",
            "research_worker": "unknown",
            "dynamic_fetch_worker": "not_configured",
            "searxng": "unavailable",
            "crawl4ai": "not_configured",
        }
    assert visited == ["http://searxng:8080/healthz"]


async def test_timeout_marks_probe_unknown_and_sampling_can_recover(monkeypatch):
    monkeypatch.setattr(health, "_PROBE_TIMEOUT_S", 0.01)
    monkeypatch.setattr(health, "_SNAPSHOT_TTL_S", 0)
    delayed = True

    async def respond(request):
        if delayed:
            await asyncio.sleep(10)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        probe = health.DependencyHealth(Database(), Redis(), client, SETTINGS)
        first = await probe.snapshot()
        assert first["checks"]["searxng"] == "unknown"
        assert first["checks"]["redis"] == "healthy"
        assert first["checks"]["fetch_worker"] == "unknown"
        delayed = False
        assert (await probe.snapshot())["checks"]["searxng"] == "healthy"
