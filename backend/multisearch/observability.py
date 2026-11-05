"""固定维度的进程指标与有界持久化快照。"""

import asyncio
import hmac
import json
import logging
import os
import re
from bisect import bisect_left
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from ipaddress import ip_address
from itertools import accumulate
from time import monotonic
from uuid import uuid4

from fastapi import FastAPI, Request
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .contracts import ImageSearchResult, Modality, Mode, ProviderRun, SearchResponse, Source
from .evidence_contracts import FetchStatus
from .settings import secret
from .tracing import bind_trace

LOGGER = logging.getLogger("uvicorn.error.multisearch.telemetry")
_MODES = frozenset(Mode)
_SOURCES = frozenset(Source)
_MODALITIES = frozenset(Modality)
_PROVIDERS = frozenset(
    {"ddgs", "searxng", "openalex", "crossref", "arxiv", "github", "local_index"}
)
_RESULT_STATUS = frozenset({"running", "completed", "partial", "failed", "cancelled"})
_PROVIDER_STATUS = frozenset(
    {"success", "empty", "partial", "failed", "timeout", "rate_limited", "circuit_open"}
)
_RIGHTS = frozenset({"unknown", "declared_unverified", "verified_license", "conflict"})
_SCOPES = frozenset({"search:text", "search:image", "fetch", "research"})
_JOB_MODES = frozenset({"fetch", "research"})
_ACTIVE = frozenset({"queued", "running", "cancelling"})
_CONTENT = frozenset({"text/html", "text/plain", "application/pdf", "application/xhtml+xml"})
_FETCH_STATUSES = frozenset(
    {"blocked", "cancelled", "completed", "failed", "ocr_pending", "timeout"}
)
_FETCH_COUNTER_KEY = "multisearch:metrics:fetch:v1"
_FETCH_FIELDS = {
    f"{content_type}:{status}": (content_type, status)
    for content_type in sorted(_CONTENT | {"unknown"})
    for status in sorted(_FETCH_STATUSES | {"unknown"})
}
_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.2, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0)
_SAMPLE_LIMIT = 4_000
_SNAPSHOT_TTL_S = 5.0

_FIXED_PATHS = {
    "/api/v1/search": "search",
    "/api/v1/playground": "playground",
    "/api/v1/fetch": "fetch",
    "/api/v1/research": "research",
    "/api/v1/capabilities": "capabilities",
    "/api/v1/providers": "providers",
    "/api/v1/me": "me",
    "/api/v1/usage": "usage",
    "/api/v1/auth/login": "auth_login",
    "/api/v1/auth/refresh": "auth_refresh",
    "/api/v1/auth/logout": "auth_logout",
    "/api/v1/auth/memberships": "auth_memberships",
    "/api/v1/auth/switch": "auth_switch",
    "/api/v1/auth/invitations/accept": "invite_accept",
    "/api/v1/keys": "keys",
    "/api/v1/admin/providers": "admin_providers",
    "/api/v1/admin/overview": "admin_overview",
    "/api/v1/admin/requests": "admin_requests",
    "/api/v1/admin/tenants": "admin_tenants",
    "/api/v1/admin/audit": "admin_audit",
    "/api/v1/admin/audit/risks": "admin_audit_risks",
    "/api/v1/admin/settings": "admin_settings",
    "/api/v1/admin/images": "admin_images",
    "/api/v1/admin/jobs": "admin_jobs",
    "/api/v1/admin/cache": "admin_cache",
    "/api/v1/admin/cache/invalidate": "cache_invalidate",
    "/api/v1/admin/routing": "admin_routing",
    "/api/v1/admin/routing/test": "routing_test",
    "/api/v1/admin/routing/publish": "routing_publish",
    "/api/v1/admin/routing/rollback": "routing_rollback",
    "/api/v1/team/members": "team_members",
    "/api/v1/team/invitations": "team_invitations",
    "/mcp": "mcp",
    "/health/live": "live",
    "/health/ready": "ready",
    "/health/dependencies": "dependency_health",
    "/metrics": "metrics",
}
_PATTERNS = tuple(
    (re.compile(pattern), route)
    for pattern, route in (
        (r"/api/v1/keys/[^/]+/rotate", "key_rotate"),
        (r"/api/v1/keys/[^/]+/revoke", "key_revoke"),
        (r"/api/v1/(?:admin/)?jobs/[^/]+/cancel", "job_cancel"),
        (r"/api/v1/(?:admin/)?jobs/[^/]+", "job"),
        (r"/api/v1/evidence/[^/]+", "evidence"),
        (r"/api/v1/admin/providers/[^/]+", "provider_update"),
        (r"/api/v1/admin/tenants/[^/]+/quota", "quota_update"),
        (r"/api/v1/admin/tenants/[^/]+", "tenant_update"),
        (r"/api/v1/(?:team|admin/tenants/[^/]+)/members/[^/]+", "member_update"),
        (r"/api/v1/(?:team|admin/tenants/[^/]+)/invitations/[^/]+/revoke", "invite_revoke"),
        (r"/api/v1/admin/tenants/[^/]+/invitations", "team_invitations"),
    )
)
_CONSOLE_ACTIONS = frozenset(
    {
        "auth_login",
        "auth_refresh",
        "auth_logout",
        "auth_memberships",
        "auth_switch",
        "invite_accept",
        "keys",
        "key_rotate",
        "key_revoke",
        "admin_tenants",
        "provider_update",
        "quota_update",
        "tenant_update",
        "member_update",
        "team_invitations",
        "invite_revoke",
        "cache_invalidate",
        "routing_publish",
        "routing_rollback",
    }
)


