from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field, SecretStr, StringConstraints, field_validator
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from .auth import SessionUser, SessionWrite, console_origin, require_console_origin
from .contracts import InputModel, Role
from .errors import ApiError
from .identity import KeyView, key_hash
from .policy import validate_limits
from .sessions import EMAIL_PATTERN, SESSION_COOKIE, TENANT_PATTERN, SessionIdentity

router = APIRouter(prefix="/api/v1", tags=["身份与密钥"])

_LOGIN_LIMIT = """
local wait = 0
for index = 1, #KEYS do
    local count = tonumber(redis.call('GET', KEYS[index]) or '0')
    if count >= tonumber(ARGV[index]) then
        wait = math.max(wait, redis.call('PTTL', KEYS[index]), 1)
    end
end
if wait > 0 then return wait end
for index = 1, #KEYS do
    local count = redis.call('INCR', KEYS[index])
    if count == 1 then redis.call('PEXPIRE', KEYS[index], 60000) end
end
return 0
"""


class Login(InputModel):
    tenant: str = Field(min_length=1, max_length=63, pattern=TENANT_PATTERN)
    email: str = Field(min_length=3, max_length=254, pattern=EMAIL_PATTERN)
    password: SecretStr = Field(min_length=1, max_length=128)


class SessionView(BaseModel):
    tenant_id: UUID
    tenant: str
    user_id: UUID
    email: str
    role: Role
    expires_at: datetime
    csrf_token: str = Field(repr=False)


class KeyCreate(InputModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    environment: Literal["dev", "test", "prod"] = "dev"
    scopes: list[Literal["search:text", "search:image", "fetch", "research"]] = Field(
        min_length=1, max_length=4
    )
    ttl_hours: int = Field(default=24, strict=True, ge=1, le=720)
    limits: dict[str, Annotated[int, Field(strict=True)]] = Field(
        default_factory=dict, max_length=11
    )

    @field_validator("scopes")
    @classmethod
    def unique_scopes(cls, scopes: list[str]) -> list[str]:
        if len(set(scopes)) != len(scopes):
            raise ValueError("作用域不得重复")
        return sorted(scopes)

    @field_validator("limits")
    @classmethod
    def limits_policy(cls, limits: dict[str, int]) -> dict[str, int]:
        return validate_limits(limits)


class KeyRotate(InputModel):
    grace_seconds: int = Field(default=0, strict=True, ge=0, le=86_400)
    ttl_hours: int | None = Field(default=None, strict=True, ge=1, le=720)


class KeyPage(BaseModel):
    keys: list[KeyView]
    next_cursor: UUID | None = None


class KeyResult(BaseModel):
    key: KeyView


class KeyCreated(KeyResult):
    secret: str = Field(repr=False)


def _set_session(response: Response, raw: str, actor: SessionIdentity) -> SessionView:
    _, secure = console_origin()
    response.set_cookie(
        SESSION_COOKIE,
        raw,
        expires=actor.expires_at,
        max_age=max(0, int((actor.expires_at - datetime.now(UTC)).total_seconds())),
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )
    return SessionView.model_validate(actor, from_attributes=True)


async def _limit_login(request: Request, tenant: str, email: str) -> None:
    peer = request.client.host if request.client is not None else "unknown"
    pepper = request.app.state.identity.pepper
    account_key = key_hash(f"login-account:{tenant}:{email}", pepper).hex()
    peer_key = key_hash(f"login-peer:{peer}", pepper).hex()
    try:
        retry_ms = await request.app.state.redis.eval(
            _LOGIN_LIMIT,
            2,
            f"multisearch:login:v1:{account_key}",
            f"multisearch:login:v1:{peer_key}",
            8,
            90,
        )
    except (RedisError, OSError, TimeoutError) as error:
        raise ApiError(503, "AUTH_UNAVAILABLE", "登录限流暂不可用", retryable=True) from error
    if retry_ms:
        raise ApiError(
            429, "RATE_LIMITED", "登录尝试过于频繁", retryable=True, retry_after_ms=int(retry_ms)
        )


@router.post("/auth/login", dependencies=[Depends(require_console_origin)])
async def login(request: Request, response: Response, body: Login) -> SessionView:
    email = body.email.lower()
    await _limit_login(request, body.tenant, email)
    try:
        raw, actor = await request.app.state.identity.sessions.login(
            body.tenant, email, body.password.get_secret_value()
        )
    except (SQLAlchemyError, OSError, TimeoutError) as error:
        raise ApiError(503, "AUTH_UNAVAILABLE", "鉴权服务暂不可用", retryable=True) from error
    return _set_session(response, raw, actor)


@router.get("/me")
async def me(actor: SessionUser) -> SessionView:
    return SessionView.model_validate(actor, from_attributes=True)


@router.post("/auth/refresh")
async def refresh(request: Request, response: Response, actor: SessionWrite) -> SessionView:
    raw, refreshed = await request.app.state.identity.sessions.refresh(actor)
    return _set_session(response, raw, refreshed)


@router.post("/auth/logout", status_code=204)
async def logout(request: Request, actor: SessionWrite) -> Response:
    await request.app.state.identity.sessions.logout(actor)
    response = Response(status_code=204)
    _, secure = console_origin()
    response.delete_cookie(SESSION_COOKIE, path="/", secure=secure, httponly=True, samesite="lax")
    return response


@router.get("/keys")
async def list_keys(
    request: Request,
    actor: SessionUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: UUID | None = None,
) -> KeyPage:
    keys, next_cursor = await request.app.state.identity.list_keys(actor, limit, cursor)
    return KeyPage(keys=keys, next_cursor=next_cursor)


@router.post("/keys", status_code=201)
async def create_key(request: Request, body: KeyCreate, actor: SessionWrite) -> KeyCreated:
    key, raw = await request.app.state.identity.create_key(
        actor,
        body.name,
        body.environment,
        body.scopes,
        body.ttl_hours,
        body.limits,
        min(5, request.app.state.settings.max_concurrent),
    )
    return KeyCreated(key=key, secret=raw)


@router.post("/keys/{key_id}/revoke")
async def revoke_key(request: Request, key_id: UUID, actor: SessionWrite) -> KeyResult:
    return KeyResult(key=await request.app.state.identity.revoke_key(actor, key_id))


@router.post("/keys/{key_id}/rotate")
async def rotate_key(
    request: Request, key_id: UUID, body: KeyRotate, actor: SessionWrite
) -> KeyCreated:
    key, raw = await request.app.state.identity.rotate_key(
        actor,
        key_id,
        body.grace_seconds,
        body.ttl_hours,
        min(5, request.app.state.settings.max_concurrent),
    )
    return KeyCreated(key=key, secret=raw)
