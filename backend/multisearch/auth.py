from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.exc import SQLAlchemyError

from .errors import ApiError
from .identity import MachineIdentity

bearer = HTTPBearer(auto_error=False)


async def require_machine(
    request: Request,
    auth: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> MachineIdentity:
    if auth is None:
        raise ApiError(401, "AUTH_REQUIRED", "需要有效的 API Key")
    try:
        return await request.app.state.identity.authenticate(auth.credentials)
    except (SQLAlchemyError, OSError, TimeoutError) as error:
        raise ApiError(503, "AUTH_UNAVAILABLE", "鉴权服务暂不可用", retryable=True) from error


SearchKey = Annotated[MachineIdentity, Depends(require_machine)]
