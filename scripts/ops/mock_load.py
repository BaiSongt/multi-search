"""通过 Mock Provider 和真实 PostgreSQL/Redis 验证四租户 20 路并发。"""

import argparse
import asyncio
import json
from contextvars import ContextVar
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

_timings: ContextVar[dict[str, float]] = ContextVar("probe_timings")


async def timed(stage, operation):
    started = monotonic()
    try:
        return await operation
    finally:
        _timings.get()[stage] = (monotonic() - started) * 1000


class TimedMeter(UsageMeter):
    async def reserve(self, *args, **kwargs):
        return await timed("admission", super().reserve(*args, **kwargs))

    async def finish(self, *args, **kwargs):
        return await timed("finish", super().finish(*args, **kwargs))


class TimedSearch(SearchService):
    async def search(self, *args, **kwargs):
        return await timed("search", super().search(*args, **kwargs))


def percentiles(values: list[float]) -> dict[str, float]:
    elapsed = sorted(values)
    return {
        f"p{percentile}_ms": round(elapsed[ceil(len(elapsed) * percentile / 100) - 1], 3)
        for percentile in (50, 95, 99)
    }


def summarize(samples: list[dict[str, float]]) -> dict:
    return {
        "samples": len(samples),
        **percentiles([sample["total"] for sample in samples]),
        "stages": {
            stage: percentiles([sample[stage] for sample in samples])
            for stage in ("auth", "admission", "search", "finish")
        },
    }


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


async def measure(
    rounds: int,
    redis_url: str | None,
    *,
    warmup_rounds: int = 0,
    concurrent_cache_hits: bool = False,
) -> dict:
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
    router = TimedSearch(redis, [MockProvider()], settings)
    service = SearchApplication(router, TimedMeter(db, settings.max_concurrent))
    keys = []
    authenticated = []
    cold_samples: list[dict[str, float]] = []
    cache_samples: list[dict[str, float]] = []
    batches = []
    try:
        for _ in range(4):
            slug = f"mockload-{uuid4().hex[:12]}"
            raw = await identities.issue(slug, f"{slug}@example.invalid", ["search:text"], 1)
            keys.append(raw)
            authenticated.append(await identities.authenticate(raw))

        async def request(key: str, body: SearchRequest, request_id: str):
            timing: dict[str, float] = {}
            token = _timings.set(timing)
            started = monotonic()
            try:
                actor = await timed("auth", identities.authenticate(key))
                result = await service.machine_search(body, actor, request_id)
                timing["total"] = (monotonic() - started) * 1000
            finally:
                _timings.reset(token)
            if result.request_id != request_id or len(result.results) != 1:
                raise RuntimeError("存在丢失或关联错误的检索结果")
            return result, timing

        for iteration in range(rounds + warmup_rounds):
            bodies = [
                SearchRequest(query=f"fixture {iteration}:{number}", mode=Mode.QUICK)
                for number in range(5)
            ]
            results = await asyncio.gather(
                *(request(key, body, uuid4().hex) for key in keys for body in bodies)
            )
            result_ids = {result.results[0].result_id for result, _ in results}
            if len(result_ids) != 20 or any(result.cache_hit for result, _ in results):
                raise RuntimeError("不同租户或不同查询共享了错误结果")
            cache_requests = (
                request(
                    keys[index // 5],
                    bodies[index % 5].model_copy(update={"mode": Mode.LOCAL}),
                    uuid4().hex,
                )
                for index in range(len(results))
            )
            hits = (
                await asyncio.gather(*cache_requests)
                if concurrent_cache_hits
                else [await hit for hit in cache_requests]
            )
            for (result, _), (cached, _) in zip(results, hits, strict=True):
                if not cached.cache_hit or cached.results != result.results:
                    raise RuntimeError("租户缓存隔离校验失败")
            if iteration >= warmup_rounds:
                cold_batch = [sample for _, sample in results]
                cache_batch = [sample for _, sample in hits]
                cold_samples.extend(cold_batch)
                cache_samples.extend(cache_batch)
                batches.append({"cold": summarize(cold_batch), "cache_hit": summarize(cache_batch)})
        async with db.connect() as connection:
            persisted = await connection.scalar(
                text("""
                    SELECT count(*) FROM usage_events
                    WHERE principal_id = ANY(CAST(:ids AS UUID[])) AND status = 'completed'
                """),
                {"ids": [actor.key_id for actor in authenticated]},
            )
        if persisted != (rounds + warmup_rounds) * 40:
            raise RuntimeError("持久化请求数与调用数不一致")
        cold = summarize(cold_samples)
        return {
            "tenants": 4,
            "concurrency": 20,
            "mock_provider_ms": 2,
            "requests_measured": len(cold_samples),
            "confirmed_cache_hits": len(cache_samples),
            "completed_persisted_events": persisted,
            **{key: cold[key] for key in ("p50_ms", "p95_ms", "p99_ms")},
            "warmup_rounds": warmup_rounds,
            "cache_hit_concurrency": 20 if concurrent_cache_hits else 1,
            "cold": cold,
            "cache_hit": summarize(cache_samples),
            "batches": batches,
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
    parser.add_argument(
        "--warmup-rounds", type=int, choices=(0, 1), default=0, help="预热轮次，不纳入延迟样本"
    )
    parser.add_argument(
        "--concurrent-cache-hits", action="store_true", help="以 20 并发测量缓存命中"
    )
    args = parser.parse_args()
    if not 1 <= args.rounds <= 5:
        parser.error("rounds 需要 1–5")
    report = asyncio.run(
        measure(
            args.rounds,
            args.redis_url,
            warmup_rounds=args.warmup_rounds,
            concurrent_cache_hits=args.concurrent_cache_hits,
        )
    )
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
