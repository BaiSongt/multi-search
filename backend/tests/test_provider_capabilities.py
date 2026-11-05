import httpx
from fastapi.encoders import jsonable_encoder

from multisearch.contracts import Modality, Source
from multisearch.providers import DDGSProvider, SearXNGProvider
from multisearch.search import SearchService
from multisearch.settings import Settings
from multisearch.vertical_providers import (
    ArxivProvider,
    CrossrefProvider,
    GitHubProvider,
    OpenAlexProvider,
)


async def test_outbound_provider_status_declares_effective_capabilities():
    settings = Settings("postgres://unused", "redis://unused", "pepper", "http://searxng:8080")
    async with httpx.AsyncClient() as client:
        providers = [
            SearXNGProvider(settings.searxng_url, client),
            DDGSProvider(),
            OpenAlexProvider(client),
            CrossrefProvider(client),
            ArxivProvider(client),
            GitHubProvider(client, "fixture-secret"),
        ]
        rows = jsonable_encoder(SearchService(None, providers, settings).provider_status())
    by_provider = {row["provider_id"]: row for row in rows}
    assert "fixture-secret" not in repr(rows)

    for row in rows:
        assert row["network_scope"] == "public"
        assert row["enabled"]
        assert row["status"] == "unknown"
        assert row["requests_per_minute"] == 120
        assert row["capabilities"]["supports_pagination"] is False
        assert row["capabilities"]["max_concurrent_requests"] == 5
        assert row["capabilities"]["requires_api_key"] == (row["provider_id"] == "github")
        assert row["combinations"] == [
            {"source_type": pair["source_type"], "modality": pair["modality"]}
            for pair in row["pair_health"]
        ]
        assert {pair["status"] for pair in row["pair_health"]} == {"unknown"}

    for name in ("searxng", "ddgs"):
        row = by_provider[name]
        assert row["capabilities"]["source_types"] == ["news", "web"]
        assert row["capabilities"]["modalities"] == ["image", "text"]
        assert row["capabilities"]["max_upstream_concurrent_requests"] == 5
        for pair in row["pair_health"]:
            filters = pair["filters"]
            assert filters["date_range"] == "metadata_postfilter"
            if pair["modality"] == "image":
                assert filters["image_options"] == {
                    "upstream": [],
                    "candidate_postfilter": [
                        "min_width",
                        "min_height",
                        "orientation",
                        "source_page_required",
                    ],
                    "platform_postfilter": ["license_policy"],
                    "unsupported": ["image_type"],
                }
                if name == "ddgs":
                    assert filters["language"] == filters["region"] == "unsupported"
                    continue
            else:
                assert filters["image_options"] is None
            if name == "ddgs":
                assert filters["language"] == "upstream_requires_region"
                assert filters["region"] == "upstream_best_effort"
            else:
                assert filters["language"] == "upstream_best_effort"
                assert filters["region"] == "upstream_requires_language"

    for name, date_field in (
        ("openalex", "published_at"),
        ("crossref", "published_at"),
        ("arxiv", "submitted_at"),
        ("github", "created_at"),
    ):
        row = by_provider[name]
        assert row["capabilities"]["source_types"] == ["code" if name == "github" else "academic"]
        assert row["capabilities"]["modalities"] == ["text"]
        assert row["capabilities"]["max_upstream_concurrent_requests"] == (
            5 if name == "openalex" else 1
        )
        [pair] = row["pair_health"]
        assert pair["filters"] == {
            "language": "unsupported",
            "region": "unsupported",
            "date_range": "upstream_and_metadata_postfilter",
            "date_field": date_field,
            "exact_identifier_date_range": None if name == "github" else "metadata_postfilter",
            "image_options": None,
        }


def test_provider_status_preserves_legacy_adapter_and_hides_disabled_images():
    class CustomProvider:
        provider_id = "custom"
        pairs = frozenset({(Source.WEB, Modality.TEXT), (Source.WEB, Modality.IMAGE)})

    settings = Settings(
        "postgres://unused", "redis://unused", "pepper", "http://searxng:8080", enable_images=False
    )
    custom, ddgs = jsonable_encoder(
        SearchService(None, [CustomProvider(), DDGSProvider()], settings).provider_status()
    )
    assert custom["capabilities"] == {
        "source_types": ["web"],
        "modalities": ["text"],
        "supports_pagination": None,
        "requires_api_key": None,
        "max_concurrent_requests": 5,
        "max_upstream_concurrent_requests": None,
    }
    assert custom["combinations"] == [{"source_type": "web", "modality": "text"}]
    assert custom["pair_health"][0]["filters"] is None
    assert ddgs["capabilities"]["modalities"] == ["text"]
    assert all(pair["filters"]["image_options"] is None for pair in ddgs["pair_health"])
