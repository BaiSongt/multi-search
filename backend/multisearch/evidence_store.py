from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .contracts import ImageSearchResult, SearchResponse
from .errors import ApiError
from .evidence_contracts import EvidenceRecord


async def save_evidence(
    connection: AsyncConnection,
    usage_id: UUID,
    records: list[EvidenceRecord],
) -> None:
    if not records:
        return
    await connection.execute(
        text("""
        INSERT INTO evidence_records (tenant_id, evidence_id, usage_id, record)
        SELECT tenant_id, :evidence_id, id, CAST(:record AS JSONB) FROM usage_events WHERE id = :usage
        ON CONFLICT (tenant_id, evidence_id) DO NOTHING
    """),
        [
            {
                "usage": usage_id,
                "evidence_id": record.evidence_id,
                "record": record.model_dump_json(),
            }
            for record in records
        ],
    )


async def save_search_metadata(
    connection: AsyncConnection,
    usage_id: UUID,
    response: SearchResponse,
) -> None:
    await save_evidence(connection, usage_id, response.evidence_records)
    observations = [result for result in response.results if isinstance(result, ImageSearchResult)]
    if not observations:
        return
    await connection.execute(
        text("""
        INSERT INTO image_observations (
            id, usage_id, tenant_id, source_page_url, original_url, thumbnail_url, rights_status,
            source_page_verified, retrieved_at, rights_checked_at, metadata
        )
        SELECT :id, id, tenant_id, :source, :original, :thumbnail, :rights, :verified,
            :retrieved, :checked, CAST(:metadata AS JSONB)
        FROM usage_events WHERE id = :usage
    """),
        [
            {
                "id": uuid4(),
                "usage": usage_id,
                "source": result.image.source_page_url,
                "original": result.image.original_url,
                "thumbnail": result.image.thumbnail_url,
                "rights": result.image.rights_status,
                "verified": result.image.source_page_verified,
                "retrieved": result.retrieved_at,
                "checked": result.image.rights_checked_at,
                "metadata": result.image.model_dump_json(),
            }
            for result in observations
        ],
    )


async def get_evidence(db: AsyncEngine, tenant_id: UUID, evidence_id: str) -> EvidenceRecord:
    async with db.connect() as connection:
        record = await connection.scalar(
            text("""
            SELECT record FROM evidence_records WHERE tenant_id = :tenant AND evidence_id = :id
        """),
            {"tenant": tenant_id, "id": evidence_id},
        )
    if record is None:
        raise ApiError(404, "EVIDENCE_NOT_FOUND", "证据不存在")
    return EvidenceRecord.model_validate(record)
