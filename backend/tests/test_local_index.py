import io
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from multisearch import local_index
from multisearch.contracts import SearchRequest, Source
from multisearch.local_index import LocalDocument, LocalIndex, parse_jsonl


def test_jsonl_normalizes_utf8_plain_text_and_accepts_private_metadata_links():
    data = (
        '\ufeff{"id":"KB-1","title":"  中文   标题  ","text":"私有\\n内容",'
        '"source_url":"https://wiki.internal/doc","language":"zh-CN"}\n\n'
    )
    [doc] = parse_jsonl(io.BytesIO(data.encode()))
    assert doc.id == "KB-1"
    assert doc.title == "中文 标题" and doc.text == "私有 内容"
    assert doc.source_url == "https://wiki.internal/doc"


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https://operator:secret@wiki.internal/doc",
        "https://wiki.internal:invalid/doc",
    ],
)
def test_non_http_or_credentialed_links_are_rejected(url):
    with pytest.raises(ValidationError):
        LocalDocument(title="Doc", text="Body", source_url=url)


def test_jsonl_caps_input_and_does_not_echo_invalid_document(monkeypatch):
    with pytest.raises(ValueError, match="第 1 行") as error:
        parse_jsonl(io.BytesIO(b'{"title":"confidential-input"}\n'))
    assert "confidential-input" not in str(error.value)
    monkeypatch.setattr(local_index, "_MAX_IMPORT_DOCS", 1)
    with pytest.raises(ValueError, match="单批"):
        parse_jsonl(io.BytesIO(b'{"title":"x","text":"y"}\n' * 2))
    monkeypatch.setattr(local_index, "_MAX_LINE_BYTES", 10)
    with pytest.raises(ValueError, match="大小"):
        parse_jsonl(io.BytesIO(b'{"title":"x","text":"y"}\n'))


def test_document_identifiers_are_tenant_scoped_and_stable():
    first, second = uuid4(), uuid4()
    identified = LocalDocument(id="external", title="Doc", text="First content")
    changed = identified.model_copy(update={"text": "Updated content"})
    a = local_index._document_id(first, identified)
    assert len(a) == 64 and a != local_index._document_id(second, identified)
    assert a == local_index._document_id(first, changed)
    anonymous = identified.model_copy(update={"id": None})
    assert local_index._document_id(first, anonymous) != local_index._document_id(
        first, anonymous.model_copy(update={"text": "Changed"})
    )


class SearchDB:
    def __init__(self):
        self.calls = []

    @asynccontextmanager
    async def connect(self):
        yield self

    async def execute(self, statement, params):
        self.calls.append((statement, params))
        return self

    def mappings(self):
        return self

    def all(self):
        return [
            {
                "id": "a" * 64,
                "title": "Fixture",
                "source_url": None,
                "published_at": datetime(2026, 10, 1, tzinfo=UTC),
                "language": "zh",
                "snippet": "匹配结果",
                "relevance": 0.6,
            }
        ]


async def test_scoped_local_search_binds_filters_and_uses_bounded_result_shape():
    db = SearchDB()
    index = LocalIndex(db)
    tenant_id = uuid4()
    assert await index.search(tenant_id, SearchRequest(query="公网查询"), 5) == []
    request = SearchRequest.model_validate(
        {
            "query": "匹配",
            "mode": "local",
            "source_types": ["local"],
            "network_scope": "offline",
            "time_range": {"from": "2026-01-01", "to": "2026-11-01"},
            "language": "zh-CN",
            "domain_denylist": ["restricted.example"],
        }
    )
    [result] = await index.search(str(tenant_id), request, 1000)
    [(statement, params)] = db.calls
    assert "d.tenant_id = :tenant_id" in str(statement)
    assert params["tenant_id"] == tenant_id and params["limit"] == 30
    assert params["literal_fallback"] and params["date_end"] == date(2026, 11, 1)
    assert params["denylist"] == ["restricted.example"]
    assert result.source_type == Source.LOCAL and result.provider_ids == ["local_index"]
    assert result.url is None and result.evidence_status == "unverified"
