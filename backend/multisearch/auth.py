import hmac
import os
from ipaddress import ip_address
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.exc import SQLAlchemyError

from .errors import ApiError
from .identity import MachineIdentity
from .sessions import SESSION_COOKIE, SessionIdentity

bearer = HTTPBearer(auto_error=False)


async def require_machine(
    request: Request,
    auth: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> MachineIdentity:
    telemetry = getattr(request.app.state, "telemetry", None)
    if auth is None:
        if telemetry:
            telemetry.record_auth("failed")
        raise ApiError(401, "AUTH_REQUIRED", "需要有效的 API Key")
    return await authenticate_machine(request.app.state.identity, auth.credentials, telemetry)


async def authenticate_machine(store, raw: str, telemetry=None) -> MachineIdentity:
    try:
        identity = await store.authenticate(raw)
    except ApiError:
        if telemetry:
            telemetry.record_auth("failed")
        raise
    except (SQLAlchemyError, OSError, TimeoutError) as error:
        if telemetry:
            telemetry.record_auth("unavailable")
        raise ApiError(503, "AUTH_UNAVAILABLE", "鉴权服务暂不可用", retryable=True) from error
    if telemetry:
        telemetry.record_auth("success", scopes=identity.scopes)
    return identity


SearchKey = Annotated[MachineIdentity, Depends(require_machine)]


def console_origin() -> tuple[str, bool]:
    value = os.getenv("MULTISEARCH_PUBLIC_CONSOLE_URL", "http://127.0.0.1:3000")
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or any(char.isspace() or ord(char) < 32 for char in value)
        ):
            raise ValueError("无效的 Console 来源")
        secure = parsed.scheme == "https"
        if not secure and not ip_address(host).is_loopback:
            raise ValueError("仅回环 Console 地址可以使用 HTTP")
        origin = f"{parsed.scheme}://{f'[{host}]' if ':' in host else host}"
        if port is not None and port != (443 if secure else 80):
            origin += f":{port}"
        return origin, secure
    except ValueError as error:
        raise ValueError("MULTISEARCH_PUBLIC_CONSOLE_URL 必须为 HTTPS 或回环 HTTP 来源") from error


def require_console_origin(request: Request) -> None:
    expected, _ = console_origin()
    if request.headers.getlist("origin") != [expected]:
        raise ApiError(403, "FORBIDDEN", "Console 请求来源不受信任")


async def require_session(request: Request) -> SessionIdentity:
    raw = request.cookies.get(SESSION_COOKIE)
    if raw is None:
        raise ApiError(401, "AUTH_REQUIRED", "需要有效的登录会话")
    try:
        return await request.app.state.identity.sessions.authenticate(raw)
    except (SQLAlchemyError, OSError, TimeoutError) as error:
        raise ApiError(503, "AUTH_UNAVAILABLE", "鉴权服务暂不可用", retryable=True) from error


SessionUser = Annotated[SessionIdentity, Depends(require_session)]


async def require_session_write(request: Request, actor: SessionUser) -> SessionIdentity:
    require_console_origin(request)
    tokens = request.headers.getlist("x-csrf-token")
    if (
        len(tokens) != 1
        or len(tokens[0]) != 64
        or not tokens[0].isascii()
        or not hmac.compare_digest(tokens[0], actor.csrf_token)
    ):
        raise ApiError(403, "FORBIDDEN", "会话防伪令牌无效")
    return actor


SessionWrite = Annotated[SessionIdentity, Depends(require_session_write)]
