import base64
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .contracts import Mode
from .errors import ApiError
from .metering import ACTIVE_EXECUTION
from .policy import effective_quota, tenant_quota

_SCOPE = """
    (CAST(:tenant AS UUID) IS NULL OR tenant_id = :tenant)
    AND (CAST(:owner AS UUID) IS NULL OR user_id = :owner)
"""
_TOTALS = """
    COUNT(*) FILTER (WHERE operation = 'search') AS search_requests,
    COUNT(*) FILTER (WHERE operation = 'fetch') AS fetch_requests,
    COUNT(*) FILTER (WHERE status IN ('failed', 'cancelled') OR
        (status = 'running' AND reservation_expires_at <= now())) AS errors,
    COALESCE(SUM(text_results_returned), 0) AS text_results_returned,
    COALESCE(SUM(image_results_returned), 0) AS image_results_returned
"""
_RESOLVED_STATUS = """
    CASE WHEN status = 'running' AND reservation_expires_at <= now() THEN 'failed'
        ELSE status END
"""
_RESOLVED_CODE = """
    CASE WHEN status = 'running' AND reservation_expires_at <= now()
         THEN 'REQUEST_INTERRUPTED' ELSE code END
"""
_REQUEST_COLUMNS = f"""
    id, request_id, trace_id, tenant_id, key_id, operation, mode, modalities, source_types,
    {_RESOLVED_STATUS} AS status,
    {_RESOLVED_CODE} AS code,
    elapsed_ms, cache_hit, text_results_returned, image_results_returned, created_at
"""
_REPORTABLE_FAILURE_CODES = (
    "CACHE_UNAVAILABLE",
    "EVIDENCE_UNAVAILABLE",
    "FETCH_BODY_TOO_LARGE",
    "FETCH_CONTENT_UNSUPPORTED",
    "FETCH_NO_EXCERPT",
    "FETCH_NOT_FOUND",
    "FETCH_PDF_ENCRYPTED",
    "FETCH_PDF_INVALID",
    "FETCH_PDF_LIMIT",
    "FETCH_PDF_UNAVAILABLE",
    "FETCH_RATE_LIMITED",
    "FETCH_REDIRECT_INVALID",
    "FETCH_REDIRECT_LIMIT",
    "FETCH_RENDER_POLICY_UNSUPPORTED",
    "FETCH_RENDER_UNAVAILABLE",
    "FETCH_RESOURCE_CORS_DENIED",
    "FETCH_RESOURCE_POLICY_UNSUPPORTED",
    "FETCH_RESOURCE_REDIRECT",
    "FETCH_ROBOTS_DENIED",
    "FETCH_ROBOTS_UNAVAILABLE",
    "FETCH_SITE_RESTRICTED",
    "FETCH_TARGET_BLOCKED",
    "FETCH_UNAVAILABLE",
    "INTERNAL_ERROR",
    "INVALID_REQUEST",
    "JOB_FAILED",
    "JOB_PRINCIPAL_INACTIVE",
    "MODE_UNAVAILABLE",
    "NO_RESEARCH_RESULTS",
    "PROVIDER_UNAVAILABLE",
    "RATE_LIMITED",
    "RATE_LIMITER_UNAVAILABLE",
    "REQUEST_INTERRUPTED",
    "SOURCE_MODALITY_UNAVAILABLE",
    "SOURCE_UNAVAILABLE",
    "TIME_BUDGET_EXCEEDED",
    "WORKER_INTERRUPTED",
)
_QUOTA_METRICS = {
    "requests": "COUNT(*) FILTER (WHERE created_at >= :day)",
    "search_requests": "COUNT(*) FILTER (WHERE created_at >= :day AND operation = 'search')",
    "image_requests": "COUNT(*) FILTER (WHERE created_at >= :day AND 'image' = ANY(modalities))",
    "image_results_returned": "COALESCE(SUM(image_results_returned) FILTER (WHERE created_at >= :day), 0)",
    "fetch_requests": "COUNT(*) FILTER (WHERE created_at >= :day AND operation = 'fetch')",
    "pages_fetched": "COALESCE(SUM(pages_fetched) FILTER (WHERE created_at >= :day), 0)",
    "active_executions": f"COUNT(*) FILTER (WHERE {ACTIVE_EXECUTION})",
}
_QUOTA_METRIC_SELECT = ", ".join(
    f"{expression} AS {name}" for name, expression in _QUOTA_METRICS.items()
)
_IMAGE_SOURCE_STATUS = """
    CASE WHEN i.source_page_url IS NULL THEN 'missing'
         WHEN i.metadata ->> 'source_page_status' = 'invalid' THEN 'invalid'
         WHEN i.source_page_verified THEN 'verified'
         WHEN i.metadata ->> 'source_verification' = 'failed' THEN 'failed'
         WHEN i.metadata ->> 'source_page_status' = 'checked'
              OR i.metadata ->> 'source_verification' = 'not_matched' THEN 'not_matched'
         ELSE 'unverified' END
"""


