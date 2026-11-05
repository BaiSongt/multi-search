import json
import logging
from asyncio import CancelledError, Lock, create_task, shield, sleep
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .contracts import Modality, Mode, ProviderRun, SearchRequest, SearchResponse
from .errors import ApiError
from .evidence_store import save_search_metadata
from .policy import effective_quota, lock_tenant
from .query_capture import CAPTURE_ADMITTED_QUERY, maintain_retained_queries, retainable_query
from .tracing import trace_id

logger = logging.getLogger(__name__)
ACTIVE_EXECUTION = """
    status = 'running' AND (
        (operation = 'search' AND reservation_expires_at > :now)
        OR (operation IN ('fetch','research') AND EXISTS (
            SELECT 1 FROM search_jobs j WHERE j.id = usage_events.id
                AND j.status IN ('running','cancelling') AND j.lease_expires_at > :now
        ))
    )
"""
_INSERT_PROVIDER_RUNS = """
    INSERT INTO provider_runs (usage_id, ordinal, provider_id, status, elapsed_ms, count, reason_codes)
    SELECT usage.id, run.ordinal, run.provider_id, run.status, run.elapsed_ms, run.count, run.reason_codes
    FROM {source} AS usage
    CROSS JOIN jsonb_to_recordset(CAST(:runs AS JSONB)) AS run(
        ordinal INTEGER, provider_id TEXT, status TEXT, elapsed_ms INTEGER, count INTEGER,
        reason_codes TEXT[]
    )
"""


@dataclass(frozen=True)
class SearchPrincipal:
    tenant_id: UUID
    user_id: UUID
    principal_id: UUID
    kind: Literal["key", "session"]
    limits: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Admission:
    event_id: UUID
    idempotent: bool
    replay: dict | None = None


@dataclass
class _AdmissionGate:
    lock: Lock = field(default_factory=Lock)
    users: int = 0


def _request_hash(body: SearchRequest) -> str:
    payload = body.model_dump(mode="json", exclude={"request_id", "idempotency_key"})
    return sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _windows(now: datetime) -> dict[str, tuple[datetime, datetime]]:
    minute = now.replace(second=0, microsecond=0)
    day = minute.replace(hour=0, minute=0)
    month = day.replace(day=1)
    return {
        "minute": (minute, minute + timedelta(minutes=1)),
        "day": (day, day + timedelta(days=1)),
        "month": (month, (month + timedelta(days=32)).replace(day=1)),
    }


def _quota_checks(
    modalities: list[Modality], mode: Mode, now: datetime, *, include_concurrency: bool
) -> dict:
    windows = _windows(now)
    checks = {
        f"requests_per_{unit}": (f"created_at >= :{unit}", end)
        for unit, (_, end) in windows.items()
    }
    if include_concurrency:
        checks["concurrency"] = (ACTIVE_EXECUTION, None)
    for modality in modalities:
        if modality in {"text", "image"}:
            checks[f"{modality}_per_day"] = (
                f"created_at >= :day AND '{modality}' = ANY(modalities)",
                windows["day"][1],
            )
    checks[f"{mode}_per_day"] = ("created_at >= :day AND mode = :mode", windows["day"][1])
    return checks


async def assert_principal(connection: AsyncConnection, principal: SearchPrincipal) -> datetime:
    params = {
        "tenant": principal.tenant_id,
        "user_id": principal.user_id,
        "key_id": principal.principal_id if principal.kind == "key" else None,
    }
    if principal.kind == "key":
        active = """
            SELECT k.id FROM api_keys k
            JOIN tenants t ON t.id = k.tenant_id
            JOIN users u ON u.id = k.owner_id
            JOIN memberships m ON (m.tenant_id, m.user_id) = (k.tenant_id, k.owner_id)
            WHERE k.tenant_id = :tenant AND k.id = :key_id AND k.owner_id = :user_id
              AND k.revoked_at IS NULL
              AND coalesce(k.rotation_expires_at, k.expires_at) > statement_timestamp()
              AND t.enabled AND u.enabled AND m.enabled
        """
    else:
        active = """
            SELECT m.user_id FROM memberships m
            JOIN tenants t ON t.id = m.tenant_id JOIN users u ON u.id = m.user_id
            WHERE m.tenant_id = :tenant AND m.user_id = :user_id AND m.enabled
              AND t.enabled AND u.enabled
        """
    row = (
        await connection.execute(
            text(f"SELECT statement_timestamp() AS now, EXISTS ({active}) AS active"), params
        )
    ).one()
    if not row.active:
        if principal.kind == "key":
            raise ApiError(401, "AUTH_REQUIRED", "需要有效的 API Key")
        raise ApiError(403, "FORBIDDEN", "租户或账户已停用")
    return row.now.astimezone(UTC)