def _label(value, allowed: frozenset) -> str:
    return str(value) if isinstance(value, str) and value in allowed else "unknown"


def _route(path: str) -> str:
    fixed = _FIXED_PATHS.get(path)
    if fixed is not None:
        return fixed
    return next((route for pattern, route in _PATTERNS if pattern.fullmatch(path)), "other")


def _metric_line(name: str, labels: dict[str, str], value: int | float) -> str:
    # 所有标签来自固定枚举、已知路由或直方图桶。
    dimensions = ",".join(f'{key}="{label}"' for key, label in labels.items())
    return f"{name}{{{dimensions}}} {value}" if labels else f"{name} {value}"


@dataclass
class Histogram:
    counts: list[int] = field(default_factory=lambda: [0] * (len(_BUCKETS) + 1))
    seconds_sum: float = 0.0

    def observe(self, seconds: float) -> None:
        seconds = max(0.0, seconds)
        self.counts[bisect_left(_BUCKETS, seconds)] += 1
        self.seconds_sum += seconds

    def lines(self, name: str, labels: dict[str, str]) -> Iterable[str]:
        bounds = [*(str(bucket) for bucket in _BUCKETS), "+Inf"]
        for bound, value in zip(bounds, accumulate(self.counts), strict=True):
            yield _metric_line(f"{name}_bucket", {**labels, "le": bound}, value)
        yield _metric_line(f"{name}_count", labels, sum(self.counts))
        yield _metric_line(f"{name}_sum", labels, self.seconds_sum)


_COUNTERS = {
    "requests": (("mode", "status", "tenant_class"), "Search invocations in the API process."),
    "provider_requests": (("provider", "status"), "Provider runs observed by the API process."),
    "cache_hits": (
        ("mode", "source_type"),
        "Search responses served from cache, by requested source.",
    ),
    "image_results": (
        ("provider", "rights_status"),
        "Image results returned by provider and rights state.",
    ),
    "image_source_missing": (("provider",), "Image results without a landing page."),
    "api_key_auth": (
        ("status", "scope"),
        "API Key authentication outcomes; multi-scope keys are reported as multiple.",
    ),
    "quota_denials": (
        ("quota_type", "modality"),
        "Search rejections attributed to a quota or rate limit.",
    ),
    "console_actions": (("action", "status"), "Console actions by HTTP status class."),
    "http_requests": (
        ("route", "method", "status"),
        "API transport requests by fixed route and HTTP status class.",
    ),
}
_HISTOGRAMS = {
    "request_duration_seconds": (
        ("mode", "status"),
        "Search duration measured in the API process.",
    ),
    "provider_duration_seconds": (
        ("provider",),
        "Provider run duration measured in the API process.",
    ),
    "http_duration_seconds": (("route",), "HTTP transport response duration in the API process."),
}


