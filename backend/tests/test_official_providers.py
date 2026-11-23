import json

import httpx
import pytest

from multisearch.contracts import Modality, SearchRequest, Source
from multisearch.official_providers import (
    AAIBReportsProvider,
    AustraliaOpenDataProvider,
    CanadaOpenDataProvider,
    ClinicalTrialsRegistryProvider,
    CordisProjectsProvider,
    EarthdataProvider,
    EuropeanDataPortalProvider,
    EuropePMCProvider,
    FederalRegisterProvider,
    FranceOpenDataProvider,
    NoaaClimateDataProvider,
    OstiResearchProvider,
    TransportationStatisticsProvider,
    UKOpenDataProvider,
    USGSProvider,
    WorldBankReportsProvider,
)
from multisearch.providers import ProviderFailure


async def test_aaib_limits_results_to_published_reports_and_labels_occurrence_dates():
    report = {
        "title": "AAIB investigation to Example, G-TEST",
        "link": "/aaib-reports/example-g-test",
        "description": "<p>Runway excursion after landing.</p>",
        "date_of_occurrence": "2024-10-20",
        "public_timestamp": "2025-01-15T09:00:00Z",
        "content_store_document_type": "aaib_report",
    }
    calls = []

    def upstream(request):
        calls.append(request)
        assert request.url.host == "www.gov.uk"
        assert request.url.path == "/api/search.json"
        assert request.url.params["count"] == "3"
        assert request.url.params["q"] == "runway excursion"
        assert request.url.params["filter_content_store_document_type"] == "aaib_report"
        return httpx.Response(200, json={
            "results": [
                report,
                {**report, "link": "//example.org/aaib-reports/not-official"},
                {**report, "content_store_document_type": "publication"},
                {**report, "link": "/aaib-reports/over-budget"},
            ],
            "total": 250,
        })

    query = SearchRequest(query="site:gov.uk AAIB runway excursion")
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        provider = AAIBReportsProvider(client)
        assert provider.supports_request(query)
        assert provider.explicitly_requested(query)
        assert not provider.supports_request(
            SearchRequest(query="visa fees", domain_allowlist=["gov.uk"])
        )
        assert not provider.supports_request(
            SearchRequest(query=query.query, time_range={"from": "2024-01-01", "to": "2024-12-31"})
        )
        result = await provider.search(query, Source.WEB, Modality.TEXT, 30, 2)

    assert len(calls) == 1
    assert result.partial
    (item,) = result.results
    assert item.url == f"https://www.gov.uk{report['link']}"
    assert item.published_at is None
    assert item.snippet == "Date of occurrence: 2024-10-20. Runway excursion after landing."
    assert item.authors == ["Air Accidents Investigation Branch"]


