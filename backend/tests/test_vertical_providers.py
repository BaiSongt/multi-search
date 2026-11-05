from datetime import UTC, datetime
from time import monotonic, time

import httpx
import pytest

from multisearch.contracts import Modality, SearchRequest, Source
from multisearch.providers import ProviderFailure
from multisearch.vertical_providers import (
    ArxivProvider,
    CrossrefProvider,
    GitHubProvider,
    OpenAlexProvider,
    academic_identifier,
    create_vertical_providers,
)

ARXIV_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2512.12345v3</id>
    <title> Lunar   orbits </title>
    <summary>Descriptive metadata for the preprint.</summary>
    <published>2025-12-10T17:00:00Z</published>
    <updated>2026-01-01T17:00:00Z</updated>
    <author><name>Ada Example</name></author>
    <arxiv:doi>10.1234/LUNAR</arxiv:doi>
    <arxiv:journal_ref>Research Journal 25 (2025)</arxiv:journal_ref>
  </entry>
</feed>
"""


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("10.1234/LUNAR", ("doi", "10.1234/lunar", None)),
        ("DOI: 10.1234/LUNAR", ("doi", "10.1234/lunar", None)),
        ("https://doi.org/10.1234/LUNAR%3Fx%23y", ("doi", "10.1234/lunar?x#y", None)),
        ("arXiv:2512.12345", ("arxiv", "2512.12345", None)),
        ("https://arxiv.org/pdf/quant-ph/9508027v2.pdf", ("arxiv", "quant-ph/9508027", "v2")),
        ("lunar paper 10.1234/example", None),
        ("https://doi.org.example.org/10.1234/lunar", None),
        ("https://doi.org/10.1234/%2e%2e/example", None),
        ("10.1234/%2e%2e/example", None),
        ("https://arxiv.org@example.org/abs/2512.12345v3", None),
    ],
)
def test_academic_identifier_requires_the_entire_query(query, expected):
    identifier = academic_identifier(query)
    actual = (identifier.kind, identifier.value, identifier.version) if identifier else None
    assert actual == expected


@pytest.mark.parametrize("adapter", [CrossrefProvider, OpenAlexProvider])
async def test_exact_doi_uses_singleton_escapes_reserved_characters_and_filters_dates(adapter):
    expected_doi = "10.1234/lunar?x#y"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.split(b"?", 1)[0]
        assert path.endswith(b"10.1234/lunar%3Fx%23y")
        if adapter is CrossrefProvider:
            assert request.url.host == "api.crossref.org" and path.startswith(b"/works/10.")
            assert dict(request.url.params) == {"mailto": "operator@example.org"}
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "message": {
                        "DOI": expected_doi,
                        "title": ["Lunar orbits"],
                        "published": {"date-parts": [[2025, 12, 10]]},
                    },
                },
            )
        assert request.url.host == "api.openalex.org" and path.startswith(b"/works/doi:10.")
        assert set(request.url.params) == {"select"}
        return httpx.Response(
            200,
            json={
                "doi": "https://doi.org/10.1234/lunar%3Fx%23y",
                "display_name": "Lunar orbits",
                "publication_date": "2025-12-10",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = (
            adapter(client, "operator@example.org")
            if adapter is CrossrefProvider
            else adapter(client)
        )
        query = "https://doi.org/10.1234/LUNAR%3Fx%23y"
        response = await source.search(
            SearchRequest(query=query), Source.ACADEMIC, Modality.TEXT, 10, 1
        )
        outside = await source.search(
            SearchRequest(query=query, time_range={"from": "2024-01-01", "to": "2024-12-31"}),
            Source.ACADEMIC,
            Modality.TEXT,
            10,
            1,
        )
    assert response.results[0].doi == expected_doi and not outside.results


@pytest.mark.parametrize("status", [200, 404])
async def test_exact_doi_distinguishes_missing_work_from_a_mismatched_record(status):
    wrong = {"status": "ok", "message": {"DOI": "10.1234/wrong", "title": ["Wrong work"]}}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, json=wrong),
        )
    ) as client:
        source = CrossrefProvider(client)
        args = (SearchRequest(query="10.1234/example"), Source.ACADEMIC, Modality.TEXT, 10, 1)
        if status == 404:
            result = await source.search(*args)
            assert result.results == [] and not result.partial
        else:
            with pytest.raises(ProviderFailure, match="failed"):
                await source.search(*args)


@pytest.mark.parametrize(
    "query", ["arXiv:2512.12345", "https://arxiv.org/abs/2512.12345v3", "2512.12345v2"]
)
async def test_arxiv_exact_id_keeps_requested_version(query):
    def handler(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {
            "id_list": query.rsplit("/", 1)[-1].removeprefix("arXiv:"),
            "max_results": "1",
        }
        return httpx.Response(200, text=ARXIV_FEED)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = ArxivProvider(client)
        args = (SearchRequest(query=query), Source.ACADEMIC, Modality.TEXT, 5, 1)
        if query.endswith("v2"):
            with pytest.raises(ProviderFailure, match="failed"):
                await source.search(*args)
        else:
            result = await source.search(*args)
            assert result.results[0].version == "v3"


async def test_openalex_filters_dates_and_maps_declared_copy_metadata():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url.copy_with(query=None)) == "https://api.openalex.org/works"
        assert request.headers["authorization"] == "Bearer mock-openalex-key"
        assert "mock-openalex-key" not in str(request.url)
        assert request.url.params["search"] == "lunar orbits"
        assert request.url.params["per_page"] == "3"
        assert request.url.params["filter"] == (
            "from_publication_date:2025-12-01,to_publication_date:2025-12-31"
        )
        published = {
            "id": "https://openalex.org/W123",
            "display_name": "  Lunar   orbits  ",
            "doi": "https://doi.org/10.1234/LUNAR",
            "publication_date": "2025-12-10",
            "language": "en",
            "authorships": [{"author": {"display_name": "Ada Example"}}],
            "primary_location": {
                "landing_page_url": "https://journals.example.org/paper?utm_source=search",
                "source": {"type": "journal", "display_name": "Research Journal"},
                "license": "cc-by",
                "license_id": "https://openalex.org/licenses/cc-by",
                "version": "publishedVersion",
            },
        }
        return httpx.Response(
            200,
            json={
                "results": [
                    published,
                    {**published, "publication_date": "2024-12-01"},
                    {**published, "publication_date": None},
                ],
            },
        )

    request = SearchRequest(
        query="lunar orbits",
        source_types=[Source.ACADEMIC],
        time_range={"from": "2025-12-01", "to": "2025-12-31"},
        domain_allowlist=["example.org"],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await OpenAlexProvider(client, "mock-openalex-key").search(
            request,
            Source.ACADEMIC,
            Modality.TEXT,
            3,
            1,
        )
    assert response.warnings == ["openalex:publication_date_missing:1"]
    assert len(response.results) == 1
    result = response.results[0]
    assert result.source_type == Source.ACADEMIC
    assert result.title == "Lunar orbits"
    assert result.canonical_url == "https://journals.example.org/paper"
    assert result.doi == "10.1234/lunar"
    assert result.authors == ["Ada Example"]
    assert result.published_at == datetime(2025, 12, 10, tzinfo=UTC)
    assert result.journal == "Research Journal"
    assert result.version == "publishedVersion"
    assert result.license_name == "cc-by"
    assert result.license_url == "https://openalex.org/licenses/cc-by"


async def test_crossref_keeps_exact_dates_and_does_not_pick_conflicting_licenses():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.crossref.org"
        assert request.url.path == "/works"
        assert request.url.params["query.bibliographic"] == "lunar orbits"
        assert request.url.params["rows"] == "30"
        assert request.url.params["mailto"] == "operator@example.org"
        assert not {"abstract", "language"}.intersection(request.url.params["select"].split(","))
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "message": {
                    "items": [
                        {
                            "DOI": "10.1234/LUNAR",
                            "title": ["Lunar orbits"],
                            "author": [
                                {"given": "Ada", "family": "Example"},
                                {"name": "Research Team"},
                            ],
                            "container-title": ["Research Journal"],
                            "published": {"date-parts": [[2025]]},
                            "published-online": {"date-parts": [[2025, 12, 10]]},
                            "URL": "http://127.0.0.1/fake-article",
                            "license": [
                                {"URL": "https://creativecommons.org/licenses/by/4.0/"},
                                {"URL": "https://publisher.example.org/license/"},
                            ],
                        },
                        {
                            "DOI": "10.1234/YEARONLY",
                            "title": ["Year only"],
                            "published": {"date-parts": [[2025]]},
                        },
                    ]
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await CrossrefProvider(client, "operator@example.org").search(
            SearchRequest(query="lunar orbits"),
            Source.ACADEMIC,
            Modality.TEXT,
            40,
            1,
        )
    result, year_only = response.results
    assert result.url == "https://doi.org/10.1234/lunar"
    assert result.journal == "Research Journal"
    assert result.authors == ["Ada Example", "Research Team"]
    assert result.published_at == datetime(2025, 12, 10, tzinfo=UTC)
    assert result.license_url is None
    assert result.snippet is None
    assert year_only.published_at is None


async def test_arxiv_atom_distinguishes_submitted_date_from_version():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url.copy_with(query=None)) == "https://export.arxiv.org/api/query"
        assert request.headers["accept"] == "application/atom+xml"
        assert request.url.params["search_query"] == (
            'all:"lunar orbits" AND submittedDate:[202512010000 TO 202512312359]'
        )
        return httpx.Response(200, text=ARXIV_FEED)

    request = SearchRequest(
        query='lunar "orbits"',
        time_range={"from": "2025-12-01", "to": "2025-12-31"},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await ArxivProvider(client).search(request, Source.ACADEMIC, Modality.TEXT, 3, 1)
    result = response.results[0]
    assert result.url == "https://arxiv.org/abs/2512.12345v3"
    assert result.arxiv_id == "2512.12345"
    assert result.version == "v3"
    assert result.published_at == datetime(2025, 12, 10, 17, tzinfo=UTC)
    assert result.journal == "Research Journal 25 (2025)"
    assert result.doi == "10.1234/lunar"
    assert result.authors == ["Ada Example"]


@pytest.mark.parametrize(
    "body",
    [
        '<!DOCTYPE feed [<!ENTITY data SYSTEM "file:///etc/passwd">]><feed>&data;</feed>',
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/api/errors#bad</id></entry></feed>',
        "<not-an-atom-feed />",
    ],
)
async def test_arxiv_rejects_entity_declarations_and_error_documents(body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body)),
    ) as client:
        with pytest.raises(ProviderFailure, match="failed"):
            await ArxivProvider(client).search(
                SearchRequest(query="lunar orbits"),
                Source.ACADEMIC,
                Modality.TEXT,
                3,
                1,
            )


async def test_arxiv_pacing_is_included_in_time_budget():
    def unexpected(_: httpx.Request):
        pytest.fail("rate-limited request must not reach arXiv")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        provider = ArxivProvider(client)
        provider._next_request = monotonic() + 3
        with pytest.raises(ProviderFailure, match="timeout"):
            await provider.search(
                SearchRequest(query="moon"), Source.ACADEMIC, Modality.TEXT, 3, 0.01
            )


async def test_github_forces_public_and_never_exposes_private_records():
    def handler(request: httpx.Request) -> httpx.Response:
        assert (
            str(request.url.copy_with(query=None)) == "https://api.github.com/search/repositories"
        )
        assert request.headers["authorization"] == "Bearer mock-github-token"
        assert "mock-github-token" not in str(request.url)
        assert request.headers["x-github-api-version"] == "2026-03-10"
        assert (
            request.url.params["q"]
            == '"moon repo:hidden/private" is:public created:2025-01-01..2025-12-31'
        )
        public = {
            "private": False,
            "visibility": "public",
            "full_name": "sample/moon",
            "html_url": "https://github.com/sample/moon",
            "default_branch": "main",
            "created_at": "2025-04-01T06:00:00Z",
            "description": "Lunar toolkit",
            "license": {"spdx_id": "MIT", "url": "https://api.github.com/licenses/mit"},
        }
        return httpx.Response(
            200,
            json={
                "incomplete_results": True,
                "items": [
                    public,
                    {**public, "private": True, "full_name": "hidden/private"},
                    {**public, "visibility": "internal", "full_name": "hidden/internal"},
                    {**public, "private": None, "full_name": "hidden/ambiguous"},
                ],
            },
        )

    request = SearchRequest(
        query='moon "repo:hidden/private"',
        time_range={"from": "2025-01-01", "to": "2025-12-31"},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await GitHubProvider(client, "mock-github-token").search(
            request,
            Source.CODE,
            Modality.TEXT,
            3,
            1,
        )
    (result,) = response.results
    assert result.source_type == Source.CODE
    assert result.title == "sample/moon"
    assert result.repository_branch == "main"
    assert result.version is None
    assert result.license_name == "MIT"
    assert result.license_url == "https://api.github.com/licenses/mit"
    assert response.partial and response.warnings == ["github:upstream_incomplete"]


@pytest.mark.parametrize(
    ("provider_class", "response", "status", "delay"),
    [
        (OpenAlexProvider, httpx.Response(429, headers={"Retry-After": "7"}), "rate_limited", 7),
        (CrossrefProvider, httpx.Response(504), "timeout", None),
        (
            GitHubProvider,
            httpx.Response(403, json={"message": "Secondary rate limit"}),
            "rate_limited",
            60,
        ),
        (GitHubProvider, httpx.Response(403, json={"message": "Forbidden"}), "failed", None),
        (
            OpenAlexProvider,
            httpx.Response(302, headers={"location": "https://outside.example.org"}),
            "failed",
            None,
        ),
    ],
)
async def test_official_adapters_do_not_redirect_or_leak_upstream_failure(
    provider_class, response, status, delay
):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return response

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        provider = (
            provider_class(client, "mock-github-token")
            if provider_class is GitHubProvider
            else provider_class(client)
        )
        with pytest.raises(ProviderFailure) as error:
            await provider.search(SearchRequest(query="moon"), provider.source, Modality.TEXT, 3, 1)
    assert error.value.status == status
    assert error.value.retry_after == delay
    assert len(calls) == 1


async def test_github_uses_epoch_reset_when_primary_search_bucket_is_empty():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                403,
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(time()) + 120)},
            )
        )
    ) as client:
        with pytest.raises(ProviderFailure) as error:
            await GitHubProvider(client, "mock-token").search(
                SearchRequest(query="moon"),
                Source.CODE,
                Modality.TEXT,
                3,
                1,
            )
    assert error.value.status == "rate_limited"
    assert 110 <= error.value.retry_after <= 120


async def test_adapter_response_has_a_hard_size_limit(monkeypatch):
    monkeypatch.setattr("multisearch.vertical_providers._MAX_UPSTREAM_BYTES", 8)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"123456789")),
    ) as client:
        with pytest.raises(ProviderFailure, match="failed"):
            await OpenAlexProvider(client).search(
                SearchRequest(query="moon"),
                Source.ACADEMIC,
                Modality.TEXT,
                3,
                1,
            )


async def test_factory_disables_unconfigured_sources_and_adapters_block_offline_search():
    def unexpected(_: httpx.Request):
        pytest.fail("invalid scope must not reach a public provider")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        assert create_vertical_providers(client, enabled_sources=[]) == []
        selected = create_vertical_providers(
            client,
            enabled_sources={"openalex", "crossref", "arxiv", "github"},
        )
        assert [provider.provider_id for provider in selected] == ["openalex", "crossref", "arxiv"]
        for provider in selected:
            with pytest.raises(ProviderFailure, match="failed"):
                await provider.search(
                    SearchRequest(query="internal", network_scope="offline"),
                    Source.ACADEMIC,
                    Modality.TEXT,
                    3,
                    1,
                )
        selected = create_vertical_providers(
            client,
            enabled_sources={"github"},
            github_token="mock-token",
        )
        assert selected[0].pairs == frozenset({(Source.CODE, Modality.TEXT)})
