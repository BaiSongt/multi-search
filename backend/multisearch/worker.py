import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import time

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from .dynamic_renderer import HEARTBEAT_KEY, DynamicRenderer
from .evidence import EvidenceBuilder
from .identity import IdentityStore
from .job_contracts import FetchRequest, JobError, JobKind, JobProgress, JobStatus
from .job_store import Claim, JobOutcome, JobStore
from .observability import FetchMetrics
from .ops_store import OpsStore
from .pdf_parser import OCRPending
from .research import ResearchOrchestrator, ResearchPricing
from .research_model import create_research_model
from .runtime import build_evidence, build_search, load_search_policies
from .safe_fetch import FetchError
from .settings import Settings
from .tracing import bind_trace, redact_http_client_logging, trace_event

logger = logging.getLogger(__name__)
HEALTH_FILE = Path("/tmp/multisearch-worker-ready")


def _remaining(claim: Claim) -> float:
    return (claim.deadline_at - datetime.now(UTC)).total_seconds()


class Worker:
    def __init__(self, store: JobStore, evidence: EvidenceBuilder):
        self.store = store
        self.evidence = evidence
        self.handlers = {JobKind.FETCH: self.fetch}
        self.dynamic_ready = False

    async def fetch(self, claim: Claim) -> JobOutcome:
        body = FetchRequest.model_validate(claim.payload)
        targets = body.targets()
        checkpoint = claim.checkpoint or {}
        records = list(checkpoint.get("evidence_records", []))
        warnings = list(checkpoint.get("warnings", []))
        errors = list(checkpoint.get("failures", []))
        pending = list(checkpoint.get("ocr_pending", []))
        completed = min(claim.progress.completed, len(targets)) if claim.progress else 0
        if completed > len(records) + len(errors) + len(pending):
            warnings.append("FETCH_ATTEMPT_INTERRUPTED")
        if body.urls and len(body.urls) > len(targets):
            warnings.append("MAX_PAGES_REACHED")
        result = {"evidence_records": records, "warnings": warnings, "failures": errors}
        if pending:
            result["ocr_pending"] = pending
        for index in range(completed, len(targets)):
            url = targets[index]
            remaining = _remaining(claim)
            if remaining <= 0:
                warnings.append("TIME_BUDGET_EXCEEDED")
                break
            progress = JobProgress(
                stage="extracting",
                completed=index + 1,
                total=len(targets),
                pages_fetched=len(records),
            )
            # 先预留本页。进程中断后跳过结果未知的尝试，避免重复消耗页数。
            if not await self.store.progress(claim, progress, checkpoint=result):
                return JobOutcome(JobStatus.CANCELLED, pages_fetched=len(records))
            remaining = _remaining(claim)
            if remaining <= 0:
                warnings.append("TIME_BUDGET_EXCEEDED")
                break
            try:
                async with asyncio.timeout(min(remaining, 20)):
                    record = await self.evidence.extract(
                        url,
                        timeout_s=min(remaining, 20),
                        network_scope=body.network_scope,
                        domain_allowlist=body.domain_allowlist,
                        domain_denylist=body.domain_denylist,
                        extraction_mode=body.extraction_mode,
                    )
            except TimeoutError:
                errors.append(
                    {"index": index, "code": "TIME_BUDGET_EXCEEDED", "message": "抓取时限已耗尽"}
                )
            except OCRPending as error:
                pending.append(index)
                result["ocr_pending"] = pending
                warnings.append(error.code)
            except FetchError as error:
                errors.append({"index": index, "code": error.code, "message": str(error)})
            else:
                records.append(record.model_dump(mode="json"))
            progress.pages_fetched = len(records)
            if not await self.store.progress(claim, progress, checkpoint=result):
                return JobOutcome(JobStatus.CANCELLED, pages_fetched=len(records))
        result["warnings"] = list(dict.fromkeys(warnings))
        if not records and not pending:
            code = errors[0]["code"] if errors else "TIME_BUDGET_EXCEEDED"
            return JobOutcome(
                JobStatus.FAILED, result, JobError(code=code, message="没有取得可用证据")
            )
        status = JobStatus.PARTIAL if warnings or errors or pending else JobStatus.COMPLETED
        return JobOutcome(status, result, pages_fetched=len(records))

    async def _watch(self, claim: Claim) -> None:
        while True:
            await asyncio.sleep(2)
            if not await self.store.renew(claim):
                return

    async def execute(self, claim: Claim) -> None:
        with bind_trace(claim.trace_id, claim.request_id or claim.job_id.hex):
            trace_event("job.execute", status=claim.kind.value)
            await self._execute(claim)

    async def _execute(self, claim: Claim) -> None:
        work = asyncio.create_task(self.handlers[claim.kind](claim))
        watch = asyncio.create_task(self._watch(claim))
        try:
            done, _ = await asyncio.wait((work, watch), return_when=asyncio.FIRST_COMPLETED)
            if watch in done:
                watch.result()
                outcome = JobOutcome(JobStatus.CANCELLED)
            else:
                try:
                    outcome = work.result()
                except Exception:
                    logger.warning("任务执行失败，任务标识 %s", claim.job_id)
                    outcome = JobOutcome(
                        JobStatus.FAILED,
                        error=JobError(code="JOB_FAILED", message="后台任务执行失败"),
                    )
            if not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            saved = await self.store.finish(claim, outcome)
            trace_event("job.finish", status=outcome.status.value if saved else "lease_lost")
        finally:
            for task in (work, watch):
                if not task.done():
                    task.cancel()
            await asyncio.gather(work, watch, return_exceptions=True)

    async def run(self) -> None:
        while True:
            try:
                claim = await self.store.claim(
                    tuple(self.handlers), allow_dynamic=self.dynamic_ready
                )
                if claim is not None:
                    await self.execute(claim)
                    continue
            except (SQLAlchemyError, OSError, TimeoutError):
                logger.warning("后台队列暂不可用")
            await asyncio.sleep(0.5)


