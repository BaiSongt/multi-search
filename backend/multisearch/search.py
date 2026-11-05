import asyncio
import json
from collections import Counter, deque
from dataclasses import dataclass, field
from hashlib import sha256
from random import uniform
from time import monotonic
from typing import Literal

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from .contracts import (
    Modality,
    Mode,
    ProviderRun,
    Scope,
    SearchBudget,
    SearchRequest,
    SearchResponse,
    Source,
)
from .errors import ApiError
from .pipeline import fuse
from .providers import Provider, ProviderFailure, ProviderResult
from .settings import Settings

_MODE_LIMITS = {Mode.LOCAL: (2_000, 30), Mode.QUICK: (4_000, 10), Mode.STANDARD: (12_000, 30)}
_PROVIDER_CONCURRENT = 5
_PROVIDER_REQUESTS_PER_MINUTE = 120
_PROVIDER_WINDOW_SECONDS = 60
_RATE_WINDOW_MS = 60_000
_SUCCESS = {"success", "empty", "partial"}
_RATE_SCRIPT = """
local used = tonumber(redis.call("GET", KEYS[1]) or "0")
if used >= tonumber(ARGV[1]) then
    return math.max(1, redis.call("PTTL", KEYS[1]))
end
if used == 0 then
    redis.call("SET", KEYS[1], 1, "PX", ARGV[2])
else
    redis.call("INCR", KEYS[1])
end
return 0
"""

Pair = tuple[Source, Modality]
RunStatus = Literal[
    "success", "empty", "partial", "failed", "timeout", "rate_limited", "circuit_open"
]


@dataclass
class Circuit:
    failures: int = 0
    open_until: float = 0
    probing: bool = False
    last_status: RunStatus | None = None
    last_elapsed_ms: int | None = None

    def health(self, now: float, retry_until: float) -> str:
        if max(self.open_until, retry_until) > now:
            return "circuit_open"
        if self.open_until:
            return "half_open"
        if self.last_status is None:
            return "unknown"
        return "healthy" if self.last_status in {"success", "empty"} else "degraded"


@dataclass
class ProviderState:
    circuits: dict[Pair, Circuit]
    slots: asyncio.Semaphore = field(
        default_factory=lambda: asyncio.Semaphore(_PROVIDER_CONCURRENT)
    )
    attempt_starts: deque[float] = field(default_factory=deque)
    retry_until: float = 0
    last_status: RunStatus | None = None
    last_elapsed_ms: int | None = None

    def admit_attempt(self, now: float) -> bool:
        while self.attempt_starts and now - self.attempt_starts[0] >= _PROVIDER_WINDOW_SECONDS:
            self.attempt_starts.popleft()
        if len(self.attempt_starts) >= _PROVIDER_REQUESTS_PER_MINUTE:
            return False
        self.attempt_starts.append(now)
        return True


@dataclass
class Flight:
    task: asyncio.Task[SearchResponse]
    waiters: int = 0


def _budget(request: SearchRequest, mode: Mode) -> SearchBudget:
    latency, count = _MODE_LIMITS[mode]
    max_results = min(request.max_results or count, count)
    return SearchBudget(
        max_latency_ms=min(request.max_latency_ms or latency, latency),
        max_results=max_results,
        max_image_results=min(request.max_image_results or max_results, max_results),
        search_rounds=0,
    )


def _resolve_mode(request: SearchRequest) -> Mode:
    if request.mode != Mode.AUTO:
        return request.mode
    if request.max_latency_ms is not None and request.max_latency_ms <= _MODE_LIMITS[Mode.QUICK][0]:
        return Mode.QUICK
    return Mode.STANDARD


def _time_error() -> ApiError:
    return ApiError(504, "TIME_BUDGET_EXCEEDED", "检索时限已耗尽", retryable=True)


