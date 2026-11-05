from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, Request

from .auth import SearchKey, SessionUser
from .contracts import Role
from .errors import ApiError
from .evidence_contracts import EvidenceRecord
from .evidence_store import get_evidence

router = APIRouter(prefix="/api/v1")
Rights = Literal["unknown", "declared_unverified", "verified_license", "conflict"]


@router.get("/evidence/{evidence_id}", response_model=EvidenceRecord)
async def evidence(
    evidence_id: Annotated[str, Path(pattern=r"^ev_[0-9a-f]{32}$")],
    request: Request,
    identity: SearchKey,
) -> EvidenceRecord:
    if not identity.scopes.intersection({"fetch", "research"}):
        raise ApiError(403, "FORBIDDEN", "API Key 缺少证据读取权限")
    return await get_evidence(request.app.state.db, identity.tenant_id, evidence_id)


@router.get("/admin/images")
async def images(
    request: Request,
    actor: SessionUser,
    rights_status: Rights | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
):
    if actor.role not in {Role.PLATFORM_ADMIN, Role.OPERATIONS_ADMIN}:
        raise ApiError(403, "FORBIDDEN", "当前角色无权查看图片质量")
    tenant_id = None if actor.role == Role.PLATFORM_ADMIN else actor.tenant_id
    return await request.app.state.reports.images(tenant_id, rights_status, limit, cursor)
