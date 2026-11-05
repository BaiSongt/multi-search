"""在 Compose API 容器内使用独立 PostgreSQL 验收本地文档检索。

POSIX shell:
    docker compose exec -T multisearch-api python - < scripts/probe_local_index.py
PowerShell:
    Get-Content -Raw -Encoding UTF8 scripts/probe_local_index.py | docker compose exec -T multisearch-api python -

需要创建临时数据库的权限。探针仅写入独立数据库，完成后删除该库。
"""

import asyncio
import os
import re
import subprocess
from datetime import UTC, datetime
from statistics import median
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from multisearch.application import SearchApplication
from multisearch.contracts import Modality, SearchRequest, Source
from multisearch.errors import ApiError
from multisearch.identity import IdentityStore
from multisearch.local_index import LocalDocument, LocalIndex
from multisearch.metering import UsageMeter
from multisearch.search import SearchService
from multisearch.settings import Settings


class PublicProvider:
    provider_id = "public_probe"
    pairs = {(Source.WEB, Modality.TEXT)}

    async def search(self, *args, **kwargs):
        raise AssertionError("本地检索不得调用公网 Provider")


class SharedCache:
    def __getattr__(self, operation):
        raise AssertionError(f"本地检索不得访问共享缓存：{operation}")


async def exercise(db, settings):
    identity = IdentityStore(db, settings.key_pepper)
    tenants = ("local-probe-a", "local-probe-b")
    keys = []
    for tenant in tenants:
        raw = await identity.issue(tenant, f"{tenant}@example.invalid", ["search:text"], 1)
        keys.append(await identity.authenticate(raw))
    first, other = keys

    index = LocalIndex(db)
    docs = [
        LocalDocument(
            id="restore",
            title="Observatory backup recovery runbook",
            text="The backup recovery checklist verifies the archive and restores the service.",
            source_url="https://engineering.probe.invalid/runbook",
            language="en-GB",
            published_at=datetime(2024, 3, 1, tzinfo=UTC),
        ),
        LocalDocument(
            id="notes",
            title="Observatory shift notes",
            text="The team rehearsed backup recovery once.",
            source_url="https://notes.probe.invalid/shift",
            language="en",
            published_at=datetime(2023, 4, 1, tzinfo=UTC),
        ),
        LocalDocument(id="noise", title="Telemetry dictionary", text="Sampling and aggregation."),
        LocalDocument(
            id="calibration",
            title="仪表校准操作",
            text="仪表校准在每次维护后进行。",
            language="zh-CN",
        ),
        LocalDocument(id="shared", title="A custody ledger", text="Alpha facility record."),
        *[
            LocalDocument(
                id=f"inventory-{number}",
                title=f"Shelfprobe {number:02}",
                text="Inventory fixture with a unique record identifier.",
            )
            for number in range(34)
        ],
    ]
    assert await index.import_documents(tenants[0], docs) == 39
    assert (
        await index.import_documents(
            tenants[1],
            [LocalDocument(id="shared", title="B custody ledger", text="Beta facility record.")],
        )
        == 1
    )

    service = SearchService(SharedCache(), [PublicProvider()], settings, local_index=index)
    app = SearchApplication(service, UsageMeter(db, settings.max_concurrent))
    elapsed = []

    async def search(key, query, scope="offline", **options):
        body = SearchRequest(query=query, source_types=["local"], network_scope=scope, **options)
        response = await app.machine_search(body, key, uuid4().hex)
        assert response.status == "completed" and response.mode_executed == "local"
        assert not response.cache_hit and response.next_cursor is None
        assert [run.provider_id for run in response.provider_runs] == ["local_index"]
        assert all(hit.source_type == "local" for hit in response.results)
        elapsed.append(response.elapsed_ms)
        return response.results

    ranked = await search(first, "backup recovery")
    assert [hit.title for hit in ranked] == [
        "Observatory backup recovery runbook",
        "Observatory shift notes",
    ]
    assert "verifies the archive" in ranked[0].snippet
    assert await search(other, "backup recovery", "private") == []
    [cjk] = await search(first, "仪表校准", language="zh-TW")
    assert cjk.title == "仪表校准操作" and cjk.language == "zh-CN"

    [filtered] = await search(
        first,
        "backup recovery",
        "private",
        language="en-US",
        domain_allowlist=["probe.invalid"],
        time_range={"from": "2024-01-01", "to": "2024-12-31"},
    )
    assert filtered.result_id == ranked[0].result_id
    [denied] = await search(first, "backup recovery", domain_denylist=["engineering.probe.invalid"])
    assert denied.result_id == ranked[1].result_id

    top = await search(first, "shelfprobe", max_results=3)
    capped = await search(first, "shelfprobe", "private", max_results=30)
    assert len(top) == 3 and len({hit.result_id for hit in capped}) == 30
    assert [hit.result_id for hit in top] == [hit.result_id for hit in capped[:3]]

    rows_a, rows_b = await asyncio.gather(
        search(first, "custody ledger", "private", idempotency_key="shared-probe-key"),
        search(other, "custody ledger", "private", idempotency_key="shared-probe-key"),
    )
    [a], [b] = rows_a, rows_b
    assert (a.title, b.title) == ("A custody ledger", "B custody ledger")
    assert a.result_id != b.result_id

    await index.import_documents(
        tenants[0],
        [LocalDocument(id="shared", title="A custody ledger revised", text="New entry.")],
    )
    [fresh] = await search(first, "custody ledger", "private")
    [unchanged] = await search(other, "custody ledger", "private")
    assert fresh.result_id == a.result_id and fresh.title == "A custody ledger revised"
    assert unchanged.result_id == b.result_id and unchanged.title == b.title

    for scope in ("offline", "private"):
        for sources in (["web"], ["local", "web"]):
            body = SearchRequest(query="blocked search", source_types=sources, network_scope=scope)
            try:
                await app.machine_search(body, first, uuid4().hex)
            except ApiError as error:
                assert error.status == 422 and error.code == "SOURCE_UNAVAILABLE"
            else:
                raise AssertionError("离线或私网检索应拒绝公网来源")
    print(f"通过 {len(elapsed)} 次本地检索和 4 次公网阻断，P50 {median(elapsed):g} ms")


async def main():
    settings = Settings.from_env()
    name = "multisearch_local_probe_" + uuid4().hex[:16]
    if re.fullmatch(r"multisearch_local_probe_[0-9a-f]{16}", name) is None:
        raise RuntimeError("无效的临时数据库标识")
    connection_options = {
        "hide_parameters": True,
        "connect_args": {"timeout": 8, "command_timeout": 10},
    }
    root = create_async_engine(
        settings.database_url, isolation_level="AUTOCOMMIT", **connection_options
    )
    url = make_url(settings.database_url).set(database=name).render_as_string(hide_password=False)
    scratch = create_async_engine(url, **connection_options)
    created = False
    try:
        async with asyncio.timeout(60):
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
            await exercise(scratch, settings)
    finally:
        try:
            async with asyncio.timeout(15):
                await scratch.dispose()
                if created:
                    async with root.connect() as connection:
                        await connection.execute(text(f'DROP DATABASE "{name}"'))
        finally:
            await root.dispose()


if __name__ == "__main__":
    asyncio.run(main())
