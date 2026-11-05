from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

DEFAULT_QUOTA = {
    "requests_per_minute": 60,
    "requests_per_day": 10_000,
    "requests_per_month": 100_000,
    "concurrency": 5,
    "text_per_day": 10_000,
    "image_per_day": 10_000,
    "local_per_day": 10_000,
    "quick_per_day": 10_000,
    "standard_per_day": 10_000,
    "evidence_per_day": 1_000,
    "deep_per_day": 100,
}
MAX_QUOTA = {
    **{name: 1_000_000 for name in DEFAULT_QUOTA},
    "requests_per_minute": 1_000,
    "concurrency": 5,
}


@dataclass(frozen=True)
class ProviderPolicy:
    enabled: bool = True
    priority: int = 0
    requests_per_minute: int = 120


def default_quota(max_concurrent: int = 5) -> dict[str, int]:
    return {**DEFAULT_QUOTA, "concurrency": min(max_concurrent, DEFAULT_QUOTA["concurrency"])}


def validate_limits(limits: dict[str, int]) -> dict[str, int]:
    for name, value in limits.items():
        if name not in MAX_QUOTA or type(value) is not int or not 0 <= value <= MAX_QUOTA[name]:
            raise ValueError(f"无效配额字段或数值：{name}")
    return limits


def effective_quota(overrides: dict[str, int], max_concurrent: int = 5) -> dict[str, int]:
    quota = {**default_quota(max_concurrent), **overrides}
    quota["concurrency"] = min(quota["concurrency"], max_concurrent)
    return quota


async def tenant_quota(
    connection: AsyncConnection, tenant_id: UUID, max_concurrent: int = 5
) -> dict[str, int]:
    overrides = await connection.scalar(
        text("SELECT quota FROM quota_policies WHERE tenant_id = :tenant"), {"tenant": tenant_id}
    )
    return effective_quota(overrides or {}, max_concurrent)


async def lock_tenant(connection: AsyncConnection, tenant_id: UUID) -> None:
    await connection.execute(
        text(
            "SELECT pg_advisory_xact_lock(hashtextextended(CAST(CAST(:tenant AS UUID) AS TEXT), 173))"
        ),
        {"tenant": tenant_id},
    )
