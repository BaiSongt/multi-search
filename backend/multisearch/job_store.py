import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from uuid import UUID, uuid4

from pydantic import TypeAdapter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .contracts import Modality, Mode, ProviderRun, SearchResult
from .errors import ApiError
from .evidence_contracts import EvidenceRecord
from .evidence_store import save_evidence, save_images
from .job_contracts import JobError, JobKind, JobProgress, JobStatus, JobView
from .metering import (
    SearchPrincipal,
    assert_principal,
    enforce_quota,
    has_concurrency_slot,
    save_provider_runs,
)
from .policy import lock_tenant, try_lock_tenant
from .reporting import _cursor, _read_cursor
from .tracing import trace_id

LEASE_SECONDS = 20
_ACTIVE = {"queued", "running", "cancelling"}
_RESULTS = TypeAdapter(list[SearchResult])
_VIEW = """
    id AS job_id, tenant_id, kind, status, created_at, updated_at, deadline_at, attempts,
    max_attempts, progress, result, error,
    (SELECT u.trace_id FROM usage_events u WHERE u.id = search_jobs.id) AS trace_id,
    (SELECT u.request_id FROM usage_events u WHERE u.id = search_jobs.id) AS request_id
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
    trace_id: str | None = None
    request_id: str | None = None


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
        *,
        worker_available: bool = True,
        idempotency_payload: dict | None = None,
    ) -> JobView:
        mode = Mode.EVIDENCE if kind == JobKind.FETCH else Mode.DEEP
        encoded = _json(payload)
        fingerprint_input = encoded if idempotency_payload is None else _json(idempotency_payload)
        fingerprint = sha256(fingerprint_input.encode()).hexdigest()
        async with self.db.begin() as connection:
            await lock_tenant(connection, principal.tenant_id)
            now = await assert_principal(connection, principal)
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
            if not worker_available:
                raise ApiError(503, "WORKER_UNAVAILABLE", "后台 Worker 暂不可用", retryable=True)
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
                    "trace_id": trace_id(),
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
                    request_id, trace_id, operation, mode, modalities, source_types, created_at,
                    reservation_expires_at, research_jobs_started)
                VALUES (:id, :tenant, :principal_kind, :principal, :owner, :key_id,
                    :request_id, :trace_id, :kind, :mode, :modalities, :sources, :now, :deadline, :research_started)
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
        # 旧 Worker 可能只在任务进度中写入模型预留，终结时同步旧报告的未知用量。
        await connection.execute(
            text("""
            WITH expired AS (
                SELECT id, CASE
                    WHEN status = 'cancelling' THEN 'cancelled'
                    WHEN deadline_at <= now() OR attempts >= max_attempts THEN
                        CASE WHEN jsonb_array_length(COALESCE(result -> 'evidence_records', '[]'::JSONB)) > 0
                            OR jsonb_array_length(COALESCE(result -> 'ocr_pending', '[]'::JSONB)) > 0
                            OR result @? '$.results[*] ? (@.evidence_status == "ocr_pending")'
                            THEN 'partial' ELSE 'failed' END
                    ELSE 'queued' END AS next_status,
                    CASE
                    WHEN status = 'cancelling' THEN 'JOB_CANCELLED'
                    WHEN deadline_at <= now() THEN 'TIME_BUDGET_EXCEEDED'
                    ELSE 'WORKER_INTERRUPTED' END AS error_code,
                    COALESCE((progress #>> '{research_budget,llm_tokens_reserved}')::INTEGER, 0)
                        AS model_reservation
                FROM search_jobs
                WHERE (status = 'queued' AND deadline_at <= now())
                    OR (status IN ('running','cancelling') AND lease_expires_at <= now())
                ORDER BY created_at LIMIT 64 FOR UPDATE SKIP LOCKED
            ), recovered AS (
                UPDATE search_jobs j SET status = e.next_status, updated_at = now(),
                    lease_token = NULL, lease_expires_at = NULL,
                    progress = jsonb_set(j.progress, '{stage}', to_jsonb(e.next_status)),
                    payload = CASE WHEN e.next_status = 'queued' THEN j.payload ELSE '{}'::JSONB END,
                    result = CASE WHEN e.next_status = 'cancelled' THEN NULL
                        WHEN jsonb_typeof(j.result -> 'usage') = 'object'
                            AND e.model_reservation > COALESCE(
                                (j.result #>> '{usage,llm_tokens_reserved}')::INTEGER, 0
                            )
                        THEN jsonb_set(j.result, '{usage}', (j.result -> 'usage') || jsonb_build_object(
                            'llm_input_tokens', NULL, 'llm_output_tokens', NULL, 'llm_tokens', NULL,
                            'llm_tokens_reserved', e.model_reservation,
                            'llm_usage_known', FALSE, 'estimated_cost', NULL
                        ))
                        ELSE j.result END,
                    error = CASE WHEN e.next_status <> 'queued' THEN
                        jsonb_build_object('code', e.error_code, 'message', '任务已到期、取消或执行中断')
                        ELSE NULL END
                FROM expired e WHERE j.id = e.id
                RETURNING j.id, j.tenant_id, j.status, j.progress, j.result, e.error_code,
                    e.model_reservation
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
                llm_tokens = GREATEST(u.llm_tokens, COALESCE((r.progress ->> 'llm_tokens')::INTEGER, 0)),
                llm_tokens_reserved = r.model_reservation,
                llm_usage_unknown = r.model_reservation > 0
                    AND NOT COALESCE((r.result #>> '{usage,llm_usage_known}')::BOOLEAN, FALSE),
                estimated_cost = (r.result #>> '{usage,estimated_cost}')::NUMERIC,
                cost_currency = r.result #>> '{usage,cost_currency}'
            FROM recovered r WHERE u.id = r.id AND r.status IN ('partial','failed','cancelled')
        """)
        )

    async def claim(
        self, kinds: tuple[JobKind, ...], *, allow_dynamic: bool = False
    ) -> Claim | None:
        async with self.db.begin() as connection:
            await self._recover(connection)
            cutoff = await connection.scalar(text("SELECT clock_timestamp()"))
        params = {
            "kinds": [kind.value for kind in kinds],
            "allow_dynamic": allow_dynamic,
            "cutoff": cutoff,
            "after": None,
            "id": None,
        }
        while True:
            async with self.db.connect() as connection:
                candidates = (
                    await connection.execute(
                        text("""
                    SELECT id, tenant_id, created_at FROM search_jobs
                    WHERE status = 'queued' AND deadline_at > now() AND kind = ANY(:kinds)
                        AND created_at <= :cutoff
                        AND (:allow_dynamic OR kind <> 'fetch'
                            OR payload->>'extraction_mode' IS DISTINCT FROM 'dynamic')
                        AND (CAST(:after AS TIMESTAMPTZ) IS NULL OR (created_at, id) > (:after, :id))
                    ORDER BY created_at, id LIMIT 64
                """),
                        params,
                    )
                ).all()
            if not candidates:
                return None
            for job_id, tenant_id, _ in candidates:
                async with self.db.begin() as connection:
                    if await try_lock_tenant(connection, tenant_id):
                        claim = await self._claim_candidate(connection, job_id, tenant_id)
                        if claim is not None:
                            return claim
            if len(candidates) < 64:
                return None
            last_id, _, last_created_at = candidates[-1]
            params.update(after=last_created_at, id=last_id)

    async def _claim_candidate(
        self,
        connection: AsyncConnection,
        job_id: UUID,
        tenant_id: UUID,
    ) -> Claim | None:
        row = (
            (
                await connection.execute(
                    text("""
            SELECT j.owner_id, j.principal_id, u.principal_kind, COALESCE(k.limits, '{}'::JSONB) AS limits
            FROM search_jobs j JOIN usage_events u ON u.id = j.id
            LEFT JOIN api_keys k ON k.id = u.key_id
            WHERE j.id = :id AND j.status = 'queued' AND j.deadline_at > now()
            FOR UPDATE OF j SKIP LOCKED
        """),
                    {"id": job_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        principal = SearchPrincipal(
            tenant_id, row["owner_id"], row["principal_id"], row["principal_kind"], row["limits"]
        )
        try:
            now = await assert_principal(connection, principal)
        except ApiError:
            await connection.execute(
                text("""
                WITH stopped AS (
                    UPDATE search_jobs SET status = 'failed', updated_at = now(), payload = '{}'::JSONB,
                        progress = jsonb_set(progress, '{stage}', '"failed"'::JSONB),
                        error = '{"code":"JOB_PRINCIPAL_INACTIVE","message":"任务身份已停用"}'::JSONB
                    WHERE id = :id RETURNING id, progress, result
                )
                UPDATE usage_events SET status = 'failed', code = 'JOB_PRINCIPAL_INACTIVE',
                    llm_tokens_reserved = COALESCE(
                        (stopped.progress #>> '{research_budget,llm_tokens_reserved}')::INTEGER, 0
                    ),
                    llm_usage_unknown = COALESCE(
                        (stopped.progress #>> '{research_budget,llm_tokens_reserved}')::INTEGER, 0
                    ) > 0 AND NOT COALESCE((stopped.result #>> '{usage,llm_usage_known}')::BOOLEAN, FALSE)
                FROM stopped WHERE usage_events.id = stopped.id
            """),
                {"id": job_id},
            )
            return None
        if not await has_concurrency_slot(connection, principal, now, self.max_concurrent):
            return None
        result = await connection.execute(
            text("""
                UPDATE search_jobs SET status = 'running', attempts = attempts + 1,
                    lease_token = :token, lease_expires_at = now() + make_interval(secs => :lease),
                    updated_at = now(), progress = jsonb_set(progress, '{stage}', '"running"'::JSONB)
                WHERE id = :id AND deadline_at > clock_timestamp()
                RETURNING id AS job_id, tenant_id, kind, lease_token, payload, deadline_at,
                    progress, result AS checkpoint,
                    (SELECT u.trace_id FROM usage_events u WHERE u.id = search_jobs.id) AS trace_id,
                    (SELECT u.request_id FROM usage_events u WHERE u.id = search_jobs.id) AS request_id
            """),
            {"id": job_id, "token": uuid4(), "lease": LEASE_SECONDS},
        )
        claimed = result.mappings().one_or_none()
        if claimed is None:
            return None
        return Claim(
            **{
                **claimed,
                "kind": JobKind(claimed["kind"]),
                "progress": JobProgress.model_validate(claimed["progress"]),
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
            outcome_result = outcome.result or {}
            result = outcome_result if row["status"] != "cancelled" else {}
            results = _RESULTS.validate_python(result.get("results", []))
            usage = outcome_result.get("usage", {})
            reserved = (row["progress"].get("research_budget") or {}).get("llm_tokens_reserved", 0)
            cost = usage.get("estimated_cost")
            await connection.execute(
                text("""
                UPDATE usage_events SET status = :status, code = :code, pages_fetched = :pages,
                    llm_tokens = :tokens, llm_tokens_reserved = :reserved,
                    llm_usage_unknown = :usage_unknown, estimated_cost = :cost, cost_currency = :currency,
                    text_results_returned = :text_count,
                    image_results_returned = :image_count, image_sources_present = :source_count,
                    elapsed_ms = LEAST(2147483647, (extract(epoch FROM (now() - created_at)) * 1000)::BIGINT)
                WHERE id = :id
            """),
                {
                    "id": claim.job_id,
                    "status": row["status"],
                    "code": row["error"]["code"] if row["error"] else None,
                    "pages": max(outcome.pages_fetched, row["progress"].get("pages_fetched", 0)),
                    "tokens": max(outcome.llm_tokens, row["progress"].get("llm_tokens", 0)),
                    "reserved": max(reserved, usage.get("llm_tokens_reserved", 0)),
                    "usage_unknown": not usage.get("llm_usage_known", reserved == 0),
                    "cost": Decimal(cost) if cost is not None else None,
                    "currency": usage.get("cost_currency"),
                    "text_count": sum(item.kind == "text" for item in results),
                    "image_count": sum(item.kind == "image" for item in results),
                    "source_count": sum(
                        item.kind == "image" and item.image.source_page_url is not None
                        for item in results
                    ),
                },
            )
            if row["status"] in {"completed", "partial"}:
                await save_evidence(
                    connection,
                    claim.job_id,
                    [
                        EvidenceRecord.model_validate(record)
                        for record in result.get("evidence_records", [])
                    ],
                )
                await save_images(connection, claim.job_id, results)
            if claim.kind == JobKind.RESEARCH:
                await save_provider_runs(
                    connection,
                    claim.job_id,
                    [
                        ProviderRun.model_validate(run)
                        for round_record in result.get("rounds", [])
                        for run in round_record.get("provider_runs", [])
                    ],
                )
            return True

    async def get(
        self,
        job_id: UUID,
        tenant_id: UUID | None,
        kinds: tuple[JobKind, ...],
        *,
        owner_id: UUID | None = None,
    ) -> JobView:
        async with self.db.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(f"""
                SELECT {_VIEW} FROM search_jobs WHERE id = :id
                    AND (CAST(:tenant AS UUID) IS NULL OR tenant_id = :tenant)
                    AND (CAST(:owner AS UUID) IS NULL OR owner_id = :owner)
                    AND kind = ANY(:kinds)
            """),
                        {
                            "id": job_id,
                            "tenant": tenant_id,
                            "owner": owner_id,
                            "kinds": [k.value for k in kinds],
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise _not_found()
            return JobView.model_validate(dict(row))

    async def cancel(
        self,
        job_id: UUID,
        tenant_id: UUID | None,
        kinds: tuple[JobKind, ...],
        *,
        owner_id: UUID | None = None,
    ) -> JobView:
        async with self.db.begin() as connection:
            params = {
                "id": job_id,
                "tenant": tenant_id,
                "owner": owner_id,
                "kinds": [k.value for k in kinds],
            }
            status = await connection.scalar(
                text("""
                SELECT status FROM search_jobs WHERE id = :id
                    AND (CAST(:tenant AS UUID) IS NULL OR tenant_id = :tenant) AND kind = ANY(:kinds)
                    AND (CAST(:owner AS UUID) IS NULL OR owner_id = :owner)
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
                reserved = (row["progress"].get("research_budget") or {}).get(
                    "llm_tokens_reserved", 0
                )
                await connection.execute(
                    text("""
                    UPDATE usage_events SET status = 'cancelled', code = 'JOB_CANCELLED',
                        llm_tokens_reserved = :reserved, llm_usage_unknown = :usage_unknown
                    WHERE id = :id
                """),
                    {"id": job_id, "reserved": reserved, "usage_unknown": reserved > 0},
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
