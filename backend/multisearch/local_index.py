"""通过部署方批准的 PostgreSQL 文档索引执行无网络检索。"""

import argparse
import asyncio
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
from typing import Annotated, BinaryIO
from unicodedata import category, normalize
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import AwareDatetime, Field, StringConstraints, ValidationError, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .contracts import InputModel, Modality, SearchRequest, Source, TextSearchResult
from .errors import ApiError
from .settings import Settings

_MAX_TENANT_DOCS = 2_000
_MAX_IMPORT_DOCS = 500
_MAX_IMPORT_BYTES = 16 * 1024 * 1024
_MAX_LINE_BYTES = 256 * 1024
_MAX_TEXT_BYTES = 65_536
_CJK = re.compile(r"[\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff\U00020000-\U000323af]")


def _plain_text(value: str) -> str:
    normalized = normalize("NFC", value)
    return " ".join(
        "".join(c for c in normalized if c.isspace() or not category(c).startswith("C")).split()
    )


def _source_domain(value: str) -> str:
    if any(c.isspace() or category(c).startswith("C") or c == "\\" for c in value):
        raise ValueError("来源链接包含无效字符")
    parsed = urlsplit(value)
    host = parsed.hostname
    _ = parsed.port
    if parsed.scheme not in {"http", "https"} or not host or parsed.username is not None:
        raise ValueError("来源链接须为无凭据的 HTTP(S) 地址")
    domain = host.rstrip(".").encode("idna").decode("ascii")
    if not domain or len(domain) > 253:
        raise ValueError("来源域名不合法")
    return domain


class LocalDocument(InputModel):
    id: Annotated[str, StringConstraints(min_length=1, max_length=160)] | None = None
    title: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    text: Annotated[str, StringConstraints(min_length=1, max_length=_MAX_TEXT_BYTES)]
    source_url: Annotated[str, StringConstraints(min_length=1, max_length=2048)] | None = None
    language: str | None = Field(default=None, pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8}){0,3}$")
    published_at: AwareDatetime | None = None

    @field_validator("id", "title", "text")
    @classmethod
    def clean_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = _plain_text(value)
        if not cleaned or len(cleaned.encode("utf-8")) > _MAX_TEXT_BYTES:
            raise ValueError("本地文档字段为空或超过大小上限")
        return cleaned

    @field_validator("source_url")
    @classmethod
    def validate_link(cls, value: str | None) -> str | None:
        if value is not None:
            _source_domain(value)
        return value


def parse_jsonl(stream: BinaryIO) -> list[LocalDocument]:
    documents: list[LocalDocument] = []
    total_bytes = 0
    line_no = 0
    while line := stream.readline(_MAX_LINE_BYTES + 1):
        line_no += 1
        total_bytes += len(line)
        if total_bytes > _MAX_IMPORT_BYTES or len(line) > _MAX_LINE_BYTES:
            raise ValueError("导入内容超过大小上限")
        if not line.strip():
            continue
        if len(documents) >= _MAX_IMPORT_DOCS:
            raise ValueError("单批本地文档数量超过上限")
        try:
            payload = line.decode("utf-8-sig" if line_no == 1 else "utf-8")
            documents.append(LocalDocument.model_validate_json(payload))
        except (ValidationError, UnicodeError):
            raise ValueError(f"第 {line_no} 行不是有效的本地文档") from None
    if not documents:
        raise ValueError("导入内容为空")
    return documents


def _document_id(tenant_id: UUID, document: LocalDocument) -> str:
    if document.id is not None:
        identity = "external\0" + document.id
    else:
        identity = "\0".join(("content", document.title, document.text, document.source_url or ""))
    return sha256(tenant_id.bytes + b"\0" + identity.encode("utf-8")).hexdigest()


_UPSERT = text("""
    INSERT INTO local_documents (
        tenant_id, id, title, plain_text, source_url, source_domain, language, published_at
    )
    VALUES (:tenant_id, :id, :title, :plain_text, :source_url, :source_domain, :language, :published_at)
    ON CONFLICT (tenant_id, id) DO UPDATE SET
        title = EXCLUDED.title,
        plain_text = EXCLUDED.plain_text,
        source_url = EXCLUDED.source_url,
        source_domain = EXCLUDED.source_domain,
        language = EXCLUDED.language,
        published_at = EXCLUDED.published_at,
        updated_at = now()
""")

_SEARCH = text("""
    WITH q AS (
        SELECT plainto_tsquery('simple'::regconfig, CAST(:query AS TEXT)) AS tokens,
               lower(CAST(:query AS TEXT)) AS literal,
               CAST(:allowlist AS TEXT[]) AS allowed,
               CAST(:denylist AS TEXT[]) AS denied
    )
    SELECT d.id, d.title, d.source_url, d.language, d.published_at,
           left(
               substring(d.plain_text from greatest(1, strpos(lower(d.plain_text), q.literal) - 80)),
               512
           ) AS snippet,
           least(1.0,
                 ts_rank_cd(d.search_vector, q.tokens, 32) +
                 CASE WHEN strpos(lower(d.title), q.literal) > 0 THEN 0.25 ELSE 0.05 END
           ) AS relevance
    FROM local_documents AS d
    JOIN tenants AS t ON t.id = d.tenant_id AND t.enabled
    CROSS JOIN q
    WHERE d.tenant_id = :tenant_id
      AND (
           d.search_vector @@ q.tokens
           OR ((CAST(:literal_fallback AS BOOLEAN) OR numnode(q.tokens) = 0)
               AND (strpos(lower(d.title), q.literal) > 0
                    OR strpos(lower(d.plain_text), q.literal) > 0))
      )
      AND (CAST(:date_start AS DATE) IS NULL
           OR timezone('UTC', d.published_at)::date >= CAST(:date_start AS DATE))
      AND (CAST(:date_end AS DATE) IS NULL
           OR timezone('UTC', d.published_at)::date <= CAST(:date_end AS DATE))
      AND (CAST(:language AS TEXT) IS NULL
           OR split_part(lower(d.language), '-', 1) = split_part(lower(CAST(:language AS TEXT)), '-', 1))
      AND NOT EXISTS (
          SELECT 1 FROM unnest(q.denied) AS blocked(hostname)
          WHERE d.source_domain = blocked.hostname
             OR right(d.source_domain, char_length(blocked.hostname) + 1) = '.' || blocked.hostname
      )
      AND (cardinality(q.allowed) = 0 OR EXISTS (
          SELECT 1 FROM unnest(q.allowed) AS permitted(hostname)
          WHERE d.source_domain = permitted.hostname
             OR right(d.source_domain, char_length(permitted.hostname) + 1) = '.' || permitted.hostname
      ))
    ORDER BY relevance DESC, d.updated_at DESC, d.id ASC
    LIMIT :limit
""")