class Telemetry:
    def __init__(self) -> None:
        self.counters: dict[str, Counter] = defaultdict(Counter)
        self.histograms: dict[str, dict[tuple[str, ...], Histogram]] = defaultdict(dict)
        self.evidence: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        self.snapshots: dict[str, tuple[float, list[str]]] = {}
        self.snapshot_locks: dict[str, asyncio.Lock] = {
            "postgres": asyncio.Lock(),
            "redis": asyncio.Lock(),
        }

    def _count(self, metric: str, *labels: str) -> None:
        self.counters[metric][labels] += 1

    def _duration(self, metric: str, labels: tuple[str, ...], seconds: float) -> None:
        histograms = self.histograms[metric]
        if labels not in histograms:
            histograms[labels] = Histogram()
        histograms[labels].observe(seconds)

    def _request(self, mode: str, status: str, elapsed_ms: float) -> None:
        self._count("requests", mode, status, "standard")
        self._duration("request_duration_seconds", (mode, status), elapsed_ms / 1000)

    def record_provider_runs(self, runs: Iterable[ProviderRun]) -> None:
        for run in runs:
            provider = _label(run.provider_id, _PROVIDERS)
            self._count("provider_requests", provider, _label(run.status, _PROVIDER_STATUS))
            self._duration("provider_duration_seconds", (provider,), run.elapsed_ms / 1000)

    def record_search(
        self,
        response: SearchResponse,
        *,
        replay: bool = False,
        source_types: Iterable[Source] = (),
    ) -> None:
        mode = _label(response.mode_executed, _MODES)
        status = _label(response.status, _RESULT_STATUS)
        self._request(mode, status, response.elapsed_ms)
        if replay:
            return
        if response.cache_hit:
            sources = {_label(source, _SOURCES) for source in source_types}
            for source_label in sources or {"unknown"}:
                self._count("cache_hits", mode, source_label)
        self.record_provider_runs(response.provider_runs)
        for result in response.results:
            if not isinstance(result, ImageSearchResult):
                continue
            for provider in {_label(p, _PROVIDERS) for p in result.provider_ids} or {"unknown"}:
                self._count("image_results", provider, _label(result.image.rights_status, _RIGHTS))
                if not result.image.source_page_url:
                    self._count("image_source_missing", provider)
        ratio = self.evidence[mode]
        ratio[0] += sum(bool(getattr(result, "evidence_refs", ())) for result in response.results)
        ratio[1] += len(response.results)

    def record_search_failure(
        self, mode: Mode, status: str, code: str, modalities: Iterable[Modality], elapsed_ms: float
    ) -> None:
        self._request(_label(mode, _MODES), _label(status, _RESULT_STATUS), elapsed_ms)
        quota_type = {"RATE_LIMITED": "rate_limit", "QUOTA_EXCEEDED": "quota"}.get(code)
        if quota_type:
            for modality in {_label(value, _MODALITIES) for value in modalities} or {"unknown"}:
                self._count("quota_denials", quota_type, modality)

    def record_auth(self, status: str, scopes: Iterable[str] = ()) -> None:
        known = {_label(scope, _SCOPES) for scope in scopes}
        scope = next(iter(known)) if len(known) == 1 else "multiple" if known else "unknown"
        self._count(
            "api_key_auth", _label(status, frozenset({"success", "failed", "unavailable"})), scope
        )

    def record_http(self, route: str, method: str, status: int, seconds: float) -> None:
        status_class = f"{status // 100}xx" if 100 <= status <= 599 else "unknown"
        if route == "metrics":
            return
        self._count("http_requests", route, method, status_class)
        self._duration("http_duration_seconds", (route,), seconds)
        if route in _CONSOLE_ACTIONS and method in {"POST", "PATCH", "DELETE"}:
            self._count("console_actions", route, status_class)

    def process_lines(self) -> list[str]:
        lines = []
        for name, (dimensions, help_text) in _COUNTERS.items():
            metric = f"multisearch_{name}_total"
            lines += _metadata(metric, "counter", help_text)
            for labels, count in sorted(self.counters[name].items()):
                lines.append(
                    _metric_line(metric, dict(zip(dimensions, labels, strict=True)), count)
                )
        for name, (dimensions, help_text) in _HISTOGRAMS.items():
            metric = f"multisearch_{name}"
            lines += _metadata(metric, "histogram", help_text)
            for labels, histogram in sorted(self.histograms[name].items()):
                lines.extend(histogram.lines(metric, dict(zip(dimensions, labels, strict=True))))
        metric = "multisearch_evidence_coverage_ratio"
        lines += _metadata(
            metric, "gauge", "Fraction of observed search results carrying an evidence reference."
        )
        lines += [
            _metric_line(metric, {"mode": mode}, covered / total)
            for mode, (covered, total) in sorted(self.evidence.items())
            if total
        ]
        return lines


