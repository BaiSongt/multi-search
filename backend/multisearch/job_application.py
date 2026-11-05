from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError

from .contracts import Mode, Scope, SearchRequest, Source
from .errors import ApiError
from .identity import MachineIdentity
from .job_contracts import FetchRequest, JobAccepted, JobKind, JobView
from .job_store import JobStore
from .metering import SearchPrincipal
from .research import research_budget
from .safe_fetch import validate_fetch_url
from .search import SearchService
from .sessions import SessionIdentity


async def worker_ready(redis: Redis, kind: JobKind) -> bool:
    try:
        return bool(await redis.exists(f"multisearch:worker:{kind.value}"))
    except (RedisError, OSError, TimeoutError):
        return False


def _machine(identity: MachineIdentity) -> SearchPrincipal:
    return SearchPrincipal(
        identity.tenant_id, identity.owner_id, identity.key_id, "key", identity.limits
    )


def _kinds(scopes: frozenset[str]) -> tuple[JobKind, ...]:
    kinds = tuple(kind for kind in JobKind if kind.value in scopes)
    if not kinds:
        raise ApiError(403, "FORBIDDEN", "API Key 缺少抓取或研究权限")
    return kinds


class JobApplication:
    def __init__(
        self, store: JobStore, redis: Redis, search: SearchService, *, enable_research: bool
    ):
        self.store = store
        self.redis = redis
        self.search = search
        self.enable_research = enable_research

    async def available(self, kind: JobKind) -> bool:
        return (kind != JobKind.RESEARCH or self.enable_research) and await worker_ready(
            self.redis, kind
        )

    async def _submit(
        self,
        kind: JobKind,
        principal: SearchPrincipal,
        body: FetchRequest | SearchRequest,
        request_id: str,
        budget: dict[str, int],
        total: int,
        *,
        session: bool = False,
        fingerprint_body: SearchRequest | None = None,
    ) -> JobAccepted:
        ready = await self.available(kind)
        if not ready and body.idempotency_key is None:
            raise ApiError(503, "WORKER_UNAVAILABLE", "后台 Worker 暂不可用", retryable=True)
        excludes = {"request_id", "idempotency_key"}
        job = await self.store.create(
            principal,
            kind,
            body.model_dump(mode="json", exclude=excludes),
            request_id,
            body.idempotency_key,
            budget["max_latency_ms"],
            total,
            worker_available=ready,
            idempotency_payload=(
                fingerprint_body.model_dump(mode="json", exclude=excludes)
                if fingerprint_body
                else None
            ),
        )
        prefix = "/api/v1/console/jobs" if session else "/api/v1/jobs"
        return JobAccepted(
            job_id=job.job_id,
            trace_id=job.trace_id,
            status=job.status,
            status_url=f"{prefix}/{job.job_id}",
            budget=budget,
        )

    async def fetch(
        self, body: FetchRequest, identity: MachineIdentity, request_id: str
    ) -> JobAccepted:
        if "fetch" not in identity.scopes:
            raise ApiError(403, "FORBIDDEN", "API Key 缺少 fetch 作用域")
        if body.network_scope != Scope.PUBLIC:
            raise ApiError(422, "SOURCE_UNAVAILABLE", "抓取仅支持公开网络")
        for url in body.targets():
            validate_fetch_url(url, body.domain_allowlist, body.domain_denylist)
        return await self._submit(
            JobKind.FETCH,
            _machine(identity),
            body,
            request_id,
            {"max_latency_ms": body.max_latency_ms, "max_pages": body.max_pages},
            len(body.targets()),
        )

    async def _research(
        self,
        body: SearchRequest,
        principal: SearchPrincipal,
        request_id: str,
        *,
        session: bool = False,
    ) -> JobAccepted:
        if not self.enable_research:
            raise ApiError(422, "MODE_UNAVAILABLE", "当前部署未启用深度研究")
        if body.mode not in {Mode.AUTO, Mode.DEEP}:
            raise ApiError(422, "INVALID_REQUEST", "研究任务仅接受 auto 或 deep 模式")
        local_only = body.source_types == [Source.LOCAL]
        if body.network_scope != Scope.PUBLIC and not local_only:
            raise ApiError(422, "SOURCE_UNAVAILABLE", "研究任务仅支持公开网络")
        if local_only:
            if body.require_evidence:
                raise ApiError(422, "EVIDENCE_UNAVAILABLE", "本地研究不执行公网证据抓取")
            body = body.model_copy(
                update={
                    "network_scope": Scope.OFFLINE,
                    "max_pages": 0,
                    "max_llm_tokens": 0,
                    "max_image_metadata_fetches": 0,
                }
            )
        self.search.plan(body.model_copy(update={"mode": Mode.STANDARD, "require_evidence": False}))
        normalized = body.model_copy(update={"mode": Mode.DEEP})
        effective = self.search.settings.deployment_policy.apply_image_options(normalized)
        budget = research_budget(effective)
        return await self._submit(
            JobKind.RESEARCH,
            principal,
            effective,
            request_id,
            budget.model_dump(),
            budget.max_search_rounds,
            session=session,
            fingerprint_body=normalized if effective is not normalized else None,
        )

    async def research(
        self,
        body: SearchRequest,
        identity: MachineIdentity,
        request_id: str,
    ) -> JobAccepted:
        if "research" not in identity.scopes:
            raise ApiError(403, "FORBIDDEN", "API Key 缺少 research 作用域")
        identity.require_search(body.modalities)
        return await self._research(body, _machine(identity), request_id)

    async def research_session(
        self,
        body: SearchRequest,
        actor: SessionIdentity,
        request_id: str,
    ) -> JobAccepted:
        actor.require_key_manager()
        return await self._research(
            body,
            SearchPrincipal(actor.tenant_id, actor.user_id, actor.user_id, "session"),
            request_id,
            session=True,
        )

    async def get(self, job_id: UUID, identity: MachineIdentity) -> JobView:
        return await self.store.get(job_id, identity.tenant_id, _kinds(identity.scopes))

    async def cancel(self, job_id: UUID, identity: MachineIdentity) -> JobView:
        return await self.store.cancel(job_id, identity.tenant_id, _kinds(identity.scopes))
