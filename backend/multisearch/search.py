import asyncio
import json
from collections import Counter, deque
from dataclasses import asdict, dataclass, field, replace
from hashlib import sha256
from random import uniform
from time import monotonic
from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from .contracts import (
    ImageSearchResult,
    Modality,
    Mode,
    ProviderRun,
    Scope,
    SearchBudget,
    SearchRequest,
    SearchResponse,
    Source,
    TextSearchResult,
)
from .errors import ApiError
from .evidence import EvidenceBuilder
from .evidence_contracts import EvidenceRecord
from .image_attribution import ImageAttributionResolver
from .image_source import PublicImageSourceResolver
from .pdf_parser import OCRPending
from .pipeline import fuse, select_results
from .providers import Provider, ProviderCapabilities, ProviderFailure, ProviderResult
from .routing_policy import (
    DEFAULT_ROUTING,
    RoutingRules,
    RoutingVersion,
    cache_epoch_signature,
    cache_ttl,
    resolve_mode,
    search_limits,
)
from .safe_fetch import FetchError
from .settings import Settings
from .tracing import trace_event, trace_id
from .vertical_providers import AcademicIdentifier, academic_identifier

if TYPE_CHECKING:
    from .local_index import LocalIndex

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

    def admit_attempt(self, now: float, limit: int) -> bool:
        while self.attempt_starts and now - self.attempt_starts[0] >= _PROVIDER_WINDOW_SECONDS:
            self.attempt_starts.popleft()
        if len(self.attempt_starts) >= limit:
            return False
        self.attempt_starts.append(now)
        return True


@dataclass
class Flight:
    task: asyncio.Task[SearchResponse]
    waiters: int = 0


def _budget(request: SearchRequest, mode: Mode, rules: RoutingRules) -> SearchBudget:
    latency, count = (45_000, 30) if mode == Mode.EVIDENCE else search_limits(rules, mode)
    max_results = min(request.max_results or count, count)
    strict_images = (
        request.image_options and request.image_options.license_policy != "any_with_status"
    )
    metadata_limit = request.max_image_metadata_fetches
    if metadata_limit is None:
        metadata_limit = 3 if strict_images or mode == Mode.EVIDENCE else 0
    return SearchBudget(
        max_latency_ms=min(request.max_latency_ms or latency, latency),
        max_results=max_results,
        max_image_results=min(request.max_image_results or max_results, max_results),
        search_rounds=0,
        max_pages=min(8, request.max_pages if request.max_pages is not None else 8)
        if mode == Mode.EVIDENCE and Modality.TEXT in request.modalities
        else 0,
        max_image_metadata_fetches=min(5, metadata_limit)
        if Modality.IMAGE in request.modalities
        else 0,
    )


def _time_error(runs: tuple[ProviderRun, ...] = ()) -> ApiError:
    return ApiError(
        504, "TIME_BUDGET_EXCEEDED", "检索时限已耗尽", retryable=True, provider_runs=runs
    )


