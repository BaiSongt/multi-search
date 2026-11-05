from asyncio import timeout
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from .admin_api import router as admin_router
from .api import router
from .application import SearchApplication
from .auth import console_origin
from .errors import ApiError, install_errors
from .identity import IdentityStore
from .image_attribution import ImageAttributionResolver
from .management_identity import router as identity_router
from .mcp_api import create_mcp, create_mcp_app
from .metering import UsageMeter
from .policy import ProviderPolicy
from .providers import create_providers
from .reporting import UsageReports
from .search import SearchService
from .sessions import bootstrap_identity
from .settings import Settings

VERSION = (Path(__file__).resolve().parents[2] / "VERSION").read_text().strip()


def create_app(settings: Settings | None = None) -> FastAPI:
    async def mcp_search(body, identity, request_id):
        return await app.state.application.machine_search(body, identity, request_id)

    async def mcp_authenticate(raw):
        try:
            return await app.state.identity.authenticate(raw)
        except (SQLAlchemyError, OSError, TimeoutError):
            raise ApiError(503, "AUTH_UNAVAILABLE", "鉴权服务暂不可用", retryable=True) from None

    mcp = create_mcp(mcp_search, version=VERSION)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config = settings or Settings.from_env()
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
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                app.state.search = SearchService(
                    app.state.redis,
                    create_providers(config, client),
                    config,
                    ImageAttributionResolver(client),
                )
                async with app.state.db.connect() as connection:
                    policies = await connection.execute(
                        text("""
                        SELECT provider_id, enabled, priority, requests_per_minute FROM provider_policies
                    """)
                    )
                    for provider_id, enabled, priority, rpm in policies:
                        if provider_id in app.state.search.provider_policies:
                            app.state.search.provider_policies[provider_id] = ProviderPolicy(
                                enabled, priority, rpm
                            )
                meter = UsageMeter(app.state.db, config.max_concurrent)
                app.state.application = SearchApplication(app.state.search, meter)
                app.state.reports = UsageReports(app.state.db, config.max_concurrent)
                async with meter.maintenance(), mcp.session_manager.run():
                    yield
        finally:
            await app.state.redis.aclose()
            await app.state.db.dispose()

    app = FastAPI(title="MultiSearch", version=VERSION, lifespan=lifespan)
    install_errors(app)
    app.include_router(router)
    app.include_router(identity_router)
    app.include_router(admin_router)
    app.router.add_route("/mcp", create_mcp_app(mcp, mcp_authenticate), include_in_schema=False)

    @app.get("/health/live")
    async def live():
        return {"status": "alive", "version": VERSION}

    @app.get("/health/ready")
    async def ready(request: Request):
        try:
            async with timeout(2):
                async with request.app.state.db.connect() as connection:
                    await connection.execute(text("SELECT id FROM usage_events LIMIT 0"))
                await request.app.state.redis.ping()
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ready", "version": VERSION}

    return app
