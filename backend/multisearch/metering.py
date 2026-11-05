import json
import logging
from asyncio import CancelledError, create_task, shield, sleep
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
from .policy import lock_tenant, tenant_quota
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


async def assert_principal(
    connection: AsyncConnection, principal: SearchPrincipal, now: datetime
) -> None:
    params = {
        "tenant": principal.tenant_id,
        "user_id": principal.user_id,
        "key_id": principal.principal_id if principal.kind == "key" else None,
        "now": now,
    }
    if principal.kind == "key":
        valid = await connection.scalar(
            text("""
            SELECT k.id FROM api_keys k
            JOIN tenants t ON t.id = k.tenant_id
            JOIN users u ON u.id = k.owner_id
            JOIN memberships m ON (m.tenant_id, m.user_id) = (k.tenant_id, k.owner_id)
            WHERE k.tenant_id = :tenant AND k.id = :key_id AND k.owner_id = :user_id
              AND k.revoked_at IS NULL AND coalesce(k.rotation_expires_at, k.expires_at) > :now
              AND t.enabled AND u.enabled AND m.enabled
        """),
            params,
        )
        if valid is None:
            raise ApiError(401, "AUTH_REQUIRED", "需要有效的 API Key")
    else:
        active = await connection.scalar(
            text("""
            SELECT m.user_id FROM memberships m
            JOIN tenants t ON t.id = m.tenant_id JOIN users u ON u.id = m.user_id
            WHERE m.tenant_id = :tenant AND m.user_id = :user_id AND m.enabled
              AND t.enabled AND u.enabled
        """),
            params,
        )
        if active is None:
            raise ApiError(403, "FORBIDDEN", "租户或账户已停用")


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
    quota = await tenant_quota(connection, principal.tenant_id, max_concurrent)
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
        SELECT {", ".join(columns)} FROM usage_events
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
        text("""
        INSERT INTO provider_runs (usage_id, ordinal, provider_id, status, elapsed_ms, count)
        VALUES (:usage_id, :ordinal, :provider_id, :status, :elapsed_ms, :count)
    """),
        [
            {
                "usage_id": usage_id,
                "ordinal": index,
                **run.model_dump(include={"provider_id", "status", "elapsed_ms", "count"}),
            }
            for index, run in enumerate(runs)
        ],
    )


async def has_concurrency_slot(
    connection: AsyncConnection,
    principal: SearchPrincipal,
    now: datetime,
    max_concurrent: int,
) -> bool:
    quota = await tenant_quota(connection, principal.tenant_id, max_concurrent)
    key_limit = principal.limits.get("concurrency")
    result = (
        (
            await connection.execute(
                text(f"""
        SELECT count(*) AS tenant_count,
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
    return result["tenant_count"] < quota["concurrency"] and (
        key_limit is None or result["key_count"] < key_limit
    )


class UsageMeter:
    def __init__(self, db: AsyncEngine, max_concurrent: int):
        self.db = db
        self.max_concurrent = max_concurrent

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
        task = create_task(self._maintain())
        try:
            yield
        finally:
            task.cancel()
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
        async with self.db.begin() as connection:
            # 同租户的额度检查和入账使用同一事务锁，多 API 实例也不会超扣。
            await lock_tenant(connection, principal.tenant_id)
            now = (await connection.scalar(text("SELECT clock_timestamp()"))).astimezone(UTC)
            params["now"] = now
            await assert_principal(connection, principal, now)
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
                text("""
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
            """),
                {
                    **params,
                    "id": event_id,
                    "idem_until": now + timedelta(days=1) if idem else None,
                    "lease": now + timedelta(milliseconds=max_latency_ms + 5000),
                },
            )
            if principal.kind == "key":
                await connection.execute(
                    text("""
                    UPDATE api_keys SET last_used_at = :now WHERE id = :key_id AND tenant_id = :tenant
                """),
                    params,
                )
            return Admission(event_id, bool(idem))

    async def finish(
        self,
        admission: Admission,
        *,
        response: SearchResponse | None = None,
        error: ApiError | None = None,
        cancelled: bool = False,
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
                }
            }
        async with self.db.begin() as connection:
            results = response.results if response else []
            await connection.execute(
                text("""
                UPDATE usage_events SET status = :status, code = :code, elapsed_ms = :elapsed_ms,
                    cache_hit = :cache_hit, text_results_returned = :text_count,
                    image_results_returned = :image_count, image_sources_present = :source_count,
                    pages_fetched = :pages, llm_tokens = :tokens, replay_payload = CAST(:replay AS JSONB)
                WHERE id = :id AND status = 'running'
            """),
                {
                    "id": admission.event_id,
                    "status": status,
                    "code": error.code if error else ("REQUEST_CANCELLED" if cancelled else None),
                    "elapsed_ms": response.elapsed_ms if response else None,
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
                },
            )
            if response is not None:
                await save_provider_runs(connection, admission.event_id, response.provider_runs)
                await save_search_metadata(connection, admission.event_id, response)

    async def cancel(self, admission: Admission) -> None:
        await shield(
            self.finish(
                admission,
                error=ApiError(409, "REQUEST_CANCELLED", "请求已取消"),
                cancelled=True,
            )
        )