async def enforce_quota(
    connection: AsyncConnection,
    principal: SearchPrincipal,
    now: datetime,
    mode: Mode,
    modalities: list[Modality],
    max_concurrent: int,
    *,
    include_concurrency: bool = True,
) -> None:
    params = {
        "tenant": principal.tenant_id,
        "user_id": principal.user_id,
        "key_id": principal.principal_id if principal.kind == "key" else None,
        "now": now,
    }
    params["mode"] = mode.value
    checks = _quota_checks(modalities, mode, now, include_concurrency=include_concurrency)
    columns = []
    for index, (condition, _) in enumerate(checks.values()):
        columns.append(f"COUNT(*) FILTER (WHERE {condition}) AS t{index}")
        if principal.limits:
            columns.append(f"COUNT(*) FILTER (WHERE {condition} AND key_id = :key_id) AS k{index}")
    # 所有 SQL 条件来自固定窗口和已验证枚举，查询内容只作为绑定参数。
    window_starts = {name: start for name, (start, _) in _windows(now).items()}
    counts = (
        (
            await connection.execute(
                text(f"""
        SELECT (SELECT quota FROM quota_policies WHERE tenant_id = :tenant) AS overrides,
            {", ".join(columns)} FROM usage_events
        WHERE tenant_id = :tenant AND (
            created_at >= :month OR (status = 'running' AND reservation_expires_at > :now)
        )
    """),
                {**params, **window_starts},
            )
        )
        .mappings()
        .one()
    )
    quota = effective_quota(counts["overrides"] or {}, max_concurrent)
    for index, (name, (_, until)) in enumerate(checks.items()):
        key_limit = principal.limits.get(name)
        if counts[f"t{index}"] >= quota[name] or (
            key_limit is not None and counts[f"k{index}"] >= key_limit
        ):
            retry_ms = max(1, int((until - now).total_seconds() * 1000)) if until else 1000
            code = (
                "RATE_LIMITED"
                if name in {"requests_per_minute", "concurrency"}
                else "QUOTA_EXCEEDED"
            )
            raise ApiError(
                429,
                code,
                f"已达到配额限制：{name}",
                retryable=True,
                retry_after_ms=retry_ms,
            )


async def save_provider_runs(
    connection: AsyncConnection,
    usage_id: UUID,
    runs: list[ProviderRun],
) -> None:
    if not runs:
        return
    await connection.execute(
        text(_INSERT_PROVIDER_RUNS.format(source="(SELECT CAST(:usage_id AS UUID) AS id)")),
        {"usage_id": usage_id, "runs": _provider_runs_json(runs)},
    )


def _provider_runs_json(runs: list[ProviderRun]) -> str:
    return json.dumps(
        [
            {
                "ordinal": index,
                "reason_codes": [reason.value for reason in run.reason_codes],
                **run.model_dump(include={"provider_id", "status", "elapsed_ms", "count"}),
            }
            for index, run in enumerate(runs)
        ]
    )