class FetchMetrics:
    """Publish API and Worker extraction outcomes to one deployment-wide counter."""

    def __init__(self, redis: Redis) -> None:
        self.redis = redis
        self.pending: Counter[str] = Counter()
        self.wakeup = asyncio.Event()
        self.stopping = False
        self.available = True

    def record(self, content_type: str | None, status: FetchStatus) -> None:
        field = f"{_label(content_type, _CONTENT)}:{_label(status, _FETCH_STATUSES)}"
        self.pending[field] += 1
        self.wakeup.set()

    @classmethod
    @asynccontextmanager
    async def connect(cls, redis_url: str):
        # A lost response can follow a successful increment, so disable client retries.
        async with (
            Redis.from_url(redis_url, retry=Retry(NoBackoff(), 0)) as redis,
            cls(redis).publishing() as metrics,
        ):
            yield metrics

    async def _publish(self) -> None:
        while not self.stopping or self.pending:
            await self.wakeup.wait()
            if not self.stopping:
                await asyncio.sleep(0.25)
            self.wakeup.clear()
            if not self.pending:
                continue
            pending, self.pending = self.pending, Counter()
            try:
                async with asyncio.timeout(0.25), self.redis.pipeline() as commands:
                    for field, count in pending.items():
                        commands.hincrby(_FETCH_COUNTER_KEY, field, count)
                    await commands.execute()
            except (RedisError, OSError, TimeoutError):
                # A lost acknowledgement may follow a successful write. Do not replay.
                if self.available:
                    LOGGER.warning("fetch_metrics_write_unavailable")
                self.available = False
            else:
                self.available = True

    @asynccontextmanager
    async def publishing(self):
        publisher = asyncio.create_task(self._publish())
        try:
            yield self
        finally:
            self.stopping = True
            self.wakeup.set()
            await publisher


class _RedactedHTTP:
    def __init__(self, app: ASGIApp, telemetry: Telemetry) -> None:
        self.app = app
        self.telemetry = telemetry

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = monotonic()
        trace_id = uuid4().hex
        scope.setdefault("state", {})["trace_id"] = trace_id
        route = _route(scope["path"])
        method = _label(
            scope["method"], frozenset({"GET", "HEAD", "POST", "PATCH", "DELETE", "OPTIONS", "PUT"})
        )
        status = 500

        async def traced(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"x-trace-id"
                ] + [(b"x-trace-id", trace_id.encode("ascii"))]
            await send(message)

        try:
            with bind_trace(trace_id):
                await self.app(scope, receive, traced)
        finally:
            seconds = monotonic() - started
            self.telemetry.record_http(route, method, status, seconds)
            if route not in {"metrics", "live", "ready"}:
                LOGGER.info(
                    json.dumps(
                        {
                            "event": "http_request",
                            "trace_id": trace_id,
                            "route": route,
                            "method": method,
                            "status": status,
                            "elapsed_ms": round(seconds * 1000, 2),
                        },
                        separators=(",", ":"),
                    )
                )