class LocalIndex:
    provider_id = "local_index"

    def __init__(self, db: AsyncEngine):
        self.db = db

    async def search(
        self, tenant_id: UUID | str, request: SearchRequest, limit: int
    ) -> list[TextSearchResult]:
        if (
            limit <= 0
            or Source.LOCAL not in request.source_types
            or Modality.TEXT not in request.modalities
        ):
            return []
        query = _plain_text(request.query)
        if not query:
            return []
        time_range = request.time_range
        async with self.db.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        _SEARCH,
                        {
                            "tenant_id": UUID(str(tenant_id)),
                            "query": query,
                            "literal_fallback": bool(_CJK.search(query)),
                            "date_start": time_range.start if time_range else None,
                            "date_end": time_range.end if time_range else None,
                            "language": request.language,
                            "allowlist": [domain.lower() for domain in request.domain_allowlist],
                            "denylist": [domain.lower() for domain in request.domain_denylist],
                            "limit": min(limit, 30),
                        },
                    )
                )
                .mappings()
                .all()
            )
        retrieved_at = datetime.now(UTC)
        return [
            TextSearchResult(
                result_id=f"local_{row['id']}",
                title=row["title"],
                url=row["source_url"],
                canonical_url=row["source_url"],
                source_type=Source.LOCAL,
                provider_ids=[self.provider_id],
                published_at=row["published_at"],
                retrieved_at=retrieved_at,
                content_type="text/plain",
                language=row["language"],
                snippet=row["snippet"],
                relevance_score=float(row["relevance"]),
            )
            for row in rows
        ]

    async def import_documents(self, tenant_slug: str, documents: Sequence[LocalDocument]) -> int:
        if not 1 <= len(documents) <= _MAX_IMPORT_DOCS:
            raise ValueError("导入文档数量超过单批限制")
        async with self.db.begin() as connection:
            tenant_id = await connection.scalar(
                text("""
                SELECT id FROM tenants WHERE slug = :slug AND enabled FOR UPDATE
            """),
                {"slug": tenant_slug},
            )
            if tenant_id is None:
                raise ApiError(404, "TENANT_NOT_FOUND", "目标租户不存在或已停用")
            ids = [_document_id(tenant_id, document) for document in documents]
            if len(set(ids)) != len(ids):
                raise ValueError("本次导入含重复文档标识")
            current, existing = (
                await connection.execute(
                    text("""
                SELECT count(*), count(*) FILTER (WHERE id = ANY(CAST(:ids AS TEXT[])))
                FROM local_documents WHERE tenant_id = :tenant_id
            """),
                    {"tenant_id": tenant_id, "ids": ids},
                )
            ).one()
            if current + len(ids) - existing > _MAX_TENANT_DOCS:
                raise ApiError(409, "LOCAL_INDEX_CAPACITY", "本地文档数量达到租户上限")
            await connection.execute(
                _UPSERT,
                [
                    {
                        "tenant_id": tenant_id,
                        "id": doc_id,
                        "title": document.title,
                        "plain_text": document.text,
                        "source_url": document.source_url,
                        "source_domain": _source_domain(document.source_url)
                        if document.source_url
                        else None,
                        "language": document.language,
                        "published_at": document.published_at,
                    }
                    for doc_id, document in zip(ids, documents, strict=True)
                ],
            )
            await connection.execute(
                text("""
                INSERT INTO audit_events (tenant_id, actor_type, action, target_id, details)
                VALUES (:tenant_id, 'local_operator', 'local_index.import', :tenant_id,
                        jsonb_build_object('documents', CAST(:count AS INTEGER)))
            """),
                {"tenant_id": tenant_id, "count": len(ids)},
            )
        return len(documents)


async def _import(tenant_slug: str, documents: list[LocalDocument]) -> int:
    settings = Settings.from_env()
    engine = create_async_engine(settings.database_url, hide_parameters=True)
    try:
        return await LocalIndex(engine).import_documents(tenant_slug, documents)
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_subparsers(dest="action", required=True).add_parser("import")
    action.add_argument("--tenant", required=True, help="目标租户标识，读取 UTF-8 JSONL 标准输入")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", args.tenant):
        parser.error("--tenant 格式无效")
    try:
        documents = parse_jsonl(sys.stdin.buffer)
        count = asyncio.run(_import(args.tenant, documents))
    except (ApiError, ValueError) as error:
        parser.exit(2, f"{error}\n")
    print(f"已写入 {count} 份本地文档")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