@pytest.mark.parametrize(
    ("provider_class", "domain", "parameter", "payload", "expected_url"),
    [
        (
            FederalRegisterProvider,
            "federalregister.gov",
            "per_page",
            {
                "results": [
                    {
                        "title": "Public notice",
                        "html_url": "https://www.federalregister.gov/documents/demo",
                        "publication_date": "2026-01-01",
                        "abstract": "Notice metadata",
                    }
                ]
            },
            "https://www.federalregister.gov/documents/demo",
        ),
        (
            UKOpenDataProvider,
            "data.gov.uk",
            "rows",
            {
                "success": True,
                "result": {
                    "results": [
                        {
                            "id": "ff69f339-99a6-4e50-9ca0-de1be386f0b9",
                            "name": "transport",
                            "private": False,
                            "state": "active",
                            "title": "Transport",
                            "notes": "Dataset description",
                        },
                        {
                            "id": "0f69f339-99a6-4e50-9ca0-de1be386f0b9",
                            "name": "private",
                            "private": True,
                            "state": "active",
                            "title": "Private dataset",
                        },
                    ]
                },
            },
            "https://www.data.gov.uk/dataset/ff69f339-99a6-4e50-9ca0-de1be386f0b9/transport",
        ),
        (
            EarthdataProvider,
            "earthdata.nasa.gov",
            "page_size",
            {
                "feed": {
                    "entry": [
                        {
                            "title": "Earth observations",
                            "id": "C100-NASA",
                            "summary": "Instrument data",
                        }
                    ]
                }
            },
            "https://search.earthdata.nasa.gov/search/granules?p=C100-NASA",
        ),
        (
            WorldBankReportsProvider,
            "documents.worldbank.org",
            "rows",
            {
                "documents": {
                    "D33408293": {
                        "display_title": "Air Transport Annual Report",
                        "url": "http://documents.worldbank.org/curated/en/839601631518964272",
                        "docdt": "2020-12-31T05:00:00Z",
                        "docty": "Annual Report",
                        "abstracts": {"cdata!": "<p>Air traffic statistics</p>"},
                    },
                    "outside": {
                        "display_title": "External",
                        "url": "https://example.org/publication",
                    },
                    "facets": {},
                }
            },
            "https://documents.worldbank.org/curated/en/839601631518964272",
        ),
        (
            ClinicalTrialsRegistryProvider,
            "clinicaltrials.gov",
            "pageSize",
            {
                "studies": [
                    {
                        "protocolSection": {
                            "identificationModule": {
                                "nctId": "NCT01234567",
                                "briefTitle": "Cabin ventilation",
                            },
                            "statusModule": {
                                "studyFirstPostDateStruct": {"date": "2026-01-01"},
                                "overallStatus": "COMPLETED",
                            },
                            "descriptionModule": {"briefSummary": "Cabin ventilation observations"},
                        }
                    },
                    {"protocolSection": {"identificationModule": {"nctId": "../non-study"}}},
                ],
                "nextPageToken": "not-consumed",
            },
            "https://clinicaltrials.gov/study/NCT01234567",
        ),
    ],
)
async def test_official_provider_caps_requests_and_returns_catalog_links(
    provider_class, domain, parameter, payload, expected_url
):
    def upstream(request):
        assert request.method == "GET"
        assert request.url.params[parameter] == "3"
        if provider_class is UKOpenDataProvider:
            assert request.url.host == "ckan.publishing.service.gov.uk"
        assert "site:" not in str(dict(request.url.params))
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        provider = provider_class(client)
        request = SearchRequest(query=f"site:{domain} aviation", domain_allowlist=[domain])
        assert provider.supports_request(request)
        assert not provider.supports_request(SearchRequest(query="aviation summer timetable"))
        assert not provider.supports_request(
            request.model_copy(update={"domain_denylist": [domain]})
        )
        result = await provider.search(request, Source.WEB, Modality.TEXT, 30, 2)
    assert [row.url for row in result.results] == [expected_url]
    assert result.results[0].snippet


async def test_official_provider_reports_retry_after():
    responses = httpx.MockTransport(lambda _: httpx.Response(429, headers={"retry-after": "40"}))
    async with httpx.AsyncClient(transport=responses) as client:
        provider = FederalRegisterProvider(client)
        with pytest.raises(ProviderFailure) as failure:
            await provider.search(
                SearchRequest(query="FAA rulemaking"), Source.WEB, Modality.TEXT, 2, 2
            )
    assert failure.value.status == "rate_limited"
    assert failure.value.retry_after == 40


async def test_france_catalogue_keeps_open_records_from_public_publishers():
    record = {
        "title": "Aéroports d’Occitanie",
        "page": "https://www.data.gouv.fr/datasets/aeroports-doccitanie",
        "description": "<p>Statistiques aéroportuaires.</p>",
        "private": False,
        "access_type": "open",
        "organization": {"name": "Région Occitanie", "badges": [{"kind": "local-authority"}]},
        "created_at": "2026-11-17T15:08:40+00:00",
    }
    calls = []

    def upstream(request):
        calls.append(request)
        assert request.url.host == "www.data.gouv.fr"
        assert request.url.path == "/api/1/datasets/"
        assert dict(request.url.params) == {"q": "aéroport", "page_size": "3"}
        return httpx.Response(
            200,
            json={
                "data": [
                    record,
                    {**record, "organization": {"badges": [{"kind": "company"}]}},
                    {**record, "access_type": "restricted"},
                    {**record, "page": "https://www.data.gouv.fr/datasets/unrequested"},
                ],
                "next_page": "https://www.data.gouv.fr/api/1/datasets/?page=2",
            },
        )

    query = SearchRequest(query="site:data.gouv.fr aéroport", language="fr-FR", region="FR")
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        provider = FranceOpenDataProvider(client)
        assert provider.supports_request(query)
        assert not provider.supports_request(query.model_copy(update={"region": "CA"}))
        assert not provider.supports_request(SearchRequest(query="site:data.gouv.fr"))
        assert not provider.supports_request(
            SearchRequest(query=query.query, time_range={"from": "2025-01-01", "to": "2026-12-31"})
        )
        result = await provider.search(query, Source.WEB, Modality.TEXT, 30, 2)

    assert len(calls) == 1
    (item,) = result.results
    assert item.url == record["page"]
    assert item.snippet == "Statistiques aéroportuaires."
    assert item.authors == ["Région Occitanie"]
    assert item.published_at is None


