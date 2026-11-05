from asyncio import timeout
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from .admin_api import router as admin_router
from .api import router
from .application import SearchApplication
from .auth import authenticate_machine, console_origin
from .errors import install_errors
from .evidence_api import router as evidence_router
from .identity import IdentityStore
from .job_api import router as job_router
from .job_application import JobApplication
from .job_contracts import JobKind
from .job_store import JobStore
from .management_identity import router as identity_router
from .mcp_api import MCPJobs, create_mcp, create_mcp_app
from .metering import UsageMeter
from .observability import FetchMetrics, install_observability
from .ops_api import router as ops_router
from .ops_store import OpsStore
from .reporting import UsageReports
from .runtime import build_evidence, build_search
from .sessions import bootstrap_identity
from .settings import Settings
from .tracing import redact_http_client_logging

VERSION = (Path(__file__).resolve().parents[2] / "VERSION").read_text().strip()


def create_app(settings: Settings | None = None) -> FastAPI:
    async def mcp_search(body, identity, request_id):
        return await app.state.application.machine_search(body, identity, request_id)

    async def mcp_authenticate(raw):
        return await authenticate_machine(app.state.identity, raw, telemetry)

    async def mcp_fetch(body, identity, request_id):
        return await app.state.job_application.fetch(body, identity, request_id)

    async def mcp_research(body, identity, request_id):
        return await app.state.job_application.research(body, identity, request_id)

    async def mcp_get(job_id, identity):
        return await app.state.job_application.get(job_id, identity)

    async def mcp_cancel(job_id, identity):
        return await app.state.job_application.cancel(job_id, identity)

    mcp = create_mcp(
        mcp_search,
        version=VERSION,
        jobs=MCPJobs(fetch=mcp_fetch, research=mcp_research, get=mcp_get, cancel=mcp_cancel),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config = settings or Settings.from_env()
        redact_http_client_logging()
        console_origin()
        app.state.settings = config
        app.state.db = create_async_engine(
            config.database_url,
            pool_pre_ping=True,
            pool_timeout=2,
            connect_args={"timeout": 2, "command_timeout": 2},
            hide_parameters=True,
        )
        app.state.identity = IdentityStore(app.state.db, config.key_pepper)
        app.state.redis = Redis.from_url(
            config.redis_url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2
        )
        try:
            await bootstrap_identity(app.state.identity.sessions)
            async with (
                httpx.AsyncClient(trust_env=False, follow_redirects=False) as client,
                FetchMetrics.connect(config.redis_url) as fetch_metrics,
            ):
                meter = UsageMeter(app.state.db, config.max_concurrent)
                ops = OpsStore(
                    app.state.db,
                    app.state.redis,
                    app.state.identity.sessions,
                    config.key_pepper,
                    baseline=config.deployment_policy.baseline(),
                )
                app.state.ops = ops
                app.state.search = await build_search(
                    config,
                    app.state.db,
                    app.state.redis,
                    client,
                    ops,
                    build_evidence(config, client, fetch_metrics),
                )
                app.state.application = SearchApplication(
                    app.state.search, meter, telemetry=telemetry
                )
                app.state.reports = UsageReports(app.state.db, config.max_concurrent)
                app.state.jobs = JobStore(app.state.db, config.max_concurrent)
                app.state.job_application = JobApplication(
                    app.state.jobs,
                    app.state.redis,
                    app.state.search,
                    enable_research=config.enable_research,
                )
                async with meter.maintenance(), mcp.session_manager.run():
                    yield
        finally:
            await app.state.redis.aclose()
            await app.state.db.dispose()

    app = FastAPI(title="MultiSearch", version=VERSION, lifespan=lifespan)
    install_errors(app)
    telemetry = install_observability(app)
    app.include_router(router)
    app.include_router(identity_router)
    app.include_router(admin_router)
    app.include_router(job_router)
    app.include_router(evidence_router)
    app.include_router(ops_router)
    app.router.add_route("/mcp", create_mcp_app(mcp, mcp_authenticate), include_in_schema=False)

    @app.get("/health/live")
    async def live():
        return {"status": "alive", "version": VERSION}

    @app.get("/health/ready")
    async def ready(request: Request):
        degraded = []
        try:
            async with timeout(2):
                async with request.app.state.db.connect() as connection:
                    await connection.execute(text("SELECT id FROM usage_events LIMIT 0"))
                await request.app.state.redis.ping()
                if (
                    request.app.state.settings.enable_research
                    and not await request.app.state.job_application.available(JobKind.RESEARCH)
                ):
                    degraded.append("research_worker")
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ready", "version": VERSION, "degraded": degraded}

    return app
