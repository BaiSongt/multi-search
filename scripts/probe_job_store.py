"""在 Compose API 容器内创建临时数据库，验收任务持久性与租户隔离。"""

import asyncio
import os
import re
import subprocess
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from multisearch.errors import ApiError
from multisearch.evidence_contracts import EvidenceLocator, EvidenceRecord
from multisearch.identity import IdentityStore
from multisearch.job_contracts import JobKind, JobStatus
from multisearch.job_store import JobOutcome, JobStore
from multisearch.metering import SearchPrincipal
from multisearch.settings import Settings
from multisearch.worker import Worker


class ProbeEvidence:
    async def extract(self, url, **kwargs):
        return EvidenceRecord(
            evidence_id="ev_" + "a" * 32,
            origin_url=url,
            canonical_url=url,
            retrieved_at=datetime.now(UTC),
            content_type="text/plain",
            evidence_text="example",
            locator=EvidenceLocator(kind="plain_text", text_offset_start=0, text_offset_end=7),
            content_sha256="1" * 64,
            extraction_method="plain_text",
        )


async def exercise(db, pepper: str):
    identity = IdentityStore(db, pepper)
    jobs = JobStore(db)
    kinds = (JobKind.FETCH,)
    principals = []
    for tenant in ("probe-a", "probe-b"):
        raw = await identity.issue(tenant, f"{tenant}@example.invalid", ["fetch"], 1)
        key = await identity.authenticate(raw)
        principals.append(SearchPrincipal(key.tenant_id, key.owner_id, key.key_id, "key"))
    first, other = principals
    payload = {"url": "https://example.org/a"}

    async def create(idempotency):
        return await jobs.create(first, JobKind.FETCH, payload, uuid4().hex, idempotency, 30_000, 1)

    initial = await create("first")
    replay = await create("first")
    assert replay.job_id == initial.job_id
    for method in (jobs.get, jobs.cancel):
        try:
            await method(initial.job_id, other.tenant_id, kinds)
        except ApiError as error:
            assert error.status == 404
        else:
            raise AssertionError("任务跨租户可见")

    claimed = await jobs.claim(kinds)
    assert claimed is not None and claimed.job_id == initial.job_id
    # 模拟 Worker 退出后没有心跳的租约，不改变生产任务数据。
    async with db.begin() as connection:
        await connection.execute(
            text("""
            UPDATE search_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :id
        """),
            {"id": initial.job_id},
        )
    recovered = await jobs.claim(kinds)
    assert recovered.job_id == initial.job_id and recovered.lease_token != claimed.lease_token
    assert not await jobs.finish(claimed, JobOutcome(JobStatus.COMPLETED))
    await Worker(jobs, ProbeEvidence()).execute(recovered)
    finished = await jobs.get(initial.job_id, first.tenant_id, kinds)
    assert finished.status == JobStatus.COMPLETED and finished.attempts == 2
    assert finished.result["evidence_records"][0]["evidence_text"] == "example"
    async with db.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT pages_fetched FROM usage_events WHERE id = :id"),
                {"id": initial.job_id},
            )
            == 1
        )

    queued = await create("queued")
    assert (await jobs.cancel(queued.job_id, first.tenant_id, kinds)).status == JobStatus.CANCELLED
    running = await create("running")
    claim = await jobs.claim(kinds)
    assert claim.job_id == running.job_id
    assert (
        await jobs.cancel(running.job_id, first.tenant_id, kinds)
    ).status == JobStatus.CANCELLING
    assert not await jobs.renew(claim)
    assert await jobs.finish(claim, JobOutcome(JobStatus.COMPLETED, {"unexpected": True}))
    cancelled = await jobs.get(running.job_id, first.tenant_id, kinds)
    assert cancelled.status == JobStatus.CANCELLED and cancelled.result is None


async def main():
    settings = Settings.from_env()
    name = "multisearch_job_probe_" + uuid4().hex[:16]
    if re.fullmatch(r"multisearch_job_probe_[0-9a-f]{16}", name) is None:
        raise RuntimeError("无效的临时数据库标识")
    root = create_async_engine(
        settings.database_url, isolation_level="AUTOCOMMIT", hide_parameters=True
    )
    url = make_url(settings.database_url).set(database=name).render_as_string(hide_password=False)
    scratch = create_async_engine(url, hide_parameters=True)
    created = False
    try:
        async with root.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{name}"'))
            created = True
        await asyncio.to_thread(
            subprocess.run,
            ["alembic", "upgrade", "head"],
            check=True,
            timeout=30,
            env={**os.environ, "DATABASE_URL": url},
        )
        await exercise(scratch, settings.key_pepper)
        print("通过独立 PostgreSQL 的队列幂等、租户隔离、租约恢复、旧 Worker 隔离、取消与证据计量")
    finally:
        await scratch.dispose()
        try:
            if created:
                async with root.connect() as connection:
                    await connection.execute(text(f'DROP DATABASE "{name}"'))
        finally:
            await root.dispose()


if __name__ == "__main__":
    asyncio.run(main())