async def test_australia_open_data_returns_public_catalogue_records_without_guessed_dates():
    domain = "data.gov.au"
    calls = []
    dataset = {
        "type": "dataset",
        "private": False,
        "state": "active",
        "notes": "<p>Monthly airport traffic totals.</p><script>ignore me</script>",
        "url": "https://example.com/resource",
        "metadata_created": "2015-02-13T00:11:39.314956",
        "metadata_modified": "2026-11-20T05:31:45Z",
        "license_title": "Creative Commons Attribution 3.0 Australia",
        "license_url": "https://creativecommons.org/licenses/by/3.0/au/",
    }
    records = [
        {
            **dataset,
            "id": f"0000000{i}-5850-47f3-815d-08289b22f5a8",
            "name": f"airport-data-{i}",
            "title": f"Airport Traffic Data {i}",
        }
        for i in range(7)
    ]
    records[0]["archived"] = True
    records[1]["unpublished"] = True
    records[2]["private"] = True

    def upstream(request):
        calls.append(request)
        assert request.url.host == domain
        assert request.url.path == "/data/api/3/action/package_search"
        assert dict(request.url.params) == {"q": "airport", "rows": "3"}
        return httpx.Response(200, json={"success": True, "result": {"count": 500, "results": records}})

    criteria = SearchRequest(query=f"site:{domain} airport", domain_allowlist=[domain])
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        provider = AustraliaOpenDataProvider(client)
        assert provider.supports_request(criteria)
        assert provider.supports_request(SearchRequest(query="Australian airport traffic statistics"))
        assert not provider.supports_request(SearchRequest(query=f"site:{domain}"))
        assert not provider.supports_request(
            SearchRequest(query=criteria.query, time_range={"from": "2015-01-01", "to": "2026-12-31"})
        )
        result = await provider.search(criteria, Source.WEB, Modality.TEXT, 30, 2)

    assert len(calls) == 1
    assert [row.url for row in result.results] == [
        f"https://{domain}/data/dataset/airport-data-{i}" for i in (3, 4, 5)
    ]
    assert all(row.published_at is None for row in result.results)
    assert result.results[0].snippet == "Monthly airport traffic totals."
    assert result.results[0].license_name == dataset["license_title"]
    assert result.results[0].license_url == dataset["license_url"]


