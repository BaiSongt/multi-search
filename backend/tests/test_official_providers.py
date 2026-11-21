import httpx
import pytest

from multisearch.contracts import Modality, SearchRequest, Source
from multisearch.official_providers import (
    EarthdataProvider,
    FederalRegisterProvider,
    UKOpenDataProvider,
)
from multisearch.providers import ProviderFailure


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
