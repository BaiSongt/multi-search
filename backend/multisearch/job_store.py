import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .contracts import Modality, Mode
from .errors import ApiError
from .evidence_contracts import EvidenceRecord
from .evidence_store import save_evidence
from .job_contracts import JobError, JobKind, JobProgress, JobStatus, JobView
from .metering import SearchPrincipal, assert_principal, enforce_quota
from .policy import lock_tenant
from .reporting import _cursor, _read_cursor

LEASE_SECONDS = 20
_ACTIVE = {"queued", "running", "cancelling"}
_VIEW = """
    id AS job_id, tenant_id, kind, status, created_at, updated_at, deadline_at, attempts,
    max_attempts, progress, result, error
"""


@dataclass(frozen=True)
class Claim:
    job_id: UUID
    tenant_id: UUID
    lease_token: UUID
    kind: JobKind
    payload: dict
    deadline_at: datetime
    progress: JobProgress | None = None
    checkpoint: dict | None = None


@dataclass(frozen=True)
class JobOutcome:
    status: JobStatus
    result: dict | None = None
    error: JobError | None = None
    pages_fetched: int = 0
    llm_tokens: int = 0


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _not_found() -> ApiError:
    return ApiError(404, "JOB_NOT_FOUND", "任务不存在")


class JobStore:
    def __init__(self, db: AsyncEngine, max_concurrent: int = 20):
        self.db = db
        self.max_concurrent = max_concurrent

    async def create(
        self,
        principal: SearchPrincipal,
        kind: JobKind,
        payload: dict,
        request_id: str,
        idempotency_key: str | None,
        max_latency_ms: int,
        total: int,
    ) -> JobView:
        mode = Mode.EVIDENCE if kind == JobKind.FETCH else Mode.DEEP
        encoded = _json(payload)
        fingerprint = sha256(encoded.encode()).hexdigest()
        async with self.db.begin() as connection:
            await lock_tenant(connection, principal.tenant_id)
            now = (await connection.scalar(text("SELECT clock_timestamp()"))).astimezone(UTC)
            await assert_principal(connection, principal, now)
            params = {
                "tenant": principal.tenant_id,
                "owner": principal.user_id,
                "principal": principal.principal_id,
                "principal_kind": principal.kind,
                "key_id": principal.principal_id if principal.kind == "key" else None,
                "kind": kind.value,
                "mode": mode.value,
                "idempotency_key": idempotency_key,
                "now": now,
                "deadline": now + timedelta(milliseconds=max_latency_ms),
            }
            if idempotency_key is not None:
                existing = (
                    (
                        await connection.execute(
                            text(f"""
                    SELECT {_VIEW}, request_hash FROM search_jobs
                    WHERE tenant_id = :tenant AND principal_id = :principal AND kind = :kind
                        AND idempotency_key = :idempotency_key
                """),
                            params,
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if existing:
                    if existing["request_hash"] != fingerprint:
                        raise ApiError(409, "IDEMPOTENCY_CONFLICT", "幂等标识已用于不同的任务")
                    return JobView.model_validate(dict(existing))
            queue_size = await connection.scalar(
                text("""
                SELECT count(*) FROM search_jobs WHERE tenant_id = :tenant
                    AND status IN ('queued','running','cancelling') AND deadline_at > :now
            """),
                params,
            )
            if queue_size >= 10:
                raise ApiError(429, "QUOTA_EXCEEDED", "当前租户待处理任务过多", retryable=True)
            modalities = [Modality(value) for value in payload.get("modalities", [])]
            await enforce_quota(
                connection,
                principal,
                now,
                mode,
                modalities,
                self.max_concurrent,
                include_concurrency=False,
            )
            params.update(
                {
                    "id": uuid4(),
                    "request_id": request_id,
                    "hash": fingerprint,
                    "payload": encoded,
                    "progress": _json({"stage": "queued", "completed": 0, "total": total}),
                    "modalities": [value.value for value in modalities],
                    "sources": payload.get("source_types", []),
                    "research_started": int(kind == JobKind.RESEARCH),
                }
            )
            await connection.execute(
                text("""
                INSERT INTO usage_events (id, tenant_id, principal_kind, principal_id, user_id, key_id,
                    request_id, operation, mode, modalities, source_types, created_at,
                    reservation_expires_at, research_jobs_started)
                VALUES (:id, :tenant, :principal_kind, :principal, :owner, :key_id,
                    :request_id, :kind, :mode, :modalities, :sources, :now, :deadline, :research_started)
            """),
                params,
            )
            job = (
                (
                    await connection.execute(
                        text(f"""
                INSERT INTO search_jobs (
                    id, tenant_id, owner_id, principal_id, kind, idempotency_key, request_hash,
                    payload, progress, deadline_at, created_at, updated_at)
                VALUES (:id, :tenant, :owner, :principal, :kind, :idempotency_key, :hash,
                    CAST(:payload AS JSONB), CAST(:progress AS JSONB), :deadline, :now, :now)
                RETURNING {_VIEW}
            """),
                        params,
                    )
                )
                .mappings()
                .one()
            )
            if principal.kind == "key":
                await connection.execute(
                    text("UPDATE api_keys SET last_used_at = :now WHERE id = :key_id"),
                    params,
                )
            return JobView.model_validate(dict(job))

    async def _recover(self, connection: AsyncConnection) -> None:
        # 过期租约可重领。任务期限或尝试次数耗尽时，原子保存终态和计量状态。
        await connection.execute(
            text("""
            WITH expired AS (
                SELECT id, CASE
                    WHEN status = 'cancelling' THEN 'cancelled'
                    WHEN deadline_at <= now() OR attempts >= max_attempts THEN
                        CASE WHEN jsonb_array_length(COALESCE(result -> 'evidence_records', '[]'::JSONB)) > 0
                            THEN 'partial' ELSE 'failed' END
                    ELSE 'queued' END AS next_status,
                    CASE
                    WHEN status = 'cancelling' THEN 'JOB_CANCELLED'
                    WHEN deadline_at <= now() THEN 'TIME_BUDGET_EXCEEDED'
                    ELSE 'WORKER_INTERRUPTED' END AS error_code
                FROM search_jobs
                WHERE (status = 'queued' AND deadline_at <= now())
                    OR (status IN ('running','cancelling') AND lease_expires_at <= now())
                ORDER BY created_at LIMIT 64 FOR UPDATE SKIP LOCKED
            ), recovered AS (
                UPDATE search_jobs j SET status = e.next_status, updated_at = now(),
                    lease_token = NULL, lease_expires_at = NULL,
                    progress = jsonb_set(j.progress, '{stage}', to_jsonb(e.next_status)),
                    payload = CASE WHEN e.next_status = 'queued' THEN j.payload ELSE '{}'::JSONB END,
                    result = CASE WHEN e.next_status = 'cancelled' THEN NULL ELSE j.result END,
                    error = CASE WHEN e.next_status <> 'queued' THEN
                        jsonb_build_object('code', e.error_code, 'message', '任务已到期、取消或执行中断')
                        ELSE NULL END
                FROM expired e WHERE j.id = e.id
                RETURNING j.id, j.tenant_id, j.status, j.progress, j.result, e.error_code
            ), evidence AS (
                INSERT INTO evidence_records (tenant_id, evidence_id, usage_id, record)
                SELECT r.tenant_id, source.record ->> 'evidence_id', r.id, source.record
                FROM recovered r
                CROSS JOIN LATERAL jsonb_array_elements(COALESCE(r.result -> 'evidence_records', '[]'::JSONB))
                    AS source(record)
                WHERE r.status = 'partial'
                ON CONFLICT (tenant_id, evidence_id) DO NOTHING
            )
            UPDATE usage_events u SET status = r.status, code = r.error_code,
                pages_fetched = GREATEST(u.pages_fetched, COALESCE((r.progress ->> 'pages_fetched')::INTEGER, 0)),
                llm_tokens = GREATEST(u.llm_tokens, COALESCE((r.progress ->> 'llm_tokens')::INTEGER, 0))
            FROM recovered r WHERE u.id = r.id AND r.status IN ('partial','failed','cancelled')
        """)
        )

    async def claim(self, kinds: tuple[JobKind, ...]) -> Claim | None:
        async with self.db.begin() as connection:
            await self._recover(connection)
            result = await connection.execute(
                text("""
                UPDATE search_jobs SET status = 'running', attempts = attempts + 1,
                    lease_token = :token, lease_expires_at = now() + make_interval(secs => :lease),
                    updated_at = now(), progress = jsonb_set(progress, '{stage}', '"running"'::JSONB)
                WHERE id = (
                    SELECT id FROM search_jobs
                    WHERE status = 'queued' AND deadline_at > now() AND kind = ANY(:kinds)
                    ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED
                )
                RETURNING id AS job_id, tenant_id, kind, lease_token, payload, deadline_at,
                    progress, result AS checkpoint
            """),
                {"token": uuid4(), "lease": LEASE_SECONDS, "kinds": [k.value for k in kinds]},
            )
            row = result.mappings().one_or_none()
            if row is None:
                return None
            return Claim(
                **{
                    **row,
                    "kind": JobKind(row["kind"]),
                    "progress": JobProgress.model_validate(row["progress"]),
                }
            )

    async def renew(self, claim: Claim) -> bool:
        async with self.db.begin() as connection:
            active = await connection.scalar(
                text("""
                UPDATE search_jobs SET lease_expires_at = now() + make_interval(secs => :lease)
                WHERE id = :id AND lease_token = :token AND status = 'running'
                    AND lease_expires_at > now()
                RETURNING id
            """),
                {"id": claim.job_id, "token": claim.lease_token, "lease": LEASE_SECONDS},
            )
            return active is not None

    async def progress(
        self, claim: Claim, progress: JobProgress, *, checkpoint: dict | None = None
    ) -> bool:
        async with self.db.begin() as connection:
            updated = await connection.scalar(
                text("""
                UPDATE search_jobs SET progress = CAST(:progress AS JSONB), updated_at = now(),
                    result = COALESCE(CAST(:checkpoint AS JSONB), result)
                WHERE id = :id AND lease_token = :token AND status = 'running'
                    AND lease_expires_at > now()
                RETURNING id
            """),
                {
                    "id": claim.job_id,
                    "token": claim.lease_token,
                    "progress": progress.model_dump_json(),
                    "checkpoint": _json(checkpoint) if checkpoint is not None else None,
                },
            )
            return updated is not None

    async def finish(self, claim: Claim, outcome: JobOutcome) -> bool:
        if outcome.status.value in _ACTIVE:
            raise ValueError("任务结果必须是终态")
        error = outcome.error.model_dump_json() if outcome.error else None
        async with self.db.begin() as connection:
            row = (
                (
                    await connection.execute(
                        text("""
                UPDATE search_jobs SET
                    status = CASE WHEN status = 'cancelling' THEN 'cancelled' ELSE :status END,
                    result = CASE WHEN status = 'cancelling' THEN NULL ELSE CAST(:result AS JSONB) END,
                    error = CASE WHEN status = 'cancelling' THEN
                        '{"code":"JOB_CANCELLED","message":"任务已取消"}'::JSONB
                        ELSE CAST(:error AS JSONB) END,
                    progress = jsonb_set(progress, '{stage}', to_jsonb(
                        CASE WHEN status = 'cancelling' THEN 'cancelled' ELSE CAST(:status AS TEXT) END)),
                    lease_token = NULL, lease_expires_at = NULL, payload = '{}'::JSONB, updated_at = now()
                WHERE id = :id AND lease_token = :token AND status IN ('running','cancelling')
                    AND lease_expires_at > now()
                RETURNING status, error, progress
            """),
                        {
                            "id": claim.job_id,
                            "token": claim.lease_token,
                            "status": outcome.status.value,
                            "result": _json(outcome.result) if outcome.result is not None else None,
                            "error": error,
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return False
            await connection.execute(
                text("""
                UPDATE usage_events SET status = :status, code = :code, pages_fetched = :pages,
                    llm_tokens = :tokens,
                    elapsed_ms = LEAST(2147483647, (extract(epoch FROM (now() - created_at)) * 1000)::BIGINT)
                WHERE id = :id
            """),
                {
                    "id": claim.job_id,
                    "status": row["status"],
                    "code": row["error"]["code"] if row["error"] else None,
                    "pages": max(outcome.pages_fetched, row["progress"].get("pages_fetched", 0)),
                    "tokens": max(outcome.llm_tokens, row["progress"].get("llm_tokens", 0)),
                },
            )
            if row["status"] in {"completed", "partial"} and outcome.result is not None:
                await save_evidence(
                    connection,
                    claim.job_id,
                    [
                        EvidenceRecord.model_validate(record)
                        for record in outcome.result.get("evidence_records", [])
                    ],
                )
            return True

    async def get(
        self, job_id: UUID, tenant_id: UUID | None, kinds: tuple[JobKind, ...]
    ) -> JobView:
        async with self.db.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(f"""
                SELECT {_VIEW} FROM search_jobs WHERE id = :id
                    AND (CAST(:tenant AS UUID) IS NULL OR tenant_id = :tenant)
                    AND kind = ANY(:kinds)
            """),
                        {"id": job_id, "tenant": tenant_id, "kinds": [k.value for k in kinds]},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise _not_found()
            return JobView.model_validate(dict(row))

    async def cancel(
        self, job_id: UUID, tenant_id: UUID | None, kinds: tuple[JobKind, ...]
    ) -> JobView:
        async with self.db.begin() as connection:
            params = {"id": job_id, "tenant": tenant_id, "kinds": [k.value for k in kinds]}
            status = await connection.scalar(
                text("""
                SELECT status FROM search_jobs WHERE id = :id
                    AND (CAST(:tenant AS UUID) IS NULL OR tenant_id = :tenant) AND kind = ANY(:kinds)
                FOR UPDATE
            """),
                params,
            )
            if status is None:
                raise _not_found()
            if status not in _ACTIVE:
                raise ApiError(409, "JOB_ALREADY_TERMINAL", "任务已经结束")
            state = "cancelled" if status == "queued" else "cancelling"
            row = (
                (
                    await connection.execute(
                        text(f"""
                UPDATE search_jobs SET status = :status, updated_at = now(),
                    progress = jsonb_set(progress, '{{stage}}', to_jsonb(CAST(:status AS TEXT))),
                    payload = CASE WHEN :status = 'cancelled' THEN '{{}}'::JSONB ELSE payload END,
                    error = CASE WHEN :status = 'cancelled' THEN
                        '{{"code":"JOB_CANCELLED","message":"任务已取消"}}'::JSONB ELSE NULL END
                WHERE id = :id RETURNING {_VIEW}
            """),
                        {"id": job_id, "status": state},
                    )
                )
                .mappings()
                .one()
            )
            if state == "cancelled":
                await connection.execute(
                    text("""
                    UPDATE usage_events SET status = 'cancelled', code = 'JOB_CANCELLED' WHERE id = :id
                """),
                    {"id": job_id},
                )
            return JobView.model_validate(dict(row))

    async def list(
        self,
        tenant_id: UUID | None,
        status: JobStatus | None,
        kind: JobKind | None,
        limit: int,
        cursor: str | None,
    ) -> dict:
        after, identifier = _read_cursor(cursor)
        async with self.db.connect() as connection:
            result = await connection.execute(
                text(f"""
                SELECT {_VIEW} FROM search_jobs
                WHERE (CAST(:tenant AS UUID) IS NULL OR tenant_id = :tenant)
                    AND (CAST(:kind AS TEXT) IS NULL OR kind = :kind)
                    AND (CAST(:status AS TEXT) IS NULL OR status = :status)
                    AND (CAST(:after AS TIMESTAMPTZ) IS NULL OR (created_at, id) < (:after, :id))
                ORDER BY created_at DESC, id DESC LIMIT :limit
            """),
                {
                    "tenant": tenant_id,
                    "kind": kind,
                    "status": status,
                    "after": after,
                    "id": identifier,
                    "limit": limit + 1,
                },
            )
            rows = result.mappings().all()
        boundary = rows[limit - 1] if len(rows) > limit else None
        return {
            "items": [JobView.model_validate(dict(row)) for row in rows[:limit]],
            "next_cursor": _cursor({"created_at": boundary["created_at"], "id": boundary["job_id"]})
            if boundary
            else None,
        }
