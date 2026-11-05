import httpx
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .crawl_parser import CrawlParser
from .evidence import EvidenceBuilder
from .image_attribution import ImageAttributionResolver
from .image_source import PublicImageSourceResolver
from .local_index import LocalIndex
from .observability import FetchMetrics
from .ops_store import OpsStore
from .policy import ProviderPolicy
from .providers import create_providers
from .safe_fetch import SafeFetcher
from .search import SearchService
from .settings import Settings
from .vertical_providers import create_vertical_providers


def build_evidence(
    config: Settings, client: httpx.AsyncClient, fetch_metrics: FetchMetrics
) -> EvidenceBuilder:
    parser = (
        CrawlParser(client, config.crawl4ai_url, config.crawl4ai_token)
        if config.crawl4ai_url
        else None
    )
    return EvidenceBuilder(SafeFetcher(), parser, record_attempt=fetch_metrics.record)


async def load_search_policies(search: SearchService, db: AsyncEngine, ops: OpsStore) -> None:
    routing = await ops.active_routing()
    async with db.connect() as connection:
        policies = await connection.execute(
            text("""
            SELECT provider_id, enabled, priority, requests_per_minute FROM provider_policies
        """)
        )
        for provider_id, enabled, priority, rpm in policies:
            if provider_id in search.provider_policies:
                search.provider_policies[provider_id] = ProviderPolicy(enabled, priority, rpm)
    if routing.version >= search.routing.version:
        search.routing = routing


async def build_search(
    config: Settings,
    db: AsyncEngine,
    redis: Redis,
    client: httpx.AsyncClient,
    ops: OpsStore,
    evidence: EvidenceBuilder,
) -> SearchService:
    providers = create_providers(config, client)
    providers.extend(
        create_vertical_providers(
            client,
            enabled_sources=config.vertical_sources,
            openalex_key=config.openalex_key,
            github_token=config.github_token,
            crossref_email=config.crossref_email,
        )
    )
    search = SearchService(
        redis,
        providers,
        config,
        ImageAttributionResolver(client),
        evidence,
        PublicImageSourceResolver(evidence.fetcher),
        LocalIndex(db) if config.enable_local_index else None,
    )
    search.load_cache_epochs = ops.cache_epochs
    await load_search_policies(search, db, ops)
    return search
