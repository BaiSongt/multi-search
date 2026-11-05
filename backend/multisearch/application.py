from asyncio import CancelledError, timeout_at
from time import monotonic

from sqlalchemy.exc import SQLAlchemyError

from .contracts import Mode, SearchRequest, SearchResponse
from .errors import ApiError
from .identity import MachineIdentity
from .metering import SearchPrincipal, UsageMeter
from .search import SearchService
from .tracing import bind_trace, trace_id


class SearchApplication:
    def __init__(self, service: SearchService, meter: UsageMeter, *, telemetry=None):
        self.service = service
        self.meter = meter
        self.telemetry = telemetry

    async def machine_search(
        self, body: SearchRequest, identity: MachineIdentity, request_id: str
    ) -> SearchResponse:
        identity.require_search(body.modalities)
        if (body.require_evidence or body.mode == Mode.EVIDENCE) and "fetch" not in identity.scopes:
            raise ApiError(403, "FORBIDDEN", "证据检索需要 fetch 作用域")
        return await self.search(
            body,
            SearchPrincipal(
                identity.tenant_id, identity.owner_id, identity.key_id, "key", identity.limits
            ),
            request_id,
        )

    async def search(
        self, body: SearchRequest, principal: SearchPrincipal, request_id: str
    ) -> SearchResponse:
        started = monotonic()
        try:
            with bind_trace(trace_id(), request_id):
                return await self._search(body, principal, request_id, started)
        except CancelledError:
            if self.telemetry:
                self.telemetry.record_search_failure(
                    body.mode,
                    "cancelled",
                    "REQUEST_CANCELLED",
                    body.modalities,
                    int((monotonic() - started) * 1000),
                )
            raise
        except ApiError as error:
            if self.telemetry:
                self.telemetry.record_search_failure(
                    body.mode,
                    "failed",
                    error.code,
                    body.modalities,
                    int((monotonic() - started) * 1000),
                )
            raise

    async def _search(
        self,
        body: SearchRequest,
        principal: SearchPrincipal,
        request_id: str,
        started: float,
    ) -> SearchResponse:
        routing = self.service.routing
        mode, budget = self.service.plan(body, routing=routing)
        deadline = started + budget.max_latency_ms / 1000
        try:
            async with timeout_at(deadline):
                admission = await self.meter.reserve(
                    principal, body, mode, request_id, budget.max_latency_ms
                )
        except TimeoutError:
            raise ApiError(504, "TIME_BUDGET_EXCEEDED", "检索时限已耗尽", retryable=True) from None
        except (SQLAlchemyError, OSError):
            raise ApiError(503, "USAGE_UNAVAILABLE", "计量服务暂不可用", retryable=True) from None
        if admission.replay:
            if "error" in admission.replay:
                raise ApiError(**admission.replay["error"])
            replay = SearchResponse.model_validate(admission.replay["response"])
            if self.telemetry:
                self.telemetry.record_search(replay, replay=True, source_types=body.source_types)
            return replay

        try:
            response = await self.service.search(
                body,
                str(principal.tenant_id),
                request_id,
                metered=True,
                deadline=deadline,
                routing=routing,
            )
        except CancelledError:
            try:
                await self.meter.cancel(admission)
            except (SQLAlchemyError, OSError):
                pass
            raise
        except ApiError as error:
            if self.telemetry:
                self.telemetry.record_provider_runs(error.provider_runs)
            await self.meter.finish(
                admission, error=error, elapsed_ms=int((monotonic() - started) * 1000)
            )
            raise
        except Exception:
            await self.meter.finish(
                admission, error=ApiError(500, "INTERNAL_ERROR", "内部服务错误")
            )
            raise
        response = response.model_copy(update={"elapsed_ms": int((monotonic() - started) * 1000)})
        try:
            await self.meter.finish(admission, response=response)
        except (SQLAlchemyError, OSError):
            raise ApiError(503, "USAGE_UNAVAILABLE", "计量服务暂不可用", retryable=True) from None
        if self.telemetry:
            self.telemetry.record_search(response, source_types=body.source_types)
        return response