@dataclass(frozen=True)
class ReportScope:
    tenant: UUID | None
    owner: UUID | None = None

    def params(self) -> dict:
        return {"tenant": self.tenant, "owner": self.owner}


def _read_cursor(cursor: str | None) -> tuple[datetime | None, UUID | None]:
    if cursor is None:
        return None, None
    try:
        time, identifier = (
            base64.b64decode(cursor, altchars=b"-_", validate=True).decode().split("|")
        )
        timestamp = datetime.fromisoformat(time)
        if timestamp.tzinfo is None:
            raise ValueError
        return timestamp, UUID(identifier)
    except (UnicodeError, ValueError):
        raise ApiError(422, "INVALID_REQUEST", "无效分页游标") from None


def _cursor(row: dict) -> str:
    material = f"{row['created_at'].isoformat()}|{row['id']}"
    return base64.urlsafe_b64encode(material.encode()).decode()


class UsageReports:
    def __init__(self, db: AsyncEngine, max_concurrent: int):
        self.db = db
        self.max_concurrent = max_concurrent

    async def _totals(self, connection: AsyncConnection, scope: ReportScope) -> dict:
        results = (
            (
                await connection.execute(
                    text(f"""
            SELECT {_TOTALS}, COALESCE(SUM(pages_fetched), 0) AS pages_fetched,
                COALESCE(SUM(research_jobs_started), 0) AS research_jobs_started,
                COALESCE(SUM(llm_tokens), 0) AS llm_tokens,
                COALESCE(SUM(llm_tokens_reserved), 0) AS llm_tokens_reserved,
                COUNT(*) FILTER (WHERE llm_usage_unknown) AS llm_usage_unknown_requests
            FROM usage_events WHERE {_SCOPE} AND created_at >= :start
        """),
                    {**scope.params(), "start": _start()},
                )
            )
            .mappings()
            .one()
        )
        return dict(results)

    async def _series(self, connection: AsyncConnection, scope: ReportScope) -> list[dict]:
        result = await connection.execute(
            text(f"""
            SELECT (created_at AT TIME ZONE 'UTC')::DATE AS day, {_TOTALS}
            FROM usage_events WHERE {_SCOPE} AND created_at >= :start
            GROUP BY day ORDER BY day
        """),
            {**scope.params(), "start": _start()},
        )
        return [dict(row) for row in result.mappings()]

    async def _breakdown(self, connection: AsyncConnection, scope: ReportScope) -> dict:
        result = await connection.execute(
            text(f"""
            WITH observed AS (
                SELECT mode, {_RESOLVED_STATUS} AS status, {_RESOLVED_CODE} AS code
                FROM usage_events WHERE {_SCOPE} AND created_at >= :start
            ), classified AS (
                SELECT mode, CASE WHEN status = 'failed' THEN CASE
                    WHEN code = ANY(CAST(:failure_codes AS TEXT[])) THEN code ELSE 'OTHER_ERROR'
                    END END AS failure
                FROM observed
            )
            SELECT mode, failure, COUNT(*) AS count FROM classified
            GROUP BY GROUPING SETS ((mode), (failure))
            HAVING mode = ANY(CAST(:modes AS TEXT[])) OR failure IS NOT NULL
            ORDER BY count DESC, mode ASC, failure ASC
            """),
            {
                **scope.params(),
                "start": _start(),
                "modes": [mode.value for mode in Mode],
                "failure_codes": list(_REPORTABLE_FAILURE_CODES),
            },
        )
        counts = result.mappings().all()
        return {
            "mode_counts": [
                {"mode": row["mode"], "count": row["count"]} for row in counts if row["mode"]
            ],
            "failure_counts": [
                {"code": row["failure"], "count": row["count"]} for row in counts if row["failure"]
            ],
        }

    async def _requests(
        self,
        connection: AsyncConnection,
        scope: ReportScope,
        limit: int,
        cursor: str | None = None,
        **filters,
    ) -> dict:
        after, identifier = _read_cursor(cursor)
        params = {
            "status": None,
            "mode": None,
            "modality": None,
            "source_type": None,
            "trace_id": None,
            "since": None,
            "until": None,
            **filters,
            **scope.params(),
            "after": after,
            "id": identifier,
            "limit": limit + 1,
        }
        result = await connection.execute(
            text(f"""
            SELECT {_REQUEST_COLUMNS} FROM usage_events WHERE {_SCOPE}
                AND (CAST(:status AS TEXT) IS NULL OR ({_RESOLVED_STATUS}) = :status)
                AND (CAST(:mode AS TEXT) IS NULL OR mode = :mode)
                AND (CAST(:modality AS TEXT) IS NULL OR :modality = ANY(modalities))
                AND (CAST(:source_type AS TEXT) IS NULL OR :source_type = ANY(source_types))
                AND (CAST(:trace_id AS TEXT) IS NULL OR trace_id = :trace_id)
                AND (CAST(:since AS TIMESTAMPTZ) IS NULL OR created_at >= :since)
                AND (CAST(:until AS TIMESTAMPTZ) IS NULL OR created_at < :until)
                AND (CAST(:after AS TIMESTAMPTZ) IS NULL OR (created_at, id) < (:after, :id))
            ORDER BY created_at DESC, id DESC LIMIT :limit
        """),
            params,
        )
        items = [dict(row) for row in result.mappings()]
        return {
            "items": items[:limit],
            "next_cursor": _cursor(items[limit - 1]) if len(items) > limit else None,
        }

    async def requests(self, scope: ReportScope, limit: int, cursor: str | None, **filters) -> dict:
        async with self.db.connect() as connection:
            return await self._requests(connection, scope, limit, cursor, **filters)

    async def request_detail(self, scope: ReportScope, usage_id: UUID) -> dict:
        async with self.db.connect() as connection:
            result = await connection.execute(
                text(f"""
                    SELECT {_REQUEST_COLUMNS}, COALESCE((
                        SELECT jsonb_agg(jsonb_build_object(
                            'provider_id', p.provider_id, 'status', p.status, 'elapsed_ms', p.elapsed_ms,
                            'count', p.count, 'reason_codes', p.reason_codes, 'attempts', p.attempts
                        ) ORDER BY p.ordinal)
                        FROM provider_runs p WHERE p.usage_id = usage_events.id
                    ), '[]'::JSONB) AS provider_runs
                    FROM usage_events WHERE {_SCOPE} AND id = :id
                """),
                {**scope.params(), "id": usage_id},
            )
            row = result.mappings().one_or_none()
            if row is None:
                raise ApiError(404, "REQUEST_NOT_FOUND", "请求记录不存在")
            return dict(row)

    async def usage(self, scope: ReportScope) -> dict:
        async with self.db.connect() as connection:
            return {
                "totals": await self._totals(connection, scope),
                "series": await self._series(connection, scope),
                **(await self._breakdown(connection, scope)),
                "quota": await tenant_quota(connection, scope.tenant, self.max_concurrent),
                "recent": (await self._requests(connection, scope, 20))["items"],
                "window_days": 30,
            }

    async def overview(self, scope: ReportScope) -> dict:
        async with self.db.connect() as connection:
            params = {**scope.params(), "start": _start()}
            details = (
                (
                    await connection.execute(
                        text(f"""
                SELECT COUNT(*) FILTER (WHERE created_at >= now() - interval '1 minute') / 60.0
                        AS requests_per_second,
                    COUNT(*) FILTER (WHERE status IN ('completed', 'partial'))::FLOAT
                        / NULLIF(COUNT(*) FILTER (WHERE ({_RESOLVED_STATUS}) <> 'running'), 0)
                        AS success_rate,
                    percentile_cont(0.5) WITHIN GROUP (ORDER BY elapsed_ms) AS p50_ms,
                    percentile_cont(0.95) WITHIN GROUP (ORDER BY elapsed_ms) AS p95_ms,
                    SUM(image_sources_present)::FLOAT / NULLIF(SUM(image_results_returned), 0)
                        AS image_source_coverage
                FROM usage_events WHERE {_SCOPE} AND created_at >= :start
            """),
                        params,
                    )
                )
                .mappings()
                .one()
            )
            jobs = await connection.execute(
                text("""
                SELECT count(*) AS active_jobs,
                    count(*) FILTER (WHERE status = 'queued') AS queued,
                    count(*) FILTER (WHERE status = 'running') AS running,
                    count(*) FILTER (WHERE status = 'cancelling') AS cancelling,
                    count(*) FILTER (WHERE status = 'queued' AND attempts > 0) AS queued_for_retry,
                    min(updated_at) FILTER (WHERE status = 'queued') AS oldest_queued_at
                FROM search_jobs WHERE (CAST(:tenant AS UUID) IS NULL OR tenant_id = :tenant)
                    AND status IN ('queued','running','cancelling') AND deadline_at > now()
            """),
                {"tenant": scope.tenant},
            )
            job_queue = dict(jobs.mappings().one())
            active_jobs = job_queue.pop("active_jobs")
            return {
                "totals": await self._totals(connection, scope),
                "series": await self._series(connection, scope),
                **dict(details),
                "requests_per_second": float(details["requests_per_second"]),
                "active_jobs": active_jobs,
                "job_queue": job_queue,
            }

    async def providers(self, tenant_id: UUID | None) -> dict[str, dict]:
        async with self.db.connect() as connection:
            result = await connection.execute(
                text("""
                SELECT p.provider_id, COUNT(*) AS runs,
                    COUNT(*) FILTER (WHERE p.status NOT IN ('success','empty')) AS errors,
                    COUNT(*) FILTER (WHERE p.status = 'rate_limited') AS rate_limited,
                    percentile_cont(0.5) WITHIN GROUP (ORDER BY p.elapsed_ms) AS p50_ms,
                    percentile_cont(0.95) WITHIN GROUP (ORDER BY p.elapsed_ms) AS p95_ms
                FROM provider_runs p JOIN usage_events u ON p.usage_id = u.id
                WHERE p.created_at >= :start AND (CAST(:tenant AS UUID) IS NULL OR u.tenant_id = :tenant)
                GROUP BY p.provider_id
            """),
                {"start": _start(), "tenant": tenant_id},
            )
            return {row["provider_id"]: dict(row) for row in result.mappings()}

    async def quota_usage(self, tenant_id: UUID, limit: int, cursor: str | None) -> dict:
        now = datetime.now(UTC)
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        after, identifier = _read_cursor(cursor)
        async with self.db.connect() as connection:
            result = await connection.execute(
                text(f"""
                WITH key_page AS MATERIALIZED (
                    SELECT id, prefix, suffix, limits, created_at, fingerprint AS cursor_id
                    FROM api_keys
                    CROSS JOIN LATERAL (SELECT CAST(left(encode(sha256(uuid_send(id)), 'hex'), 32) AS UUID) AS fingerprint) masked
                    WHERE tenant_id = :tenant
                        AND (CAST(:after AS TIMESTAMPTZ) IS NULL OR (created_at, fingerprint) < (:after, :id))
                    ORDER BY created_at DESC, fingerprint DESC LIMIT :limit
                ), observed AS MATERIALIZED (
                    SELECT GROUPING(key_id) AS all_keys, key_id, {_QUOTA_METRIC_SELECT}
                    FROM usage_events WHERE tenant_id = :tenant AND (created_at >= :day OR status = 'running')
                    GROUP BY GROUPING SETS ((), (key_id))
                )
                SELECT COALESCE(q.quota, '{{}}'::JSONB) AS quota,
                    (SELECT to_jsonb(o) - 'all_keys' - 'key_id' FROM observed o WHERE o.all_keys = 1) AS usage,
                    COALESCE((SELECT jsonb_agg(jsonb_build_object(
                        'key_mask', k.prefix || '…' || k.suffix,
                        'created_at', k.created_at, 'cursor_id', k.cursor_id, 'limits', k.limits,
                        'usage', to_jsonb(o) - 'all_keys' - 'key_id'
                    ) ORDER BY k.created_at DESC, k.cursor_id DESC)
                    FROM key_page k LEFT JOIN observed o ON o.all_keys = 0 AND o.key_id = k.id), '[]'::JSONB) AS keys
                FROM tenants t LEFT JOIN quota_policies q ON q.tenant_id = t.id
                WHERE t.id = :tenant
                """),
                {
                    "tenant": tenant_id,
                    "day": day,
                    "now": now,
                    "after": after,
                    "id": identifier,
                    "limit": limit + 1,
                },
            )
            row = result.mappings().one_or_none()
            if row is None:
                raise ApiError(404, "TENANT_NOT_FOUND", "租户不存在")

        quota = effective_quota(row["quota"], self.max_concurrent)
        keys = row["keys"]
        boundary = keys[limit - 1] if len(keys) > limit else None
        return {
            "day": day,
            "observed_at": now,
            "quota": quota,
            "usage": row["usage"],
            "keys": {
                "items": [
                    {
                        "key_mask": key["key_mask"],
                        "usage": key["usage"] or dict.fromkeys(_QUOTA_METRICS, 0),
                        "quota": {
                            name: min(cap, key["limits"].get(name, cap))
                            for name, cap in quota.items()
                        },
                    }
                    for key in keys[:limit]
                ],
                "next_cursor": _cursor(
                    {
                        "created_at": datetime.fromisoformat(boundary["created_at"]),
                        "id": boundary["cursor_id"],
                    }
                )
                if boundary
                else None,
            },
        }

    async def images(
        self,
        tenant_id: UUID | None,
        rights_status: str | None,
        limit: int,
        cursor: str | None,
        *,
        source_status: str | None = None,
    ) -> dict:
        after, identifier = _read_cursor(cursor)
        params = {"tenant": tenant_id, "start": _start()}
        async with self.db.connect() as connection:
            counts = (
                (
                    await connection.execute(
                        text(f"""
                SELECT i.rights_status, {_IMAGE_SOURCE_STATUS} AS source_status, count(*) AS count
                FROM image_observations i
                WHERE (CAST(:tenant AS UUID) IS NULL OR i.tenant_id = :tenant)
                    AND i.retrieved_at >= :start
                GROUP BY i.rights_status, source_status
            """),
                        params,
                    )
                )
                .mappings()
                .all()
            )
            rows = (
                (
                    await connection.execute(
                        text(f"""
                SELECT i.id, u.request_id, i.tenant_id, i.source_page_url, i.original_url, i.thumbnail_url,
                    i.rights_status, i.source_page_verified, i.retrieved_at,
                    COALESCE(i.metadata ->> 'source_page_status',
                        CASE WHEN i.source_page_verified THEN 'checked' ELSE 'unverified' END) AS source_page_status,
                    COALESCE((i.metadata ->> 'source_checked_at')::TIMESTAMPTZ, i.rights_checked_at) AS last_check_at,
                    source.source_status
                FROM image_observations i JOIN usage_events u ON u.id = i.usage_id
                CROSS JOIN LATERAL (SELECT {_IMAGE_SOURCE_STATUS} AS source_status) AS source
                WHERE (CAST(:tenant AS UUID) IS NULL OR i.tenant_id = :tenant) AND i.retrieved_at >= :start
                    AND (CAST(:rights AS TEXT) IS NULL OR i.rights_status = :rights)
                    AND (CAST(:source AS TEXT) IS NULL OR source.source_status = :source)
                    AND (CAST(:after AS TIMESTAMPTZ) IS NULL OR (i.retrieved_at, i.id) < (:after, :id))
                ORDER BY i.retrieved_at DESC, i.id DESC LIMIT :limit
            """),
                        {
                            **params,
                            "rights": rights_status,
                            "source": source_status,
                            "after": after,
                            "id": identifier,
                            "limit": limit + 1,
                        },
                    )
                )
                .mappings()
                .all()
            )
        rights_counts: Counter[str] = Counter()
        source_counts: Counter[str] = Counter()
        for row in counts:
            rights_counts[row["rights_status"]] += row["count"]
            source_counts[row["source_status"]] += row["count"]
        boundary = rows[limit - 1] if len(rows) > limit else None
        return {
            "counts": dict(rights_counts),
            "source_counts": dict(source_counts),
            "items": [dict(row) for row in rows[:limit]],
            "next_cursor": _cursor({"created_at": boundary["retrieved_at"], "id": boundary["id"]})
            if boundary
            else None,
        }


def _start() -> datetime:
    return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=29)
