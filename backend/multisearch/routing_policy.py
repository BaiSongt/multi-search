from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import Modality, Mode, SearchRequest, Source

_HARD_LIMITS = {Mode.LOCAL: (2000, 30), Mode.QUICK: (4000, 10), Mode.STANDARD: (12_000, 30)}


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModeBudget(_FrozenModel):
    max_latency_ms: int = Field(strict=True, ge=100, le=12_000)
    max_results: int = Field(strict=True, ge=1, le=30)


class RoutingBudgets(_FrozenModel):
    local: ModeBudget = ModeBudget(max_latency_ms=2000, max_results=30)
    quick: ModeBudget = ModeBudget(max_latency_ms=4000, max_results=10)
    standard: ModeBudget = ModeBudget(max_latency_ms=12_000, max_results=30)

    @model_validator(mode="after")
    def within_mode_ceilings(self) -> Self:
        for mode, (latency, count) in _HARD_LIMITS.items():
            budget = getattr(self, mode.value)
            if budget.max_latency_ms > latency or budget.max_results > count:
                raise ValueError(f"{mode.value} 的预算不得超过服务上限")
        return self


class CacheTtls(_FrozenModel):
    web: int = Field(default=3600, strict=True, ge=0, le=86_400)
    news: int = Field(default=600, strict=True, ge=0, le=86_400)
    image: int = Field(default=1800, strict=True, ge=0, le=86_400)
    academic: int = Field(default=86_400, strict=True, ge=0, le=86_400)
    code: int = Field(default=3600, strict=True, ge=0, le=86_400)
    negative: int = Field(default=30, strict=True, ge=0, le=120)


class RoutingRules(_FrozenModel):
    budgets: RoutingBudgets = RoutingBudgets()
    auto_quick_max_latency_ms: int = Field(default=4000, strict=True, ge=0, le=4000)
    quick_fallback: bool = Field(default=True, strict=True)
    cache_ttl_seconds: CacheTtls = CacheTtls()

    @model_validator(mode="after")
    def validate_auto_threshold(self) -> Self:
        if self.auto_quick_max_latency_ms > self.budgets.quick.max_latency_ms:
            raise ValueError("自动快速模式阈值不得超过快速模式时限")
        return self


class RoutingVersion(_FrozenModel):
    version: int
    policy: RoutingRules
    created_at: datetime | None
    actor_id: UUID | None
    reason: str
    rollback_of: int | None


DEFAULT_ROUTING = RoutingVersion(
    version=0,
    policy=RoutingRules(),
    created_at=None,
    actor_id=None,
    reason="内置基线",
    rollback_of=None,
)


def resolve_mode(request: SearchRequest, rules: RoutingRules) -> Mode:
    if request.mode != Mode.AUTO:
        return request.mode
    if (
        request.max_latency_ms is not None
        and request.max_latency_ms <= rules.auto_quick_max_latency_ms
    ):
        return Mode.QUICK
    return Mode.STANDARD


def search_limits(rules: RoutingRules, mode: Mode) -> tuple[int, int]:
    budget = getattr(rules.budgets, mode.value)
    return budget.max_latency_ms, budget.max_results


def cache_ttl(rules: RoutingRules, request: SearchRequest, empty: bool) -> int:
    ttls = rules.cache_ttl_seconds
    if empty:
        return ttls.negative
    candidates = [
        getattr(ttls, source.value, ttls.web)
        for source in request.source_types
        if source != Source.LOCAL
    ]
    if Modality.IMAGE in request.modalities:
        candidates.append(ttls.image)
    return min(candidates, default=ttls.web)


def cache_epoch_signature(
    request: SearchRequest, epochs: dict[str, int]
) -> tuple[tuple[str, int], ...]:
    scopes = {"all", *(source.value for source in request.source_types)}
    if Modality.IMAGE in request.modalities:
        scopes.add("image")
    return tuple((scope, epochs.get(scope, 0)) for scope in sorted(scopes))