def _metadata(metric: str, type_: str, help_text: str) -> list[str]:
    return [f"# HELP {metric} {help_text}", f"# TYPE {metric} {type_}"]


def _gauge(
    name: str, help_text: str, values: Iterable[tuple[dict[str, str], int | float]]
) -> list[str]:
    metric = f"multisearch_{name}"
    return [
        *_metadata(metric, "gauge", help_text),
        *(_metric_line(metric, labels, value) for labels, value in values),
    ]


_DB_METRICS = """
WITH usage AS MATERIALIZED (
    SELECT id, mode, operation, status, cache_hit
    FROM usage_events
    WHERE created_at > now() - interval '30 minutes'
    ORDER BY created_at DESC, id DESC LIMIT :cap
), providers AS MATERIALIZED (
    SELECT provider_id, status
    FROM provider_runs
    WHERE created_at > now() - interval '30 minutes'
    ORDER BY created_at DESC LIMIT :cap
), images AS MATERIALIZED (
    SELECT rights_status, source_page_url IS NULL AS missing
    FROM image_observations
    WHERE retrieved_at > now() - interval '30 minutes'
    ORDER BY retrieved_at DESC, id DESC LIMIT :cap
), active_jobs AS MATERIALIZED (
    SELECT kind, status, created_at
    FROM search_jobs WHERE status IN ('queued','running','cancelling')
    ORDER BY status, created_at, id LIMIT :cap
), fetches AS MATERIALIZED (
    SELECT j.status, j.result
    FROM usage u JOIN search_jobs j ON j.id = u.id
    WHERE u.operation = 'fetch' AND j.status NOT IN ('queued','running','cancelling')
), fetched_records AS (
    SELECT f.status, record->>'content_type' AS content_type
    FROM fetches f LEFT JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(f.result->'evidence_records') = 'array'
            THEN f.result->'evidence_records' ELSE '[]'::jsonb END
    ) record ON true
)
SELECT jsonb_build_object(
    'usage', COALESCE((SELECT jsonb_agg(g) FROM (
        SELECT mode, status, count(*) AS n
        FROM usage WHERE operation = 'search' GROUP BY mode, status
    ) g), '[]'::jsonb),
    'providers', COALESCE((SELECT jsonb_agg(g) FROM (
        SELECT provider_id, status, count(*) AS n FROM providers GROUP BY provider_id, status
    ) g), '[]'::jsonb),
    'images', COALESCE((SELECT jsonb_agg(g) FROM (
        SELECT rights_status, count(*) AS n, count(*) FILTER (WHERE missing) AS missing
        FROM images GROUP BY rights_status
    ) g), '[]'::jsonb),
    'jobs', COALESCE((SELECT jsonb_agg(g) FROM (
        SELECT kind, status, count(*) AS n,
            EXTRACT(epoch FROM now() - min(created_at)) AS oldest_seconds
        FROM active_jobs GROUP BY kind, status
    ) g), '[]'::jsonb),
    'fetches', COALESCE((SELECT jsonb_agg(g) FROM (
        SELECT content_type, status, count(*) AS n FROM fetched_records GROUP BY content_type, status
    ) g), '[]'::jsonb),
    'cache_hits', (SELECT count(*) FROM usage WHERE cache_hit AND operation = 'search'),
    'samples', jsonb_build_object(
        'usage', (SELECT count(*) FROM usage),
        'providers', (SELECT count(*) FROM providers),
        'images', (SELECT count(*) FROM images),
        'jobs', (SELECT count(*) FROM active_jobs)
    )
)
"""


def _grouped(rows: list[dict], dimensions: tuple[tuple[str, str, frozenset], ...]) -> list:
    grouped: Counter = Counter()
    for row in rows:
        labels = tuple(_label(row[field], allowed) for _, field, allowed in dimensions)
        grouped[labels] += int(row["n"])
    keys = [key for key, _, _ in dimensions]
    return [(dict(zip(keys, labels, strict=True)), n) for labels, n in sorted(grouped.items())]


