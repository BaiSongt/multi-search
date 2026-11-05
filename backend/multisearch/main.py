from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from .settings import Settings

VERSION = (Path(__file__).resolve().parents[2] / "VERSION").read_text().strip()


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config = settings or Settings.from_env()
        app.state.settings = config
        app.state.db = create_async_engine(config.database_url, pool_pre_ping=True)
        app.state.redis = Redis.from_url(
            config.redis_url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2
        )
        try:
            yield
        finally:
            await app.state.redis.aclose()
            await app.state.db.dispose()

    app = FastAPI(title="MultiSearch", version=VERSION, lifespan=lifespan)

    @app.get("/health/live")
    async def live():
        return {"status": "alive", "version": VERSION}

    @app.get("/health/ready")
    async def ready(request: Request):
        from asyncio import timeout

        try:
            async with timeout(2):
                async with request.app.state.db.connect() as connection:
                    await connection.execute(text("SELECT 1"))
                await request.app.state.redis.ping()
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ready", "version": VERSION}

    return app
