"""在 Compose API 容器内验证 Commons 实时许可与 L2、L3 严格过滤。

POSIX shell:
    docker compose exec -T multisearch-api python - < scripts/probe_image_license.py
PowerShell:
    Get-Content -Raw -Encoding UTF8 scripts/probe_image_license.py | docker compose exec -T multisearch-api python -

检索候选固定。真实解析器访问 Commons，且 L3 会尝试核对公开对照页面。
探针绕过缓存和服务入口计量，不读取密钥或连接 PostgreSQL、Redis。
"""

import asyncio
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx

from multisearch.contracts import (
    ImageAttribution,
    ImageSearchResult,
    Modality,
    SearchRequest,
    Source,
)
from multisearch.evidence import EvidenceBuilder
from multisearch.image_attribution import ImageAttributionResolver
from multisearch.image_source import PublicImageSourceResolver
from multisearch.providers import ProviderResult
from multisearch.safe_fetch import SafeFetcher
from multisearch.search import SearchService
from multisearch.settings import Settings

PAGE = "https://commons.wikimedia.org/wiki/File:Common_Kingfisher_Alcedo_atthis.jpg"
ASSET = "https://upload.wikimedia.org/wikipedia/commons/c/cc/Common_Kingfisher_Alcedo_atthis.jpg"


def candidate(name, page, original, status="unknown"):
    return ImageSearchResult(
        result_id=name,
        title=name,
        source_type=Source.WEB,
        provider_ids=["probe"],
        url=page,
        canonical_url=page,
        retrieved_at=datetime.now(UTC),
        image=ImageAttribution(original_url=original, source_page_url=page, rights_status=status),
    )


class FixedCandidates:
    provider_id = "probe"
    pairs = {(Source.WEB, Modality.IMAGE)}

    async def search(self, request, source, modality, limit, timeout_s):
        assert request.image_options.license_policy == "any_with_status"
        hits = [candidate("commons", PAGE, ASSET)]
        for status in ("unknown", "declared_unverified", "conflict"):
            page = f"https://example.org/{status}"
            hits.append(candidate(status, page, f"{page}.jpg", status))
        return ProviderResult(hits)


async def main():
    settings = Settings(database_url="", redis_url="", key_pepper="", searxng_url="")
    request = SearchRequest(
        query="common kingfisher attribution",
        mode="standard",
        source_types=["web"],
        modalities=["image"],
        max_results=10,
        max_latency_ms=15_000,
        max_image_metadata_fetches=5,
        image_options={"license_policy": "verified_only"},
        cache_policy="bypass",
    )
    async with (
        asyncio.timeout(40),
        httpx.AsyncClient(trust_env=False, follow_redirects=False) as client,
    ):
        fetcher = SafeFetcher()
        search = SearchService(
            None,
            [FixedCandidates()],
            settings,
            ImageAttributionResolver(client),
            EvidenceBuilder(fetcher),
            PublicImageSourceResolver(fetcher),
        )
        for mode, lookups in (("standard", 1), ("evidence", 4)):
            body = SearchRequest.model_validate({**request.model_dump(), "mode": mode})
            result = await search.search(body, "synthetic-attribution", mode, metered=True)
            assert [item.result_id for item in result.results] == ["commons"], result.warnings
            image = result.results[0].image
            assert image.source_page_verified and image.rights_status == "verified_license"
            assert image.license_name == "CC BY-SA 2.5"
            evidence_url = urlsplit(image.rights_evidence_url or "")
            assert evidence_url.hostname == "commons.wikimedia.org"
            assert evidence_url.path == "/w/index.php"
            assert int(parse_qs(evidence_url.query)["oldid"][0]) > 0
            assert result.budget.image_metadata_fetches == lookups
            print(mode, result.status, image.license_name, image.rights_evidence_url, lookups)

        body = request.model_copy(update={"max_image_metadata_fetches": 0})
        zero = await search.search(body, "synthetic-attribution", "budget-zero", metered=True)
        assert zero.results == [] and zero.budget.image_metadata_fetches == 0
        assert "IMAGE_LICENSE_VERIFICATION_UNAVAILABLE" in zero.warnings
        print("通过零核验预算过滤")


if __name__ == "__main__":
    asyncio.run(main())