async def _postgres_lines(request: Request) -> list[str]:
    try:
        async with asyncio.timeout(1.5), request.app.state.db.begin() as connection:
            await connection.execute(text("SET LOCAL statement_timeout = '1000ms'"))
            document = await connection.scalar(text(_DB_METRICS), {"cap": _SAMPLE_LIMIT})
    except (SQLAlchemyError, OSError, TimeoutError):
        return _dependency("postgres", False)
    lines = _dependency("postgres", True)
    lines += _gauge(
        "sample_capped",
        "Database snapshot reached its record cap (1) and may omit older records.",
        (
            ({"dataset": name}, int(count >= _SAMPLE_LIMIT))
            for name, count in sorted(document["samples"].items())
        ),
    )
    lines += _gauge(
        "stored_search_requests_window",
        "Stored search requests in the recent bounded 30-minute snapshot.",
        _grouped(
            document["usage"], (("mode", "mode", _MODES), ("status", "status", _RESULT_STATUS))
        ),
    )
    lines += _gauge(
        "stored_provider_runs_window",
        "Stored provider runs in the recent bounded 30-minute snapshot.",
        _grouped(
            document["providers"],
            (("provider", "provider_id", _PROVIDERS), ("status", "status", _PROVIDER_STATUS)),
        ),
    )
    lines += _gauge(
        "stored_cache_hits_window",
        "Stored cache hits in the recent bounded 30-minute snapshot.",
        [({}, document["cache_hits"])],
    )
    lines += _gauge(
        "stored_image_results_window",
        "Stored image results in the recent bounded 30-minute snapshot.",
        _grouped(document["images"], (("rights_status", "rights_status", _RIGHTS),)),
    )
    lines += _gauge(
        "stored_images_missing_source_window",
        "Stored images missing a landing page in the recent bounded snapshot.",
        [({}, sum(item["missing"] for item in document["images"]))],
    )
    lines += _gauge(
        "jobs_active",
        "Active jobs in the bounded durable queue snapshot.",
        _grouped(document["jobs"], (("mode", "kind", _JOB_MODES), ("status", "status", _ACTIVE))),
    )
    ages = {kind: 0.0 for kind in sorted(_JOB_MODES)}
    for job in document["jobs"]:
        if job["status"] == "queued" and job["kind"] in ages:
            ages[job["kind"]] = max(0.0, float(job["oldest_seconds"]))
    lines += _gauge(
        "oldest_queued_job_seconds",
        "Age of the oldest queued job in the durable snapshot.",
        (({"mode": kind}, seconds) for kind, seconds in ages.items()),
    )
    lines += _gauge(
        "stored_fetch_observations_window",
        "Stored fetch evidence records and terminal jobs without records in the bounded snapshot.",
        _grouped(
            document["fetches"],
            (("content_type", "content_type", _CONTENT), ("status", "status", _RESULT_STATUS)),
        ),
    )
    return lines


def _dependency(name: str, healthy: bool) -> list[str]:
    return [_metric_line("multisearch_metrics_dependency_up", {"dependency": name}, int(healthy))]


async def _redis_lines(request: Request) -> list[str]:
    try:
        async with asyncio.timeout(1.5):
            redis = request.app.state.redis
            async with redis.pipeline(transaction=False) as commands:
                commands.info("memory")
                for kind in sorted(_JOB_MODES):
                    commands.exists(f"multisearch:worker:{kind}")
                commands.hmget(_FETCH_COUNTER_KEY, list(_FETCH_FIELDS))
                memory, *workers, fetches = await commands.execute()
            used_bytes = int(memory["used_memory"])
            fetch_counts = [int(value) if value is not None else 0 for value in fetches]
    except (KeyError, RedisError, OSError, ValueError, TimeoutError):
        return _dependency("redis", False)
    lines = _dependency("redis", True)
    lines += _gauge(
        "redis_used_memory_bytes", "Redis used memory reported by INFO memory.", [({}, used_bytes)]
    )
    lines += _gauge(
        "worker_ready",
        "Worker heartbeat present in Redis (1).",
        (
            ({"mode": kind}, int(bool(ready)))
            for kind, ready in zip(sorted(_JOB_MODES), workers, strict=True)
        ),
    )
    metric = "multisearch_fetch_total"
    lines += _metadata(
        metric, "counter", "Evidence extraction attempts across the API and Workers."
    )
    lines.extend(
        _metric_line(metric, {"content_type": content_type, "status": status}, count)
        for (content_type, status), count in zip(_FETCH_FIELDS.values(), fetch_counts, strict=True)
        if count > 0
    )
    return lines