class SearchService:
    def __init__(
        self,
        redis: Redis,
        providers: list[Provider],
        settings: Settings,
        attribution: ImageAttributionResolver | None = None,
        evidence: EvidenceBuilder | None = None,
        image_source: PublicImageSourceResolver | None = None,
        local_index: "LocalIndex | None" = None,
    ):
        self.redis = redis
        self.providers = {provider.provider_id: provider for provider in providers}
        self.provider_policies = {
            provider.provider_id: settings.deployment_policy.provider(provider.provider_id)
            for provider in providers
        }
        self.settings = settings
        self.attribution = attribution
        self.evidence = evidence
        self.image_source = image_source
        self.local_index = local_index
        self.routing = DEFAULT_ROUTING
        self.load_cache_epochs = None
        self.attribution_slots = asyncio.Semaphore(5)
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

    @staticmethod
    def _accepts_identifier(
        provider: Provider, pair: Pair, identifier: AcademicIdentifier | None
    ) -> bool:
        return (
            identifier is None
            or pair[0] != Source.ACADEMIC
            or getattr(provider, "identifier_kind", identifier.kind) == identifier.kind
        )

    def _supported_pairs(self, identifier: AcademicIdentifier | None = None) -> set[Pair]:
        pairs = {
            pair
            for provider in self.providers.values()
            if self.provider_policies[provider.provider_id].enabled
            for pair in self._pairs(provider)
            if self._accepts_identifier(provider, pair, identifier)
        }
        if self.local_index is not None:
            pairs.add((Source.LOCAL, Modality.TEXT))
        return pairs

    def capabilities(self) -> dict:
        pairs = self._supported_pairs()
        limits = {
            mode: dict(
                zip(
                    ("max_latency_ms", "max_results"),
                    search_limits(self.routing.policy, mode),
                    strict=True,
                )
            )
            for mode in _MODE_LIMITS
        }
        if self.evidence is not None:
            limits[Mode.EVIDENCE] = {"max_latency_ms": 45_000, "max_results": 30}
        return {
            "modes": {
                mode: "available"
                if mode in (*_MODE_LIMITS, Mode.AUTO)
                or (mode == Mode.EVIDENCE and self.evidence is not None)
                else "unavailable"
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
                scope: "available"
                if scope == Scope.PUBLIC or self.local_index is not None
                else "unavailable"
                for scope in Scope
            },
            "combinations": [
                {"source_type": source, "modality": kind} for source, kind in sorted(pairs)
            ],
            "limits": {
                "modes": limits,
                "evidence_max_pages": 8 if self.evidence else 0,
                "tenant_requests_per_minute": self.settings.requests_per_minute,
                "tenant_concurrency": self.tenant_concurrency,
                "process_concurrency": self.settings.max_concurrent,
                "provider_concurrency": _PROVIDER_CONCURRENT,
                "provider_requests_per_minute": _PROVIDER_REQUESTS_PER_MINUTE,
                "max_image_metadata_fetches": 5 if self.attribution else 0,
            },
        }

    def provider_status(self) -> list[dict]:
        now = monotonic()
        providers = []
        for provider_id, state in self.states.items():
            policy = self.provider_policies[provider_id]
            profile = getattr(self.providers[provider_id], "capabilities", None)
            if not isinstance(profile, ProviderCapabilities):
                profile = None
            upstream_limit = None
            if profile:
                upstream_limit = min(
                    _PROVIDER_CONCURRENT, profile.upstream_concurrency or _PROVIDER_CONCURRENT
                )
            pair_health = [
                {
                    "source_type": source,
                    "modality": kind,
                    "status": circuit.health(now, state.retry_until),
                    "last_status": circuit.last_status,
                    "last_elapsed_ms": circuit.last_elapsed_ms,
                    "filters": (
                        asdict(filters)
                        if profile and (filters := profile.filters.get(kind))
                        else None
                    ),
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
                    "status": health if policy.enabled else "disabled",
                    "enabled": policy.enabled,
                    "priority": policy.priority,
                    "requests_per_minute": policy.requests_per_minute,
                    "last_status": state.last_status,
                    "last_elapsed_ms": state.last_elapsed_ms,
                    "network_scope": profile.network_scope if profile else Scope.PUBLIC,
                    "capabilities": {
                        "source_types": sorted({source for source, _ in state.circuits}),
                        "modalities": sorted({kind for _, kind in state.circuits}),
                        "supports_pagination": profile.supports_pagination if profile else None,
                        "requires_api_key": profile.requires_api_key if profile else None,
                        "max_concurrent_requests": _PROVIDER_CONCURRENT,
                        "max_upstream_concurrent_requests": upstream_limit,
                    },
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

    def _cache_key(
        self,
        request: SearchRequest,
        tenant_id: str,
        mode: Mode,
        routing: RoutingVersion,
        epochs: dict[str, int],
    ) -> str:
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
        budget = _budget(request, mode, routing.policy)
        filters.update(
            mode=mode,
            max_results=budget.max_results,
            max_image_results=budget.max_image_results,
            max_image_metadata_fetches=budget.max_image_metadata_fetches,
            max_pages=budget.max_pages,
        )
        policies = sorted(
            (key, policy.enabled, policy.priority, policy.requests_per_minute)
            for key, policy in self.provider_policies.items()
        )
        payload = json.dumps(
            [
                self.cache_signature,
                policies,
                routing.version,
                routing.policy.model_dump(mode="json"),
                cache_epoch_signature(request, epochs),
                filters,
            ],
            sort_keys=True,
            ensure_ascii=False,
        )
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
        rules: RoutingRules,
    ) -> None:
        if response.partial or monotonic() >= deadline:
            return
        ttl = cache_ttl(rules, request, empty=not response.results)
        if not ttl:
            return
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
        routing: RoutingVersion,
        epochs: dict[str, int],
    ) -> SearchResponse:
        for cached_mode in (Mode.STANDARD, Mode.QUICK):
            cached = await self._cache_get(
                self._cache_key(request, tenant_id, cached_mode, routing, epochs), deadline
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
                    policy = self.provider_policies[provider.provider_id]
                    if not policy.enabled:
                        return result, "circuit_open", attempts
                    if not state.admit_attempt(now, policy.requests_per_minute):
                        return result, "rate_limited", attempts
                    attempts += 1
                    try:
                        candidate = await provider.search(request, *pair, limit, remaining)
                    except ProviderFailure as error:
                        status, retry_after = error.status, error.retry_after
                        result.reason_codes = tuple(
                            sorted(set(result.reason_codes + error.reason_codes))
                        )
                        if status == "rate_limited" and retry_after:
                            state.retry_until = max(state.retry_until, monotonic() + retry_after)
                    else:
                        result = replace(
                            candidate,
                            reason_codes=tuple(
                                sorted(set(result.reason_codes + candidate.reason_codes))
                            ),
                        )
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
            # 聚合来源仍交付候选时，部分引擎失败不应触发整个来源熔断。
            delivered = status in {"success", "empty"} or (status == "partial" and result.results)
            if observed and delivered:
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
            reason_codes=list(result.reason_codes),
        )
        trace_event(
            "provider.run",
            status=status,
            elapsed_ms=run.elapsed_ms,
            provider=provider.provider_id,
            reason_codes=run.reason_codes,
        )
        return result, run

    async def _run_pair(
        self,
        request: SearchRequest,
        pair: Pair,
        mode: Mode,
        limit: int,
        deadline: float,
        rules: RoutingRules,
        identifier: AcademicIdentifier | None,
    ) -> list[tuple[ProviderResult, ProviderRun]]:
        preferred = ("ddgs", "searxng") if mode == Mode.QUICK else ("searxng", "ddgs")
        eligible = [
            provider
            for provider in self.providers.values()
            if self.provider_policies[provider.provider_id].enabled
            and pair in self._pairs(provider)
            and self._accepts_identifier(provider, pair, identifier)
        ]
        eligible.sort(
            key=lambda p: (
                -self.provider_policies[p.provider_id].priority,
                preferred.index(p.provider_id) if p.provider_id in preferred else len(preferred),
            )
        )
        if mode in {Mode.STANDARD, Mode.EVIDENCE}:
            return await asyncio.gather(
                *(
                    self._run_provider(provider, request, pair, limit, deadline)
                    for provider in eligible
                )
            )

        runs = []
        if not rules.quick_fallback:
            eligible = eligible[:1]
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

    async def _run_local_index(
        self,
        tenant_id: str,
        request: SearchRequest,
        limit: int,
        deadline: float,
    ) -> list[tuple[ProviderResult, ProviderRun]]:
        started = monotonic()
        results = []
        try:
            async with asyncio.timeout(deadline - started):
                results = await self.local_index.search(tenant_id, request, limit)
            status = "success" if results else "empty"
        except TimeoutError:
            status = "timeout"
        except (SQLAlchemyError, OSError):
            status = "failed"
        return [
            (
                ProviderResult(results),
                ProviderRun(
                    provider_id="local_index",
                    source_type=Source.LOCAL,
                    modality=Modality.TEXT,
                    status=status,
                    elapsed_ms=int((monotonic() - started) * 1000),
                    count=len(results),
                ),
            )
        ]

    async def _execute(
        self,
        request: SearchRequest,
        mode: Mode,
        budget: SearchBudget,
        cache_key: str,
        deadline: float,
        routing: RoutingVersion,
        tenant_id: str,
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
        identifier = (
            academic_identifier(request.query) if Source.ACADEMIC in request.source_types else None
        )
        supported = self._supported_pairs(identifier)
        warnings = [
            f"SOURCE_MODALITY_UNAVAILABLE:{source}:{kind}"
            for source, kind in requested
            if (source, kind) not in supported
        ]
        partial = bool(warnings)
        strict = request.image_options and request.image_options.license_policy != "any_with_status"
        provider_request = request
        if strict:
            provider_request = request.model_copy(
                update={
                    "image_options": request.image_options.model_copy(
                        update={"license_policy": "any_with_status"}
                    )
                }
            )
        provider_deadline = deadline
        if mode == Mode.EVIDENCE:
            now = monotonic()
            provider_deadline = min(deadline, now + min(12, max(0, deadline - now) * 0.55))
        elif self.attribution and budget.max_image_metadata_fetches:
            provider_deadline -= min(2, max(0, deadline - monotonic()) * 0.25)
        executions = [
            self._run_pair(
                provider_request,
                pair,
                mode,
                (max(budget.max_image_results, 10) if strict else budget.max_image_results)
                if pair[1] == Modality.IMAGE
                else budget.max_results,
                provider_deadline,
                routing.policy,
                identifier,
            )
            for pair in requested
            if pair in supported and pair[0] != Source.LOCAL
        ]
        if (
            Source.LOCAL in request.source_types
            and (Source.LOCAL, Modality.TEXT) in supported
            and Modality.TEXT in request.modalities
        ):
            executions.append(
                self._run_local_index(tenant_id, request, budget.max_results, provider_deadline)
            )
        reports = await asyncio.gather(*executions)
        records = [record for pair_records in reports for record in pair_records]
        runs = [run for _, run in records]
        if not any(run.status in _SUCCESS for run in runs):
            if monotonic() >= provider_deadline or any(run.status == "timeout" for run in runs):
                raise _time_error(tuple(runs))
            raise ApiError(
                502,
                "PROVIDER_UNAVAILABLE",
                "当前没有可用的检索来源",
                retryable=True,
                provider_runs=runs,
            )
        for result, run in records:
            warnings.extend(result.warnings)
            if run.status not in {"success", "empty"}:
                partial = True
                warnings.append(
                    f"PROVIDER_{run.status.upper()}:{run.provider_id}:{run.source_type}:{run.modality}"
                )
        candidate_count = sum(len(result.results) for result, _ in records)
        candidates = fuse(
            (result.results for result, _ in records), candidate_count, candidate_count
        )
        metadata_fetches = 0
        resolve_public = mode == Mode.EVIDENCE and self.image_source is not None
        if (self.attribution or resolve_public) and budget.max_image_metadata_fetches:
            targets = []
            for index, result in enumerate(candidates):
                if not isinstance(result, ImageSearchResult):
                    continue
                first_party = self.attribution is not None and self.attribution.supports(result)
                if first_party or (resolve_public and result.image.source_page_url):
                    targets.append((index, result, first_party))
            targets.sort(key=lambda target: not target[2])
            targets = targets[: budget.max_image_metadata_fetches]
            outcomes = await asyncio.gather(
                *(
                    self._resolve_image(result, deadline, first_party, request)
                    for _, result, first_party in targets
                )
            )
            for (index, _, _), (resolved, attempted) in zip(targets, outcomes, strict=True):
                candidates[index] = resolved
                metadata_fetches += attempted
            if not targets:
                warnings.append("IMAGE_ATTRIBUTION_SOURCE_UNSUPPORTED")
            if any(not resolved.image.source_page_verified for resolved, _ in outcomes):
                warnings.append("IMAGE_ATTRIBUTION_INCOMPLETE")
                partial = True
        if strict:
            permitted = {"verified_license"}
            if request.image_options.license_policy == "declared_only":
                permitted.add("declared_unverified")
            candidates = [
                result
                for result in candidates
                if not isinstance(result, ImageSearchResult)
                or result.image.rights_status in permitted
            ]
            if not any(isinstance(result, ImageSearchResult) for result in candidates):
                warnings.append("IMAGE_LICENSE_VERIFICATION_UNAVAILABLE")
        results = select_results(candidates, budget.max_results, budget.max_image_results)
        evidence_records = []
        pages_attempted = 0
        if mode == Mode.EVIDENCE:
            (
                results,
                evidence_records,
                evidence_warnings,
                pages_attempted,
            ) = await self._extract_evidence(results, request, budget, deadline)
            if evidence_warnings:
                warnings.extend(evidence_warnings)
                partial = True
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
            evidence_records=evidence_records,
            routing_policy_version=routing.version,
            warnings=list(dict.fromkeys(warnings)),
            provider_runs=runs,
            budget=budget.model_copy(
                update={
                    "search_rounds": 1,
                    "image_metadata_fetches": metadata_fetches,
                    "pages_fetched": len(evidence_records),
                    "pages_attempted": pages_attempted,
                }
            ),
        )
        if request.cache_policy != "bypass":
            await self._cache_store(cache_key, request, response, deadline, routing.policy)
        return response

    async def _extract_evidence(
        self,
        results: list,
        request: SearchRequest,
        budget: SearchBudget,
        deadline: float,
    ) -> tuple[list, list[EvidenceRecord], list[str], int]:
        targets = [
            (index, result)
            for index, result in enumerate(results)
            if isinstance(result, TextSearchResult)
            and result.url
            and result.source_type != Source.LOCAL
        ]
        warnings = []
        if len(targets) > budget.max_pages:
            warnings.append("EVIDENCE_PAGE_BUDGET_REACHED")

        attempted = 0

        async def extract(
            result: TextSearchResult,
        ) -> EvidenceRecord | Literal["ocr_pending"] | None:
            nonlocal attempted
            remaining = deadline - monotonic()
            if remaining <= 0:
                return None
            attempted += 1
            try:
                record = await self.evidence.extract(
                    result.url,
                    timeout_s=min(remaining, 12),
                    result_id=result.result_id,
                    query=request.query,
                    network_scope=request.network_scope,
                    domain_allowlist=request.domain_allowlist,
                    domain_denylist=request.domain_denylist,
                )
                trace_event("fetch.extract", status="success")
                return record
            except OCRPending:
                trace_event("fetch.extract", status="ocr_pending")
                return "ocr_pending"
            except (FetchError, TimeoutError):
                trace_event("fetch.extract", status="failed")
                return None

        selected = targets[: budget.max_pages]
        extracted = await asyncio.gather(*(extract(result) for _, result in selected))
        records = []
        for (index, result), record in zip(selected, extracted, strict=True):
            if record == "ocr_pending":
                warnings.append("OCR_PENDING")
                results[index] = result.model_copy(
                    update={"evidence_status": "ocr_pending", "evidence_refs": []}
                )
                continue
            if record is None:
                warnings.append("EVIDENCE_EXTRACTION_INCOMPLETE")
                continue
            records.append(record)
            results[index] = result.model_copy(
                update={
                    "evidence_status": "content_extracted",
                    "evidence_refs": [record.evidence_id],
                }
            )
        if Modality.TEXT in request.modalities and not records:
            warnings.append("EVIDENCE_UNAVAILABLE")
        if Modality.IMAGE in request.modalities and not any(
            isinstance(result, ImageSearchResult) and result.image.source_page_verified
            for result in results
        ):
            warnings.append("IMAGE_EVIDENCE_UNAVAILABLE")
        return results, records, warnings, attempted

    async def _resolve_image(
        self,
        result: ImageSearchResult,
        deadline: float,
        first_party: bool,
        request: SearchRequest,
    ) -> tuple[ImageSearchResult, int]:
        attempted = 0
        try:
            async with asyncio.timeout(deadline - monotonic()), self.attribution_slots:
                remaining = deadline - monotonic()
                if remaining > 0:
                    attempted = 1
                    if first_party:
                        resolved = await self.attribution.resolve(result, remaining)
                    else:
                        resolved = await self.image_source.resolve(
                            result,
                            timeout_s=min(remaining, 12),
                            domain_allowlist=request.domain_allowlist,
                            domain_denylist=request.domain_denylist,
                        )
                    return resolved, attempted
        except TimeoutError:
            pass
        return result, attempted

    async def _join(
        self,
        request: SearchRequest,
        mode: Mode,
        budget: SearchBudget,
        cache_key: str,
        deadline: float,
        routing: RoutingVersion,
        tenant_id: str,
    ) -> SearchResponse:
        if request.cache_policy == "bypass":
            return await self._execute(
                request, mode, budget, cache_key, deadline, routing, tenant_id
            )
        key = f"{cache_key}:{budget.max_latency_ms}:{request.cache_policy}"
        flight = self.flights.get(key)
        if flight is None:
            flight = Flight(
                asyncio.create_task(
                    self._execute(request, mode, budget, cache_key, deadline, routing, tenant_id)
                )
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

    def plan(
        self, request: SearchRequest, *, routing: RoutingVersion | None = None
    ) -> tuple[Mode, SearchBudget]:
        policy = (routing or self.routing).policy
        needs_evidence = request.require_evidence or request.mode == Mode.EVIDENCE
        if needs_evidence and (
            self.evidence is None or request.mode not in {Mode.AUTO, Mode.EVIDENCE}
        ):
            raise ApiError(422, "EVIDENCE_UNAVAILABLE", "所选模式无法提取证据")
        if request.mode == Mode.DEEP:
            raise ApiError(422, "MODE_UNAVAILABLE", "当前版本未启用深度研究")
        local_only = request.source_types == [Source.LOCAL] and self.local_index is not None
        if Source.LOCAL in request.source_types and len(request.source_types) != 1:
            raise ApiError(422, "SOURCE_UNAVAILABLE", "本地索引请求必须单独指定 local 来源")
        if request.network_scope != Scope.PUBLIC and not local_only:
            raise ApiError(422, "SOURCE_UNAVAILABLE", "当前版本没有获准使用的内部检索来源")
        if local_only and needs_evidence:
            raise ApiError(422, "EVIDENCE_UNAVAILABLE", "本地索引不执行公网证据抓取")
        identifier = (
            academic_identifier(request.query) if Source.ACADEMIC in request.source_types else None
        )
        supported = self._supported_pairs(identifier)
        if not supported.intersection(
            (source, kind) for source in request.source_types for kind in request.modalities
        ):
            raise ApiError(422, "SOURCE_MODALITY_UNAVAILABLE", "请求的来源与模态组合不可用")
        mode = Mode.EVIDENCE if needs_evidence else resolve_mode(request, policy)
        if local_only and request.mode == Mode.AUTO:
            mode = Mode.LOCAL
        if mode == Mode.LOCAL and not local_only and request.cache_policy != "use":
            raise ApiError(422, "INVALID_REQUEST", "local 模式只支持 cache_policy=use")
        return mode, _budget(request, mode, policy)

    async def search(
        self,
        request: SearchRequest,
        tenant_id: str,
        request_id: str,
        *,
        metered: bool = False,
        deadline: float | None = None,
        routing: RoutingVersion | None = None,
    ) -> SearchResponse:
        started = monotonic()
        if request.idempotency_key is not None and not metered:
            raise ApiError(422, "INVALID_REQUEST", "幂等请求需要启用用量计量")
        routing = routing or self.routing
        mode, budget = self.plan(request, routing=routing)
        trace_event("router.plan", status=mode.value)
        deadline = deadline if deadline is not None else started + budget.max_latency_ms / 1000
        if started >= deadline:
            raise _time_error()

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
            if not metered:
                await self._rate_limit(tenant_id, deadline)
            epochs = {}
            effective_request = request
            has_local_index = Source.LOCAL in request.source_types and self.local_index is not None
            if has_local_index:
                effective_request = request.model_copy(update={"cache_policy": "bypass"})
            elif self.load_cache_epochs and request.cache_policy != "bypass":
                try:
                    async with asyncio.timeout(min(0.08, deadline - monotonic())):
                        epochs = await self.load_cache_epochs(tenant_id)
                except (SQLAlchemyError, RedisError, TimeoutError, OSError):
                    if mode == Mode.LOCAL:
                        raise ApiError(
                            503, "CACHE_UNAVAILABLE", "租户缓存版本暂不可用", retryable=True
                        ) from None
                    effective_request = request.model_copy(update={"cache_policy": "bypass"})
            if mode == Mode.LOCAL and not has_local_index:
                response = await self._local(request, tenant_id, budget, deadline, routing, epochs)
            else:
                cache_key = self._cache_key(effective_request, tenant_id, mode, routing, epochs)
                response = await self._join(
                    effective_request, mode, budget, cache_key, deadline, routing, tenant_id
                )
            return response.model_copy(
                update={
                    "request_id": request_id,
                    "trace_id": trace_id(),
                    "mode_requested": request.mode,
                    "routing_policy_version": routing.version,
                    "elapsed_ms": int((monotonic() - started) * 1000),
                }
            )
        finally:
            self.active -= 1
            self.active_by_tenant[tenant_id] -= 1
            if not self.active_by_tenant[tenant_id]:
                del self.active_by_tenant[tenant_id]