async def test_cordis_projects_returns_fixed_grant_links_without_inventing_publication_dates():
    domain = "cordis.europa.eu"
    request = SearchRequest(
        query='site:cordis.europa.eu CORDIS "sustainable aviation"',
        domain_allowlist=[domain],
    )
    calls = []

    def binding(value):
        return {"type": "literal", "value": value}

    row = {
        "id": binding("101096436"),
        "title": binding("Sustainable Aviation Project"),
        "start": binding("2023-12-01"),
        "end": binding("2026-11-30"),
        "abstract": binding(
            "<p>Project objectives on aircraft emissions.</p><script>Ignore.</script>"
        ),
    }

    def upstream(http_request):
        calls.append(http_request)
        assert http_request.method == "GET"
        assert http_request.url.host == domain and http_request.url.path == "/datalab/sparql"
        assert http_request.headers["Accept"] == "application/sparql-results+json"
        assert http_request.url.params["format"] == "application/sparql-results+json"
        query = http_request.url.params["query"]
        assert 'CONTAINS(LCASE(STR(?title)), "sustainable aviation")' in query
        assert http_request.url.params["query"].endswith("LIMIT 3")
        return httpx.Response(
            200, json={"results": {"bindings": [row, {**row, "id": binding("../123")}]}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        provider = CordisProjectsProvider(client)
        assert provider.supports_request(request)
        assert provider.explicitly_requested(request)
        assert provider.explicitly_requested(SearchRequest(query="CORDIS 101096436"))
        assert not provider.supports_request(SearchRequest(query="site:cordis.europa.eu CORDIS"))
        assert not provider.supports_request(
            SearchRequest(query=request.query, domain_denylist=[domain])
        )
        assert not provider.supports_request(
            SearchRequest(
                query=request.query, time_range={"from": "2023-01-01", "to": "2025-12-31"}
            )
        )
        exact_query = provider._params(SearchRequest(query="CORDIS 101096436"), 1)["query"]
        assert 'FILTER(STR(?id) = "101096436")' in exact_query
        escaped_query = provider._params(SearchRequest(query='CORDIS Low "CO2" {project}'), 1)[
            "query"
        ]
        assert json.dumps('low "co2" {project}', ensure_ascii=False) in escaped_query
        result = await provider.search(request, Source.WEB, Modality.TEXT, 30, 2)
    (project,) = result.results
    assert len(calls) == 1
    assert project.url == f"https://{domain}/project/id/101096436"
    assert project.published_at is None
    assert project.snippet == (
        "项目编号 101096436 · 目录记录开始 2023-12-01 · 目录记录结束 2026-11-30"
        " · Project objectives on aircraft emissions."
    )
    assert result.partial and result.warnings == ["eu-cordis-projects:invalid_metadata:1"]


async def test_osti_returns_bounded_official_citations_with_publication_dates():
    domain = "www.osti.gov"
    query = SearchRequest(
        query='site:osti.gov DOE OSTI "sustainable aviation fuel"',
        language="en",
        domain_allowlist=["osti.gov"],
        time_range={"from": "2025-01-01", "to": "2025-12-31"},
    )
    calls = []
    report = {
        "osti_id": "2519255",
        "title": "Sustainable aviation fuel report",
        "description": "<p>Fuel life-cycle analysis.</p><script>ignore me</script>",
        "publication_date": "2025-12-31T00:00:00Z",
        "entry_date": "2026-01-10T00:00:00Z",
        "language": "English",
        "authors": ["Example, Alex", None],
        "doi": "10.2172/2519255",
        "links": [{"rel": "citation", "href": "https://example.com/invalid"}],
    }

    def upstream(request):
        calls.append(request)
        assert request.url.host == domain
        assert request.url.path == "/api/v1/records"
        assert request.headers["Accept"] == "application/json"
        assert dict(request.url.params) == {
            "q": '"sustainable aviation fuel"',
            "rows": "3",
            "language": "English",
            "publication_date_start": "01/01/2025",
            "publication_date_end": "12/31/2025",
        }
        return httpx.Response(200, json=[
            {**report, "osti_id": "../2519255"},
            {**report, "osti_id": "2519256", "publication_date": None},
            {**report, "osti_id": "2519257", "publication_date": "2026-01-01"},
            report,
        ])

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        provider = OstiResearchProvider(client)
        assert provider.supports_request(query)
        assert provider.supports_request(SearchRequest(query="aviation fuel life cycle"))
        assert not provider.supports_request(SearchRequest(query="site:osti.gov DOE OSTI"))
        assert not provider.supports_request(SearchRequest(query="aviation summer timetable"))
        result = await provider.search(query, Source.WEB, Modality.TEXT, 30, 2)
    (record,) = result.results
    assert len(calls) == 1
    assert record.url == f"https://{domain}/biblio/2519255"
    assert record.published_at.date().isoformat() == "2025-12-31"
    assert record.snippet == "Fuel life-cycle analysis."
    assert record.authors == ["Example, Alex"]
    assert record.doi == "10.2172/2519255"


async def test_noaa_caps_catalogue_results_and_keeps_observation_dates_separate():
    domain = "www.ncei.noaa.gov"
    catalogue = f"https://{domain}/access/metadata/landing-page/bin/iso"
    calls = []

    def upstream(request):
        calls.append(request)
        assert request.url.host == domain
        assert request.url.path == "/access/services/search/v1/datasets"
        assert dict(request.url.params) == {"text": "wind gust"}
        results = [
            {
                "fileId": f"gov.noaa.ncdc:C{i:05d}",
                "name": f"Wind observations {i}",
                "description": "<p>Historical airport observations</p>",
                "startDate": "2000-01-01",
                "endDate": "2026-11-21",
            }
            for i in range(4)
        ]
        return httpx.Response(200, json={"results": [
            {"fileId": "https://example.com/data", "name": "External"}, *results,
        ]})

    query = SearchRequest(query=f"site:{domain} NOAA wind gust", domain_allowlist=[domain])
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        provider = NoaaClimateDataProvider(client)
        assert provider.supports_request(query)
        assert not provider.supports_request(SearchRequest(query=f"site:{domain}"))
        assert not provider.supports_request(SearchRequest(query="airport timetables"))
        assert not provider.supports_request(
            SearchRequest(query=query.query, time_range={"from": "2026-01-01", "to": "2026-12-31"})
        )
        result = await provider.search(query, Source.WEB, Modality.TEXT, 30, 2)
    assert len(calls) == 1
    assert [row.url for row in result.results] == [
        f"{catalogue}?id=gov.noaa.ncdc:C{i:05d}" for i in range(3)
    ]
    assert all(row.published_at is None for row in result.results)
    assert result.results[0].snippet == "Historical airport observations"


async def test_transportation_statistics_keeps_bounded_official_dataset_links():
    domain = "data.bts.gov"
    url = f"https://{domain}/Aviation/Commercial-Aviation-Departures/bpqk-hyst"
    resource = {
        "id": "bpqk-hyst",
        "type": "dataset",
        "provenance": "official",
        "name": "Commercial Aviation Departures",
        "description": "Daily counts of commercial flights",
        "updatedAt": "2026-11-20T12:30:00Z",
    }
    record = {"resource": resource, "metadata": {"domain": domain}, "link": url}

    def upstream(request):
        assert request.url.host == "api.us.socrata.com"
        assert dict(request.url.params) == {
            "search_context": domain,
            "q": "aviation",
            "only": "datasets",
            "provenance": "official",
            "limit": "3",
        }
        return httpx.Response(
            200,
            json={
                "results": [
                    {**record, "resource": {**resource, "provenance": "community"}},
                    {**record, "link": f"https://another.example/{resource['id']}"},
                    {**record, "link": f"{url}?redirect=external"},
                    {**record, "resource": {**resource, "name": "Outside the candidate budget"}},
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        provider = TransportationStatisticsProvider(client)
        search = SearchRequest(query=f"site:{domain} aviation", domain_allowlist=[domain])
        assert provider.supports_request(search)
        assert provider.supports_request(SearchRequest(query="commercial aviation departures"))
        assert not provider.supports_request(SearchRequest(query="FAA airworthiness directives"))
        assert not provider.supports_request(SearchRequest(query="Europe PMC aviation noise"))
        assert not provider.supports_request(
            SearchRequest(query=search.query, time_range={"from": "2020-01-01", "to": "2021-01-01"})
        )
        (row,) = (await provider.search(search, Source.WEB, Modality.TEXT, 30, 3)).results
    assert row.url == url
    assert row.snippet == resource["description"]
    assert row.published_at is None
    assert row.provider_ids == ["us-bts-open-data"]


def european_dataset(identity, catalogue, publisher, **fields):
    return {
        "id": identity,
        "index": "dataset",
        "catalog": {"id": catalogue},
        "resource": f"http://data.europa.eu/88u/dataset/{identity}",
        "publisher": publisher,
        "title": {"en": "Public dataset " + identity},
        "description": {
            "en": "<p>Official description <b>with context</b>.</p><script>omit</script>"
        },
        **fields,
    }


async def test_european_catalogue_keeps_institutions_and_consistent_distribution_licenses():
    license = {"resource": "http://spdx.org/licenses/CC-BY-4.0", "label": "CC-BY-4.0"}
    rows = [
        european_dataset(
            "eurostat-one",
            "estat",
            {
                "resource": "http://publications.europa.eu/resource/authority/corporate-body/ESTAT",
            },
            issued="2020-06-15",
            distributions=[{"license": license}],
        ),
        european_dataset(
            "air-quality",
            "eea-sdi",
            {"homepage": "http://www.eea.europa.eu#organization"},
            issued="2020-06-16T01:15:00+02:00",
            distributions=[{"license": license}, {}],
        ),
        european_dataset(
            "project-portfolio",
            "cinea",
            {
                "resource": "http://publications.europa.eu/resource/authority/corporate-body/CINEA",
            },
            modified="2026-10-01",
            distributions=[
                {"license": license},
                {
                    "license": {
                        "resource": "https://creativecommons.org/publicdomain/zero/1.0/",
                        "label": "CC0",
                    }
                },
            ],
        ),
    ]

    def upstream(request):
        assert request.url.host == "data.europa.eu"
        query = dict(request.url.params)
        assert json.loads(query.pop("facets")) == {"catalog": ["estat", "eea-sdi", "cinea"]}
        assert query == {
            "q": "transport energy",
            "filters": "dataset",
            "aggregation": "false",
            "limit": "3",
        }
        return httpx.Response(200, json={"result": {"results": [*rows, rows[0]]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        provider = EuropeanDataPortalProvider(client)
        request = SearchRequest(query="site:data.europa.eu transport energy")
        result = await provider.search(request, Source.WEB, Modality.TEXT, 30, 3)
    assert [row.url for row in result.results] == [
        f"https://data.europa.eu/data/datasets/{item['id']}?locale=en" for item in rows
    ]
    official, environment, agency = result.results
    assert official.authors == ["Eurostat"]
    assert official.snippet == "Eurostat · Official description with context."
    assert official.license_name == "CC-BY-4.0"
    assert official.license_url == license["resource"]
    assert environment.authors == ["European Environment Agency"]
    assert environment.published_at.date().isoformat() == "2020-06-15"
    assert environment.license_url is None
    assert agency.authors[0].startswith("European Climate")
    assert agency.license_url is None
    assert agency.published_at is None


async def test_european_named_catalogue_filters_dates_and_rejects_unverified_records():
    trusted = european_dataset(
        "statistics",
        "estat",
        {
            "resource": "http://publications.europa.eu/resource/authority/corporate-body/ESTAT",
        },
        issued="2019-03-15",
        modified="2020-03-15",
    )

    def upstream(request):
        query = dict(request.url.params)
        assert json.loads(query.pop("facets")) == {"catalog": ["estat"]}
        assert query == {
            "q": "energy prices",
            "filters": "dataset",
            "aggregation": "false",
            "limit": "3",
            "dateType": "issued",
            "minDate": "2020-01-01T00:00:00Z",
            "maxDate": "2020-12-31T23:59:59.999Z",
        }
        return httpx.Response(
            200,
            json={
                "result": {
                    "results": [
                        trusted,
                        {**trusted, "issued": "2020-03-15", "access_rights": {"id": "RESTRICTED"}},
                        {
                            **trusted,
                            "issued": "2020-03-15",
                            "publisher": {"resource": "https://another.example/ESTAT"},
                        },
                    ]
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        provider = EuropeanDataPortalProvider(client)
        request = SearchRequest(
            query="Eurostat energy prices", time_range={"from": "2020-01-01", "to": "2020-12-31"}
        )
        assert provider.supports_request(request)
        assert provider.supports_request(
            SearchRequest(query="European Environment Agency air quality")
        )
        assert not provider.supports_request(SearchRequest(query="FAA airworthiness directives"))
        assert not provider.supports_request(request.model_copy(update={"region": "US"}))
        assert not (await provider.search(request, Source.WEB, Modality.TEXT, 3, 3)).results


async def test_canada_returns_public_catalogue_rows_with_date_filter():
    def upstream(request):
        assert request.url.host == "open.canada.ca"
        assert dict(request.url.params) == {
            "rows": "2",
            "q": "aviation",
            "fq": "portal_type:dataset AND metadata_created:[2020-01-01T00:00:00Z TO 2021-01-01T23:59:59.999Z]",
        }
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "results": [
                        {
                            "id": "08132980-c6b8-45e5-a9e0-9a88f9808c8a",
                            "title": "Aviation statistics",
                            "notes": "Published dataset",
                            "state": "active",
                            "private": False,
                            "metadata_created": "2020-04-27T12:54:54",
                        },
                    ]
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        provider = CanadaOpenDataProvider(client)
        search = SearchRequest(
            query="site:open.canada.ca aviation",
            time_range={"from": "2020-01-01", "to": "2021-01-01"},
        )
        assert provider.supports_request(search)
        (row,) = (await provider.search(search, Source.WEB, Modality.TEXT, 2, 3)).results
    assert row.url == "https://open.canada.ca/data/en/dataset/08132980-c6b8-45e5-a9e0-9a88f9808c8a"


async def test_usgs_keeps_exact_publication_and_skips_missing_or_unverified_dates():
    def upstream(request):
        assert request.url.host == "pubs.usgs.gov"
        assert dict(request.url.params) == {"q": "groundwater", "page_size": "3"}
        return httpx.Response(
            200,
            json={
                "records": [
                    {
                        "indexId": "sir20265077",
                        "title": "Groundwater report",
                        "publicationDate": "2026-11-20",
                        "docAbstract": "Verified metadata",
                        "links": [
                            {
                                "type": {"id": 39},
                                "url": "https://elsewhere.example.org/not-the-report",
                            },
                            {"type": {"id": 11}, "url": "https://pubs.usgs.gov/sir/report.pdf"},
                            {
                                "type": {"id": 39},
                                "url": "https://pubs.usgs.gov/publication/sir20265077/full",
                            },
                        ],
                    },
                    {
                        "indexId": "..",
                        "title": "Invalid citation location",
                        "publicationDate": "2026-11-20",
                    },
                    {"indexId": "fs20263027", "title": "Unknown date"},
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        provider = USGSProvider(client)
        search = SearchRequest(
            query="groundwater", time_range={"from": "2026-01-01", "to": "2026-12-31"}
        )
        assert provider.capabilities.filters[Modality.TEXT].date_range == "metadata_postfilter"
        assert provider.supports_request(search)
        result = await provider.search(search, Source.WEB, Modality.TEXT, 10, 3)
    assert [row.url for row in result.results] == [
        "https://pubs.usgs.gov/publication/sir20265077/full"
    ]


async def test_europe_pmc_queries_public_articles_and_keeps_verified_identifiers():
    def upstream(request):
        assert request.url.host == "www.ebi.ac.uk"
        assert dict(request.url.params) == {
            "query": "OPEN_ACCESS:Y AND (aviation noise)",
            "resultType": "core",
            "format": "json",
            "pageSize": "3",
        }
        return httpx.Response(
            200,
            json={
                "resultList": {
                    "result": [
                        {
                            "pmcid": "PMC12390125",
                            "isOpenAccess": "Y",
                            "firstPublicationDate": "2025-06-15",
                            "title": "Aviation noise measurements",
                            "abstractText": "<p>Open findings</p>",
                        },
                        {"pmcid": "PMC123456", "isOpenAccess": "N", "title": "Limited access"},
                        {"pmcid": "../invalid", "isOpenAccess": "Y", "title": "Invalid identifier"},
                    ]
                }
            },
        )

    request = SearchRequest(
        query="site:europepmc.org aviation noise", domain_allowlist=["europepmc.org"]
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        provider = EuropePMCProvider(client)
        assert provider.supports_request(SearchRequest(query="aviation noise"))
        assert not provider.supports_request(SearchRequest(query="air traffic capacity"))
        result = await provider.search(request, Source.WEB, Modality.TEXT, 10, 3)
    (row,) = result.results
    assert row.url == "https://europepmc.org/article/PMC/PMC12390125"
    assert row.published_at.date().isoformat() == "2025-06-15"
    assert row.snippet == "Open findings"