async def _cached_snapshot(
    telemetry: Telemetry, name: str, load: Callable[[], Awaitable[list[str]]]
) -> list[str]:
    async with telemetry.snapshot_locks[name]:
        now = monotonic()
        entry = telemetry.snapshots.get(name)
        if entry is not None and now - entry[0] < _SNAPSHOT_TTL_S:
            return entry[1]
        result = await load()
        telemetry.snapshots[name] = (monotonic(), result)
        return result


def _token_is_valid(token: str) -> bool:
    return (
        32 <= len(token) <= 256
        and token.isascii()
        and all(33 <= ord(char) <= 126 for char in token)
    )


def _can_scrape(request: Request, token: str, allow_loopback: bool) -> bool:
    authorizations = request.headers.getlist("authorization")
    if authorizations:
        return bool(
            token
            and len(authorizations) == 1
            and authorizations[0].isascii()
            and hmac.compare_digest(authorizations[0], f"Bearer {token}")
        )
    if (
        not allow_loopback
        or request.client is None
        or any(
            name in request.headers
            for name in ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip")
        )
    ):
        return False
    try:
        return ip_address(request.client.host).is_loopback
    except ValueError:
        return False


def install_observability(
    app: FastAPI, *, token: str | None = None, allow_loopback: bool | None = None
) -> Telemetry:
    """安装受保护的抓取端点，返回可用于业务计量的实例。"""
    token = secret("MULTISEARCH_METRICS_TOKEN") if token is None else token
    if token and not _token_is_valid(token):
        raise ValueError("MULTISEARCH_METRICS_TOKEN 需要 32–256 个可见 ASCII 字符")
    if allow_loopback is None:
        allow_loopback = os.getenv("MULTISEARCH_METRICS_ALLOW_LOOPBACK", "false").lower() == "true"
    telemetry = Telemetry()
    app.state.telemetry = telemetry
    app.add_middleware(_RedactedHTTP, telemetry=telemetry)

    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request) -> Response:
        if not _can_scrape(request, token, allow_loopback):
            return Response(
                status_code=401 if token else 404,
                headers={
                    "Cache-Control": "no-store",
                    **({"WWW-Authenticate": "Bearer"} if token else {}),
                },
            )
        postgres, redis = await asyncio.gather(
            _cached_snapshot(telemetry, "postgres", lambda: _postgres_lines(request)),
            _cached_snapshot(telemetry, "redis", lambda: _redis_lines(request)),
        )
        providers = request.app.state.search.provider_status()
        lines = telemetry.process_lines()
        lines += _gauge(
            "circuit_open",
            "Provider has an open circuit for at least one supported source and modality.",
            (
                (
                    {"provider": _label(item["provider_id"], _PROVIDERS)},
                    int(
                        item["status"] == "circuit_open"
                        or any(
                            pair["status"] == "circuit_open" for pair in item.get("pair_health", ())
                        )
                    ),
                )
                for item in providers
                if item["provider_id"] in _PROVIDERS
            ),
        )
        lines += _metadata(
            "multisearch_metrics_dependency_up", "gauge", "Dependency snapshot was collected (1)."
        )
        lines += [postgres[0], redis[0], *postgres[1:], *redis[1:]]
        return Response(
            "\n".join(lines) + "\n",
            headers={
                "Content-Type": "text/plain; version=0.0.4; charset=utf-8",
                "Cache-Control": "no-store",
            },
        )

    return telemetry
