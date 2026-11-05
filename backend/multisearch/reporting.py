import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .errors import ApiError
from .policy import tenant_quota

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
                COALESCE(SUM(llm_tokens), 0) AS llm_tokens
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
            SELECT id, request_id, tenant_id, key_id, operation, mode, modalities, source_types,
                {_RESOLVED_STATUS} AS status,
                CASE WHEN status = 'running' AND reservation_expires_at <= now()
                     THEN 'REQUEST_INTERRUPTED' ELSE code END AS code,
                elapsed_ms, cache_hit, text_results_returned, image_results_returned, created_at
            FROM usage_events WHERE {_SCOPE}
                AND (CAST(:status AS TEXT) IS NULL OR ({_RESOLVED_STATUS}) = :status)
                AND (CAST(:mode AS TEXT) IS NULL OR mode = :mode)
                AND (CAST(:modality AS TEXT) IS NULL OR :modality = ANY(modalities))
                AND (CAST(:source_type AS TEXT) IS NULL OR :source_type = ANY(source_types))
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

    async def usage(self, scope: ReportScope) -> dict:
        async with self.db.connect() as connection:
            return {
                "totals": await self._totals(connection, scope),
                "series": await self._series(connection, scope),
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
            active_jobs = await connection.scalar(
                text("""
                SELECT count(*) FROM search_jobs WHERE (CAST(:tenant AS UUID) IS NULL OR tenant_id = :tenant)
                    AND status IN ('queued','running','cancelling') AND deadline_at > now()
            """),
                {"tenant": scope.tenant},
            )
            return {
                "totals": await self._totals(connection, scope),
                "series": await self._series(connection, scope),
                **dict(details),
                "requests_per_second": float(details["requests_per_second"]),
                "active_jobs": active_jobs,
            }

    async def providers(self) -> dict[str, dict]:
        async with self.db.connect() as connection:
            result = await connection.execute(
                text("""
                SELECT provider_id, COUNT(*) AS runs,
                    COUNT(*) FILTER (WHERE status NOT IN ('success','empty')) AS errors,
                    COUNT(*) FILTER (WHERE status = 'rate_limited') AS rate_limited,
                    percentile_cont(0.5) WITHIN GROUP (ORDER BY elapsed_ms) AS p50_ms,
                    percentile_cont(0.95) WITHIN GROUP (ORDER BY elapsed_ms) AS p95_ms
                FROM provider_runs WHERE created_at >= :start GROUP BY provider_id
            """),
                {"start": _start()},
            )
            return {row["provider_id"]: dict(row) for row in result.mappings()}

    async def images(
        self,
        tenant_id: UUID | None,
        rights_status: str | None,
        limit: int,
        cursor: str | None,
    ) -> dict:
        after, identifier = _read_cursor(cursor)
        params = {"tenant": tenant_id, "start": _start()}
        async with self.db.connect() as connection:
            counts = (
                (
                    await connection.execute(
                        text("""
                SELECT rights_status, count(*) AS count FROM image_observations
                WHERE (CAST(:tenant AS UUID) IS NULL OR tenant_id = :tenant) AND retrieved_at >= :start
                GROUP BY rights_status
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
                        text("""
                SELECT i.id, u.request_id, i.tenant_id, i.source_page_url, i.original_url, i.thumbnail_url,
                    i.rights_status, i.source_page_verified, i.retrieved_at,
                    COALESCE(i.metadata ->> 'source_page_status',
                        CASE WHEN i.source_page_verified THEN 'checked' ELSE 'unverified' END) AS source_page_status,
                    COALESCE((i.metadata ->> 'source_checked_at')::TIMESTAMPTZ, i.rights_checked_at) AS last_check_at
                FROM image_observations i JOIN usage_events u ON u.id = i.usage_id
                WHERE (CAST(:tenant AS UUID) IS NULL OR i.tenant_id = :tenant) AND i.retrieved_at >= :start
                    AND (CAST(:rights AS TEXT) IS NULL OR i.rights_status = :rights)
                    AND (CAST(:after AS TIMESTAMPTZ) IS NULL OR (i.retrieved_at, i.id) < (:after, :id))
                ORDER BY i.retrieved_at DESC, i.id DESC LIMIT :limit
            """),
                        {
                            **params,
                            "rights": rights_status,
                            "after": after,
                            "id": identifier,
                            "limit": limit + 1,
                        },
                    )
                )
                .mappings()
                .all()
            )
        boundary = rows[limit - 1] if len(rows) > limit else None
        return {
            "counts": {row["rights_status"]: row["count"] for row in counts},
            "items": [dict(row) for row in rows[:limit]],
            "next_cursor": _cursor({"created_at": boundary["retrieved_at"], "id": boundary["id"]})
            if boundary
            else None,
        }


def _start() -> datetime:
    return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=29)