class SearchService:
    def __init__(self, redis: Redis, providers: list[Provider], settings: Settings):
        self.redis = redis
        self.providers = {provider.provider_id: provider for provider in providers}
        self.settings = settings
        self.states = {
            provider.provider_id: ProviderState({pair: Circuit() for pair in self._pairs(provider)})
            for provider in providers
        }
        self.active = 0
        self.active_by_tenant: Counter[str] = Counter()
        self.tenant_concurrency = min(settings.max_concurrent, 5)
        self.flights: dict[str, Flight] = {}
        self.cache_signature = (
            settings.searxng_url,
            sorted((p.provider_id, getattr(p, "cache_version", "")) for p in providers),
        )

    def _pairs(self, provider: Provider) -> set[Pair]:
        return {
            pair
            for pair in provider.pairs
            if self.settings.enable_images or pair[1] != Modality.IMAGE
        }

    def _supported_pairs(self) -> set[Pair]:
        return {pair for provider in self.providers.values() for pair in self._pairs(provider)}

    def capabilities(self) -> dict:
        pairs = self._supported_pairs()
        return {
            "modes": {
                mode: "available" if mode in (*_MODE_LIMITS, Mode.AUTO) else "unavailable"
                for mode in Mode
            },
            "source_types": {
                source: "available" if any(s == source for s, _ in pairs) else "unavailable"
                for source in Source
            },
            "modalities": {
                kind: "available" if any(m == kind for _, m in pairs) else "unavailable"
                for kind in Modality
            },
            "network_scopes": {
                scope: "available" if scope == Scope.PUBLIC else "unavailable" for scope in Scope
            },
            "combinations": [
                {"source_type": source, "modality": kind} for source, kind in sorted(pairs)
            ],
            "limits": {
                "modes": {
                    mode: {"max_latency_ms": latency, "max_results": count}
                    for mode, (latency, count) in _MODE_LIMITS.items()
                },
                "tenant_requests_per_minute": self.settings.requests_per_minute,
                "tenant_concurrency": self.tenant_concurrency,
                "process_concurrency": self.settings.max_concurrent,
                "provider_concurrency": _PROVIDER_CONCURRENT,
                "provider_requests_per_minute": _PROVIDER_REQUESTS_PER_MINUTE,
            },
        }

    def provider_status(self) -> list[dict]:
        now = monotonic()
        providers = []
        for provider_id, state in self.states.items():
            pair_health = [
                {
                    "source_type": source,
                    "modality": kind,
                    "status": circuit.health(now, state.retry_until),
                    "last_status": circuit.last_status,
                    "last_elapsed_ms": circuit.last_elapsed_ms,
                }
                for (source, kind), circuit in sorted(state.circuits.items())
            ]
            statuses = {record["status"] for record in pair_health}
            if len(statuses) == 1:
                health = statuses.pop()
            elif statuses <= {"healthy", "unknown"}:
                health = "healthy" if statuses else "unknown"
            else:
                health = "degraded"
            providers.append(
                {
                    "provider_id": provider_id,
                    "status": health,
                    "last_status": state.last_status,
                    "last_elapsed_ms": state.last_elapsed_ms,
                    "network_scope": Scope.PUBLIC,
                    "combinations": [
                        {"source_type": source, "modality": kind}
                        for source, kind in sorted(state.circuits)
                    ],
                    "pair_health": pair_health,
                }
            )
        return providers

    async def _rate_limit(self, tenant_id: str, deadline: float) -> None:
        try:
            async with asyncio.timeout(min(0.5, deadline - monotonic())):
                retry_ms = await self.redis.eval(
                    _RATE_SCRIPT,
                    1,
                    f"multisearch:rate:v1:{tenant_id}",
                    self.settings.requests_per_minute,
                    _RATE_WINDOW_MS,
                )
        except (RedisError, TimeoutError):
            if monotonic() >= deadline:
                raise _time_error() from None
            raise ApiError(
                503, "RATE_LIMITER_UNAVAILABLE", "检索限流服务不可用", retryable=True
            ) from None
        if retry_ms:
            raise ApiError(
                429,
                "RATE_LIMITED",
                "当前租户已达到每分钟检索次数上限",
                retryable=True,
                retry_after_ms=int(retry_ms),
            )

    def _cache_key(self, request: SearchRequest, tenant_id: str, mode: Mode) -> str:
        filters = request.model_dump(
            mode="json",
            exclude={
                "mode",
                "cache_policy",
                "request_id",
                "idempotency_key",
                "max_latency_ms",
                "max_pages",
                "max_search_rounds",
                "max_llm_tokens",
                "max_image_metadata_fetches",
                "max_results",
                "max_image_results",
            },
        )
        budget = _budget(request, mode)
        filters.update(
            mode=mode,
            max_results=budget.max_results,
            max_image_results=budget.max_image_results,
        )
        payload = json.dumps([self.cache_signature, filters], sort_keys=True, ensure_ascii=False)
        digest = sha256(payload.encode()).hexdigest()
        return f"multisearch:search:v1:{tenant_id}:{digest}"

    async def _cache_get(self, key: str, deadline: float) -> SearchResponse | None:
        if monotonic() >= deadline:
            return None
        try:
            async with asyncio.timeout(min(0.08, deadline - monotonic())):
                value = await self.redis.get(key)
            if value is not None:
                return SearchResponse.model_validate_json(value)
        except (RedisError, TimeoutError, ValidationError):
            # 缓存读取失败时继续受预算约束的检索。
            pass
        return None

    async def _cache_store(
        self,
        key: str,
        request: SearchRequest,
        response: SearchResponse,
        deadline: float,
    ) -> None:
        if response.partial or monotonic() >= deadline:
            return
        if not response.results:
            ttl = 30
        elif Source.NEWS in request.source_types:
            ttl = 600
        elif Modality.IMAGE in request.modalities:
            ttl = 1800
        else:
            ttl = 3600
        try:
            async with asyncio.timeout(min(0.08, deadline - monotonic())):
                value = response.model_dump_json(exclude={"provider_runs"})
                await self.redis.set(key, value, ex=ttl)
        except (RedisError, TimeoutError):
            pass

    @staticmethod
    def _cached_response(
        cached: SearchResponse, mode: Mode, budget: SearchBudget
    ) -> SearchResponse:
        return cached.model_copy(
            update={
                "cache_hit": True,
                "mode_executed": mode,
                "provider_runs": [],
                "budget": budget,
            }
        )

    async def _local(
        self,
        request: SearchRequest,
        tenant_id: str,
        budget: SearchBudget,
        deadline: float,
    ) -> SearchResponse:
        for cached_mode in (Mode.STANDARD, Mode.QUICK):
            cached = await self._cache_get(
                self._cache_key(request, tenant_id, cached_mode), deadline
            )
            if cached is not None:
                return self._cached_response(cached, Mode.LOCAL, budget)
        if monotonic() >= deadline:
            raise _time_error()
        return SearchResponse(
            request_id="",
            status="partial",
            mode_requested=request.mode,
            mode_executed=Mode.LOCAL,
            partial=True,
            elapsed_ms=0,
            result_count=0,
            results=[],
            warnings=["LOCAL_CACHE_MISS"],
            budget=budget,
        )

    async def _attempt(
        self,
        provider: Provider,
        state: ProviderState,
        request: SearchRequest,
        pair: Pair,
        limit: int,
        deadline: float,
        *,
        is_probe: bool,
    ) -> tuple[ProviderResult, RunStatus, int]:
        attempts = 0
        result = ProviderResult([])
        status: RunStatus = "timeout"
        circuit = state.circuits[pair]
        try:
            async with asyncio.timeout(deadline - monotonic()), state.slots:
                for index in range(2):
                    now = monotonic()
                    remaining = deadline - now
                    if remaining <= 0:
                        break
                    if max(circuit.open_until, state.retry_until) > now or (
                        circuit.probing and not is_probe
                    ):
                        return result, "circuit_open" if not attempts else status, attempts
                    if not state.admit_attempt(now):
                        return result, "rate_limited", attempts
                    attempts += 1
                    try:
                        result = await provider.search(request, *pair, limit, remaining)
                    except ProviderFailure as error:
                        status, retry_after = error.status, error.retry_after
                        if status == "rate_limited" and retry_after:
                            state.retry_until = max(state.retry_until, monotonic() + retry_after)
                    else:
                        status = (
                            "partial"
                            if result.partial
                            else "success"
                            if result.results
                            else "empty"
                        )
                        return result, status, attempts
                    if index or status not in {"timeout", "rate_limited"}:
                        break
                    delay = max(uniform(0.1, 0.25), retry_after or 0)
                    if delay + 0.15 >= deadline - monotonic():
                        break
                    await asyncio.sleep(delay)
        except TimeoutError:
            status = "timeout"
        except Exception:
            # 适配器的异常信息可能包含完整 URL 或查询。
            status = "failed"
        return result, status, attempts

    async def _run_provider(
        self,
        provider: Provider,
        request: SearchRequest,
        pair: Pair,
        limit: int,
        deadline: float,
    ) -> tuple[ProviderResult, ProviderRun]:
        started = monotonic()
        state = self.states[provider.provider_id]
        circuit = state.circuits[pair]
        if max(circuit.open_until, state.retry_until) > started or circuit.probing:
            result, status, attempts = ProviderResult([]), "circuit_open", 0
        else:
            is_probe = bool(circuit.open_until)
            circuit.probing = is_probe
            try:
                result, status, attempts = await self._attempt(
                    provider,
                    state,
                    request,
                    pair,
                    limit,
                    deadline,
                    is_probe=is_probe,
                )
            finally:
                if is_probe:
                    circuit.probing = False
            # 调用方自设的很短时限不足以判断上游是否故障。
            observed = attempts and (status != "timeout" or monotonic() - started >= 1)
            if observed and status in {"success", "empty"}:
                circuit.failures = 0
                circuit.open_until = 0
            elif observed:
                circuit.failures += 1
                if circuit.failures >= 3 or is_probe:
                    circuit.open_until = monotonic() + 15
        elapsed_ms = int((monotonic() - started) * 1000)
        if attempts and (status != "timeout" or elapsed_ms >= 1000):
            state.last_status = status
            state.last_elapsed_ms = elapsed_ms
            circuit.last_status = status
            circuit.last_elapsed_ms = elapsed_ms
        run = ProviderRun(
            provider_id=provider.provider_id,
            source_type=pair[0],
            modality=pair[1],
            status=status,
            elapsed_ms=elapsed_ms,
            count=len(result.results),
            attempts=attempts,
        )
        return result, run

    async def _run_pair(
        self,
        request: SearchRequest,
        pair: Pair,
        mode: Mode,
        limit: int,
        deadline: float,
    ) -> list[tuple[ProviderResult, ProviderRun]]:
        preferred = ("ddgs", "searxng") if mode == Mode.QUICK else ("searxng", "ddgs")
        eligible = [
            provider for provider in self.providers.values() if pair in self._pairs(provider)
        ]
        eligible.sort(
            key=lambda p: (
                preferred.index(p.provider_id) if p.provider_id in preferred else len(preferred)
            )
        )
        if mode == Mode.STANDARD:
            return await asyncio.gather(
                *(
                    self._run_provider(provider, request, pair, limit, deadline)
                    for provider in eligible
                )
            )

        runs = []
        for index, provider in enumerate(eligible):
            now = monotonic()
            if now >= deadline:
                break
            remaining_providers = len(eligible) - index
            # 快速模式为每个候补来源预留可执行的时间。
            provider_deadline = now + (deadline - now) / remaining_providers
            result, run = await self._run_provider(
                provider, request, pair, limit, provider_deadline
            )
            runs.append((result, run))
            if result.results:
                break
        return runs

    async def _execute(
        self,
        request: SearchRequest,
        mode: Mode,
        budget: SearchBudget,
        cache_key: str,
        deadline: float,
    ) -> SearchResponse:
        if request.cache_policy == "use":
            cached = await self._cache_get(cache_key, deadline)
            if cached is not None:
                return self._cached_response(cached, mode, budget)
        if monotonic() >= deadline:
            raise _time_error()

        requested = [
            (source, modality) for source in request.source_types for modality in request.modalities
        ]
        supported = self._supported_pairs()
        warnings = [
            f"SOURCE_MODALITY_UNAVAILABLE:{source}:{kind}"
            for source, kind in requested
            if (source, kind) not in supported
        ]
        partial = bool(warnings)
        reports = await asyncio.gather(
            *(
                self._run_pair(
                    request,
                    pair,
                    mode,
                    budget.max_image_results if pair[1] == Modality.IMAGE else budget.max_results,
                    deadline,
                )
                for pair in requested
                if pair in supported
            )
        )
        records = [record for pair_records in reports for record in pair_records]
        runs = [run for _, run in records]
        if not any(run.status in _SUCCESS for run in runs):
            if monotonic() >= deadline or any(run.status == "timeout" for run in runs):
                raise _time_error()
            raise ApiError(502, "PROVIDER_UNAVAILABLE", "当前没有可用的检索来源", retryable=True)
        for result, run in records:
            warnings.extend(result.warnings)
            if run.status not in {"success", "empty"}:
                partial = True
                warnings.append(
                    f"PROVIDER_{run.status.upper()}:{run.provider_id}:{run.source_type}:{run.modality}"
                )
        results = fuse(
            (result.results for result, _ in records), budget.max_results, budget.max_image_results
        )
        if partial:
            warnings.append("PARTIAL_RESULT")
        response = SearchResponse(
            request_id="",
            status="partial" if partial else "completed",
            mode_requested=request.mode,
            mode_executed=mode,
            partial=partial,
            elapsed_ms=0,
            result_count=len(results),
            results=results,
            warnings=list(dict.fromkeys(warnings)),
            provider_runs=runs,
            budget=budget.model_copy(update={"search_rounds": 1}),
        )
        if request.cache_policy != "bypass":
            await self._cache_store(cache_key, request, response, deadline)
        return response

    async def _join(
        self,
        request: SearchRequest,
        mode: Mode,
        budget: SearchBudget,
        cache_key: str,
        deadline: float,
    ) -> SearchResponse:
        if request.cache_policy == "bypass":
            return await self._execute(request, mode, budget, cache_key, deadline)
        key = f"{cache_key}:{budget.max_latency_ms}:{request.cache_policy}"
        flight = self.flights.get(key)
        if flight is None:
            flight = Flight(
                asyncio.create_task(self._execute(request, mode, budget, cache_key, deadline))
            )
            self.flights[key] = flight
        flight.waiters += 1
        try:
            return await asyncio.shield(flight.task)
        finally:
            flight.waiters -= 1
            if flight.waiters == 0:
                del self.flights[key]
                if not flight.task.done():
                    flight.task.cancel()
                    await asyncio.gather(flight.task, return_exceptions=True)

    async def search(
        self, request: SearchRequest, tenant_id: str, request_id: str
    ) -> SearchResponse:
        started = monotonic()
        if request.idempotency_key is not None:
            raise ApiError(422, "INVALID_REQUEST", "当前版本不支持 idempotency_key")
        if request.require_evidence or request.mode == Mode.EVIDENCE:
            raise ApiError(422, "EVIDENCE_UNAVAILABLE", "当前版本未启用证据提取")
        if request.mode == Mode.DEEP:
            raise ApiError(422, "MODE_UNAVAILABLE", "当前版本未启用深度研究")
        if request.network_scope != Scope.PUBLIC:
            raise ApiError(422, "SOURCE_UNAVAILABLE", "当前版本没有获准使用的内部检索来源")
        supported = self._supported_pairs()
        if not supported.intersection(
            (source, kind) for source in request.source_types for kind in request.modalities
        ):
            raise ApiError(422, "SOURCE_MODALITY_UNAVAILABLE", "请求的来源与模态组合不可用")
        mode = _resolve_mode(request)
        if mode == Mode.LOCAL and request.cache_policy != "use":
            raise ApiError(422, "INVALID_REQUEST", "local 模式只支持 cache_policy=use")
        budget = _budget(request, mode)
        deadline = started + budget.max_latency_ms / 1000

        if (
            self.active >= self.settings.max_concurrent
            or self.active_by_tenant[tenant_id] >= self.tenant_concurrency
        ):
            raise ApiError(
                429, "RATE_LIMITED", "同时进行的检索过多", retryable=True, retry_after_ms=1000
            )
        self.active += 1
        self.active_by_tenant[tenant_id] += 1
        try:
            await self._rate_limit(tenant_id, deadline)
            if mode == Mode.LOCAL:
                response = await self._local(request, tenant_id, budget, deadline)
            else:
                cache_key = self._cache_key(request, tenant_id, mode)
                response = await self._join(request, mode, budget, cache_key, deadline)
            return response.model_copy(
                update={
                    "request_id": request_id,
                    "mode_requested": request.mode,
                    "elapsed_ms": int((monotonic() - started) * 1000),
                }
            )
        finally:
            self.active -= 1
            self.active_by_tenant[tenant_id] -= 1
            if not self.active_by_tenant[tenant_id]:
                del self.active_by_tenant[tenant_id]
