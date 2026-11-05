from asyncio import Lock, gather, timeout
from datetime import UTC, datetime
from time import monotonic
from typing import Literal, TypedDict

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from .dynamic_renderer import HEARTBEAT_KEY
from .job_contracts import JobKind
from .settings import Settings

_PROBE_TIMEOUT_S = 1.5
_SNAPSHOT_TTL_S = 2.0

type ServiceStatus = Literal["healthy", "unavailable", "unknown", "not_configured"]
type ServiceName = Literal[
    "api",
    "postgresql",
    "redis",
    "fetch_worker",
    "research_worker",
    "dynamic_fetch_worker",
    "searxng",
    "crawl4ai",
]


class DependencySnapshot(TypedDict):
    checked_at: datetime
    checks: dict[ServiceName, ServiceStatus]


class DependencyHealth:
    """采样固定依赖，仅输出状态枚举，合并并发请求。"""

    def __init__(
        self, db: AsyncEngine, redis: Redis, client: httpx.AsyncClient, settings: Settings
    ) -> None:
        self.db = db
        self.redis = redis
        self.client = client
        self.settings = settings
        self._lock = Lock()
        self._cached: DependencySnapshot | None = None
        self._expires_at = 0.0

    async def snapshot(self) -> DependencySnapshot:
        async with self._lock:
            if self._cached is not None and monotonic() < self._expires_at:
                return self._cached
            database, redis, searxng, parser = await gather(
                self._postgresql(),
                self._redis(),
                self._http(self.settings.searxng_url, "/healthz"),
                self._http(self.settings.crawl4ai_url, "/health/ready"),
            )
            self._cached = {
                "checked_at": datetime.now(UTC),
                "checks": {
                    "api": "healthy",
                    "postgresql": database,
                    **redis,
                    "searxng": searxng,
                    "crawl4ai": parser,
                },
            }
            self._expires_at = monotonic() + _SNAPSHOT_TTL_S
            return self._cached

    async def _postgresql(self) -> ServiceStatus:
        try:
            async with timeout(_PROBE_TIMEOUT_S), self.db.connect() as connection:
                await connection.execute(text("SELECT id FROM usage_events LIMIT 0"))
        except TimeoutError:
            return "unknown"
        except (SQLAlchemyError, OSError):
            return "unavailable"
        return "healthy"

    async def _redis(self) -> dict[ServiceName, ServiceStatus]:
        enabled_workers: dict[ServiceName, str] = {
            "fetch_worker": f"multisearch:worker:{JobKind.FETCH.value}"
        }
        checks: dict[ServiceName, ServiceStatus] = {
            "redis": "unknown",
            "fetch_worker": "unknown",
            "research_worker": "not_configured",
            "dynamic_fetch_worker": "not_configured",
        }
        if self.settings.enable_research:
            enabled_workers["research_worker"] = f"multisearch:worker:{JobKind.RESEARCH.value}"
            checks["research_worker"] = "unknown"
        if self.settings.dynamic_fetch_domains:
            enabled_workers["dynamic_fetch_worker"] = HEARTBEAT_KEY
            checks["dynamic_fetch_worker"] = "unknown"
        try:
            async with (
                timeout(_PROBE_TIMEOUT_S),
                self.redis.pipeline(transaction=False) as commands,
            ):
                commands.ping()
                for key in enabled_workers.values():
                    commands.exists(key)
                ping, *workers = await commands.execute()
        except TimeoutError:
            return checks
        except (RedisError, OSError):
            checks["redis"] = "unavailable"
            return checks
        if not ping:
            checks["redis"] = "unavailable"
            return checks
        checks["redis"] = "healthy"
        for worker, ready in zip(enabled_workers, workers, strict=True):
            if ready:
                checks[worker] = "healthy"
            elif worker != "fetch_worker":
                checks[worker] = "unavailable"
        return checks

    async def _http(self, base_url: str, path: str) -> ServiceStatus:
        if not base_url:
            return "not_configured"
        try:
            async with (
                timeout(_PROBE_TIMEOUT_S),
                self.client.stream(
                    "GET", f"{base_url.rstrip('/')}{path}", follow_redirects=False, timeout=None
                ) as response,
            ):
                return "healthy" if response.status_code == 200 else "unavailable"
        except TimeoutError:
            return "unknown"
        except (httpx.HTTPError, httpx.InvalidURL):
            return "unavailable"