async def heartbeat(redis: Redis, worker: Worker) -> None:
    while True:
        renderer = worker.evidence.renderer
        worker.dynamic_ready = renderer is not None and await renderer.ready()
        try:
            async with worker.store.db.connect() as connection:
                await connection.execute(text("SELECT 1"))
            async with redis.pipeline() as pipeline:
                for kind in worker.handlers:
                    pipeline.set(f"multisearch:worker:{kind.value}", "ready", ex=10)
                if worker.dynamic_ready:
                    pipeline.set(HEARTBEAT_KEY, "ready", ex=10)
                await pipeline.execute()
            await asyncio.to_thread(HEALTH_FILE.write_text, str(time()), encoding="ascii")
        except (RedisError, SQLAlchemyError, OSError, TimeoutError):
            logger.warning("无法更新后台任务就绪状态")
        await asyncio.sleep(3)


async def main() -> None:
    redact_http_client_logging()
    settings = Settings.from_env()
    db = create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"timeout": 2, "command_timeout": 3},
    )
    redis = Redis.from_url(
        settings.redis_url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2
    )
    try:
        async with (
            httpx.AsyncClient(trust_env=False, follow_redirects=False) as client,
            FetchMetrics.connect(settings.redis_url) as fetch_metrics,
            DynamicRenderer.connect(
                settings.renderer_token if settings.enable_dynamic_fetch else None
            ) as renderer,
        ):
            evidence = build_evidence(settings, client, fetch_metrics, renderer=renderer)
            store = JobStore(db, settings.max_concurrent)
            worker = Worker(store, evidence)
            if settings.enable_research:
                identity = IdentityStore(db, settings.key_pepper)
                ops = OpsStore(
                    db,
                    redis,
                    identity.sessions,
                    settings.key_pepper,
                    baseline=settings.deployment_policy.baseline(),
                )
                search = await build_search(settings, db, redis, client, ops, evidence)
                model = create_research_model(
                    client,
                    backend=settings.research_model_backend,
                    base_url=settings.research_model_base_url,
                    model=settings.research_model_name,
                    api_key=settings.research_model_api_key,
                )
                price_fields = {
                    "currency": settings.research_cost_currency,
                    "input_per_million_tokens": settings.research_input_per_million_tokens,
                    "output_per_million_tokens": settings.research_output_per_million_tokens,
                }
                pricing = (
                    ResearchPricing.model_validate(price_fields)
                    if any(price_fields.values())
                    else None
                )
                orchestrator = ResearchOrchestrator(search, store, model, pricing=pricing)

                async def research(claim: Claim) -> JobOutcome:
                    await load_search_policies(search, db, ops)
                    return await orchestrator.run(claim)

                worker.handlers[JobKind.RESEARCH] = research
            async with asyncio.TaskGroup() as group:
                group.create_task(heartbeat(redis, worker))
                group.create_task(worker.run())
                group.create_task(worker.run())
    finally:
        await redis.aclose()
        await db.dispose()


def healthcheck() -> int:
    try:
        return int(time() - float(HEALTH_FILE.read_text(encoding="ascii")) > 10)
    except (OSError, ValueError):
        return 1


if __name__ == "__main__":
    if "--healthcheck" in sys.argv[1:]:
        raise SystemExit(healthcheck())
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