async def has_concurrency_slot(
    connection: AsyncConnection,
    principal: SearchPrincipal,
    now: datetime,
    max_concurrent: int,
) -> bool:
    key_limit = principal.limits.get("concurrency")
    result = (
        (
            await connection.execute(
                text(f"""
        SELECT (SELECT quota FROM quota_policies WHERE tenant_id = :tenant) AS overrides,
            count(*) AS tenant_count,
            count(*) FILTER (WHERE key_id = CAST(:key_id AS UUID)) AS key_count
        FROM usage_events WHERE tenant_id = :tenant AND ({ACTIVE_EXECUTION})
    """),
                {
                    "tenant": principal.tenant_id,
                    "now": now,
                    "key_id": principal.principal_id if principal.kind == "key" else None,
                },
            )
        )
        .mappings()
        .one()
    )
    quota = effective_quota(result["overrides"] or {}, max_concurrent)
    return result["tenant_count"] < quota["concurrency"] and (
        key_limit is None or result["key_count"] < key_limit
    )


class UsageMeter:
    def __init__(self, db: AsyncEngine, max_concurrent: int):
        self.db = db
        self.max_concurrent = max_concurrent
        self._admission_gates: dict[UUID, _AdmissionGate] = {}

    @asynccontextmanager
    async def _tenant_admission(self, tenant_id: UUID) -> AsyncIterator[AsyncConnection]:
        gate = self._admission_gates.setdefault(tenant_id, _AdmissionGate())
        gate.users += 1
        try:
            # 本进程的同租户等待者先排队，再占用数据库连接。
            async with gate.lock, self.db.begin() as connection:
                await lock_tenant(connection, tenant_id)
                yield connection
        finally:
            gate.users -= 1
            if not gate.users:
                del self._admission_gates[tenant_id]

    async def purge_expired_replays(self) -> None:
        async with self.db.begin() as connection:
            await connection.execute(
                text("""
                UPDATE usage_events SET idempotency_key = NULL, idempotency_until = NULL,
                    request_hash = NULL, replay_payload = NULL
                WHERE idempotency_until <= now()
            """)
            )

    async def _maintain(self) -> None:
        while True:
            try:
                await self.purge_expired_replays()
            except (SQLAlchemyError, OSError):
                logger.warning("暂时无法清理过期的幂等响应")
            await sleep(300)

    @asynccontextmanager
    async def maintenance(self):
        tasks = (create_task(self._maintain()), create_task(maintain_retained_queries(self.db)))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(CancelledError):
                    await task

    async def reserve(
        self,
        principal: SearchPrincipal,
        body: SearchRequest,
        mode: Mode,
        request_id: str,
        max_latency_ms: int,
    ) -> Admission:
        idem = body.idempotency_key
        fingerprint = _request_hash(body) if idem else None
        params = {
            "tenant": principal.tenant_id,
            "principal": principal.principal_id,
            "user_id": principal.user_id,
            "kind": principal.kind,
            "idempotency_key": idem,
            "key_id": principal.principal_id if principal.kind == "key" else None,
            "hash": fingerprint,
            "request_id": request_id,
            "trace_id": trace_id(),
            "mode": mode.value,
            "modalities": [value.value for value in body.modalities],
            "source_types": [value.value for value in body.source_types],
        }
        async with self._tenant_admission(principal.tenant_id) as connection:
            now = await assert_principal(connection, principal)
            params["now"] = now
            if idem is not None:
                await connection.execute(
                    text("""
                    UPDATE usage_events SET idempotency_key = NULL, idempotency_until = NULL,
                        request_hash = NULL, replay_payload = NULL
                    WHERE tenant_id = :tenant AND principal_kind = :kind
                      AND principal_id = :principal AND idempotency_key = :idempotency_key
                      AND idempotency_until <= :now
                """),
                    params,
                )
                row = (
                    (
                        await connection.execute(
                            text("""
                    SELECT id, request_hash, reservation_expires_at, replay_payload
                    FROM usage_events
                    WHERE tenant_id = :tenant AND principal_kind = :kind
                      AND principal_id = :principal AND idempotency_key = :idempotency_key
                """),
                            params,
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if row:
                    if row["request_hash"] != fingerprint:
                        raise ApiError(409, "IDEMPOTENCY_CONFLICT", "幂等标识已用于不同的请求")
                    if row["replay_payload"] is not None:
                        return Admission(row["id"], True, row["replay_payload"])
                    if row["reservation_expires_at"] > now:
                        raise ApiError(
                            409,
                            "REQUEST_IN_PROGRESS",
                            "请求正在执行",
                            retryable=True,
                            retry_after_ms=1000,
                        )
                    raise ApiError(503, "REQUEST_INTERRUPTED", "上一次请求执行中断")

            await enforce_quota(
                connection, principal, now, mode, body.modalities, self.max_concurrent
            )
            event_id = uuid4()
            await connection.execute(
                text(f"""
                WITH last_used AS (
                    UPDATE api_keys SET last_used_at = :now
                    WHERE id = :key_id AND tenant_id = :tenant
                ), admitted AS (
                    INSERT INTO usage_events (
                        id, tenant_id, principal_kind, principal_id, user_id, key_id, request_id, trace_id,
                        idempotency_key, idempotency_until, request_hash, mode, modalities, source_types,
                        created_at, reservation_expires_at
                    )
                    VALUES (
                        :id, :tenant, :kind, :principal, :user_id, :key_id, :request_id, :trace_id,
                        :idempotency_key, :idem_until, :hash, :mode, :modalities, :source_types,
                        :now, :lease
                    )
                    RETURNING id, tenant_id, created_at
                )
                {CAPTURE_ADMITTED_QUERY}
            """),
                {
                    **params,
                    "id": event_id,
                    "idem_until": now + timedelta(days=1) if idem else None,
                    "lease": now + timedelta(milliseconds=max_latency_ms + 5000),
                    "retained_query": retainable_query(body.query),
                },
            )
            return Admission(event_id, bool(idem))

    async def finish(
        self,
        admission: Admission,
        *,
        response: SearchResponse | None = None,
        error: ApiError | None = None,
        cancelled: bool = False,
        elapsed_ms: int | None = None,
    ) -> None:
        status = "cancelled" if cancelled else "failed"
        replay = None
        if response is not None:
            status = response.status
            if admission.idempotent:
                replay = {"response": response.model_dump(mode="json")}
        elif error is not None and admission.idempotent:
            replay = {
                "error": {
                    "status": error.status,
                    "code": error.code,
                    "message": str(error),
                    "retryable": error.retryable,
                    "retry_after_ms": error.retry_after_ms,
                    "provider_runs": [run.model_dump(mode="json") for run in error.provider_runs],
                }
            }
        async with self.db.begin() as connection:
            results = response.results if response else []
            runs = response.provider_runs if response else error.provider_runs if error else []
            insert_runs = _INSERT_PROVIDER_RUNS.format(source="finished")
            updated = await connection.scalar(
                text(f"""
                WITH finished AS (
                    UPDATE usage_events SET status = :status, code = :code, elapsed_ms = :elapsed_ms,
                        cache_hit = :cache_hit, text_results_returned = :text_count,
                        image_results_returned = :image_count, image_sources_present = :source_count,
                        pages_fetched = :pages, llm_tokens = :tokens, replay_payload = CAST(:replay AS JSONB)
                    WHERE id = :id AND status = 'running'
                    RETURNING id
                ), recorded AS ({insert_runs})
                SELECT id FROM finished
            """),
                {
                    "id": admission.event_id,
                    "status": status,
                    "code": error.code if error else ("REQUEST_CANCELLED" if cancelled else None),
                    "elapsed_ms": response.elapsed_ms if response else elapsed_ms,
                    "cache_hit": response.cache_hit if response else False,
                    "text_count": sum(result.kind == "text" for result in results),
                    "image_count": sum(result.kind == "image" for result in results),
                    "source_count": sum(
                        result.kind == "image" and result.image.source_page_url is not None
                        for result in results
                    ),
                    "pages": response.budget.pages_fetched if response else 0,
                    "tokens": response.budget.llm_tokens if response else 0,
                    "replay": json.dumps(replay, ensure_ascii=False) if replay else None,
                    "runs": _provider_runs_json(runs),
                },
            )
            if updated is not None and response is not None:
                await save_search_metadata(connection, admission.event_id, response)

    async def cancel(self, admission: Admission) -> None:
        await shield(
            self.finish(
                admission,
                error=ApiError(409, "REQUEST_CANCELLED", "请求已取消"),
                cancelled=True,
            )
        )
