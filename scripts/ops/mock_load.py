"""通过 Mock Provider 和真实 PostgreSQL/Redis 验证四租户 20 路并发。"""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from math import ceil
from time import monotonic
from uuid import uuid4

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from multisearch.application import SearchApplication
from multisearch.contracts import Modality, Mode, SearchRequest, Source, TextSearchResult
from multisearch.identity import IdentityStore
from multisearch.metering import UsageMeter
from multisearch.providers import ProviderResult
from multisearch.search import SearchService
from multisearch.settings import Settings


class MockProvider:
    provider_id = "ddgs"
    pairs = frozenset({(Source.WEB, Modality.TEXT)})

    async def search(self, request, source, modality, limit, timeout_s):
        await asyncio.sleep(0.002)
        identifier = uuid4().hex
        url = f"https://example.org/{identifier}"
        return ProviderResult(
            [
                TextSearchResult(
                    result_id=identifier,
                    title=request.query,
                    url=url,
                    canonical_url=url,
                    source_type=source,
                    provider_ids=[self.provider_id],
                    retrieved_at=datetime.now(UTC),
                )
            ]
        )


async def measure(rounds: int, redis_url: str | None) -> dict:
    settings = Settings.from_env()
    db = create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_timeout=2,
        connect_args={"timeout": 2, "command_timeout": 2},
        hide_parameters=True,
    )
    redis = Redis.from_url(redis_url or settings.redis_url, decode_responses=True, socket_timeout=2)
    identities = IdentityStore(db, settings.key_pepper)
    router = SearchService(redis, [MockProvider()], settings)
    service = SearchApplication(router, UsageMeter(db, settings.max_concurrent))
    keys = []
    authenticated = []
    durations: list[float] = []
    try:
        for _ in range(4):
            slug = f"mockload-{uuid4().hex[:12]}"
            raw = await identities.issue(slug, f"{slug}@example.invalid", ["search:text"], 1)
            keys.append(raw)
            authenticated.append(await identities.authenticate(raw))

        async def request(key: str, body: SearchRequest, request_id: str):
            started = monotonic()
            actor = await identities.authenticate(key)
            result = await service.machine_search(body, actor, request_id)
            duration = (monotonic() - started) * 1000
            if result.request_id != request_id or len(result.results) != 1:
                raise RuntimeError("存在丢失或关联错误的检索结果")
            return result, duration

        for iteration in range(rounds):
            bodies = [
                SearchRequest(query=f"fixture {iteration}:{number}", mode=Mode.QUICK)
                for number in range(5)
            ]
            results = await asyncio.gather(
                *(request(key, body, uuid4().hex) for key in keys for body in bodies)
            )
            result_ids = {result.results[0].result_id for result, _ in results}
            if len(result_ids) != 20:
                raise RuntimeError("不同租户或不同查询共享了错误结果")
            durations.extend(duration for _, duration in results)
            for index, (result, _) in enumerate(results):
                cached, _ = await request(
                    keys[index // 5],
                    bodies[index % 5].model_copy(update={"mode": Mode.LOCAL}),
                    uuid4().hex,
                )
                if not cached.cache_hit or cached.results != result.results:
                    raise RuntimeError("租户缓存隔离校验失败")
        async with db.connect() as connection:
            persisted = await connection.scalar(
                text("""
                    SELECT count(*) FROM usage_events
                    WHERE principal_id = ANY(CAST(:ids AS UUID[])) AND status = 'completed'
                """),
                {"ids": [actor.key_id for actor in authenticated]},
            )
        if persisted != rounds * 40:
            raise RuntimeError("持久化请求数与调用数不一致")
        elapsed = sorted(durations)
        return {
            "tenants": 4,
            "concurrency": 20,
            "mock_provider_ms": 2,
            "requests_measured": len(elapsed),
            "confirmed_cache_hits": rounds * 20,
            "completed_persisted_events": persisted,
            "p50_ms": round(elapsed[ceil(len(elapsed) * 0.5) - 1], 3),
            "p95_ms": round(elapsed[ceil(len(elapsed) * 0.95) - 1], 3),
            "p99_ms": round(elapsed[ceil(len(elapsed) * 0.99) - 1], 3),
        }
    finally:
        for actor in authenticated:
            await identities.revoke(actor.key_id)
        await redis.aclose()
        await db.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--redis-url")
    args = parser.parse_args()
    if not 1 <= args.rounds <= 5:
        parser.error("rounds 需要 1–5")
    print(json.dumps(asyncio.run(measure(args.rounds, args.redis_url)), ensure_ascii=False))


if __name__ == "__main__":
    main()
