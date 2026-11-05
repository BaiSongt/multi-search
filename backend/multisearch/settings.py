import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit

from .deployment_policy import DeploymentPolicy, load_deployment_policy
from .render_resources import ResourceSpec, parse_resource_manifest
from .safe_fetch import FetchError, validate_fetch_url

_DYNAMIC_DOMAIN = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?"
)


def secret(name: str) -> str:
    if file := os.getenv(f"{name}_FILE"):
        return Path(file).read_text(encoding="utf-8").strip()
    return os.getenv(name, "")


def _dynamic_domains(name: str) -> frozenset[str]:
    value = os.getenv(name, "").strip()
    if not value:
        return frozenset()
    hosts = [host.strip() for host in value.split(",")]
    if any(
        not host.isascii() or len(host) > 253 or _DYNAMIC_DOMAIN.fullmatch(host.lower()) is None
        for host in hosts
    ):
        raise ValueError(f"{name} 需要逗号分隔的精确公网域名")
    domains = frozenset(host.lower() for host in hosts)
    try:
        for host in domains:
            validate_fetch_url(f"https://{host}/")
    except FetchError:
        raise ValueError(f"{name} 需要逗号分隔的精确公网域名") from None
    return domains


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    redis_url: str
    key_pepper: str = field(repr=False)
    searxng_url: str
    enable_ddgs: bool = True
    enable_images: bool = True
    max_concurrent: int = 20
    requests_per_minute: int = 60
    vertical_sources: frozenset[str] = frozenset()
    openalex_key: str = field(default="", repr=False)
    github_token: str = field(default="", repr=False)
    crossref_email: str = field(default="", repr=False)
    crawl4ai_url: str = ""
    crawl4ai_token: str = field(default="", repr=False)
    enable_local_index: bool = False
    enable_research: bool = False
    research_model_backend: str = ""
    research_model_base_url: str = field(default="", repr=False)
    research_model_name: str = ""
    research_model_api_key: str = field(default="", repr=False)
    research_cost_currency: str = ""
    research_input_per_million_tokens: str = ""
    research_output_per_million_tokens: str = ""
    deployment_policy: DeploymentPolicy = field(default_factory=DeploymentPolicy)
    dynamic_fetch_domains: frozenset[str] = frozenset()
    dynamic_fetch_resource_domains: frozenset[str] = frozenset()
    enable_dynamic_fetch: bool = False
    renderer_token: str = field(default="", repr=False)
    dynamic_fetch_resources: dict[str, tuple[ResourceSpec, ...]] = field(
        default_factory=dict, repr=False
    )

    @classmethod
    def from_env(cls) -> "Settings":
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            password = secret("POSTGRES_PASSWORD")
            if not password:
                raise ValueError("缺少 PostgreSQL 连接配置")
            database_url = (
                f"postgresql+asyncpg://multisearch:{quote(password, safe='')}@postgres/multisearch"
            )
        pepper = secret("API_KEY_HASH_PEPPER")
        if len(pepper.encode()) < 32:
            raise ValueError("API Key pepper 至少需要 32 字节")
        max_concurrent = int(os.getenv("MULTISEARCH_MAX_CONCURRENT_REQUESTS", "20"))
        if not 1 <= max_concurrent <= 20:
            raise ValueError("MULTISEARCH_MAX_CONCURRENT_REQUESTS 需要在 1–20 之间")
        vertical_sources = {
            source.strip().lower()
            for source in os.getenv("MULTISEARCH_VERTICAL_SOURCES", "").split(",")
            if source.strip()
        }
        academic = {"openalex", "crossref", "arxiv"}
        if os.getenv("MULTISEARCH_ENABLE_ACADEMIC", "false").lower() == "true":
            vertical_sources.update(academic)
        if not vertical_sources <= academic | {"github"}:
            raise ValueError("MULTISEARCH_VERTICAL_SOURCES 包含不支持的来源")
        crawl4ai_url = os.getenv("CRAWL4AI_BASE_URL", "").rstrip("/")
        crawl4ai_token = secret("CRAWL4AI_API_TOKEN")
        if crawl4ai_url and (crawl4ai_url != "http://crawl4ai:11235" or len(crawl4ai_token) < 32):
            raise ValueError("Crawl4AI 需要固定内部地址及专用令牌")
        dynamic_domains = _dynamic_domains("MULTISEARCH_DYNAMIC_FETCH_DOMAINS")
        resource_domains = _dynamic_domains("MULTISEARCH_DYNAMIC_FETCH_RESOURCE_DOMAINS")
        dynamic_enabled = (
            bool(dynamic_domains)
            and os.getenv("MULTISEARCH_ENABLE_DYNAMIC_FETCH", "false").lower() == "true"
        )
        dynamic_resources = parse_resource_manifest(
            os.getenv("MULTISEARCH_DYNAMIC_FETCH_RESOURCES", ""),
            authorized_resource_hosts=resource_domains,
        )
        if any(urlsplit(page).hostname not in dynamic_domains for page in dynamic_resources):
            raise ValueError("动态资源清单的页面主机须在 MULTISEARCH_DYNAMIC_FETCH_DOMAINS 中")
        return cls(
            database_url=database_url,
            redis_url=os.getenv("REDIS_URL", "redis://redis:6379/0"),
            key_pepper=pepper,
            searxng_url=os.getenv("SEARXNG_BASE_URL", "http://searxng:8080"),
            enable_ddgs=os.getenv("MULTISEARCH_ENABLE_DDGS", "true").lower() == "true",
            enable_images=os.getenv("MULTISEARCH_ENABLE_IMAGE_SEARCH", "true").lower() == "true",
            max_concurrent=max_concurrent,
            vertical_sources=frozenset(vertical_sources),
            openalex_key=secret("MULTISEARCH_OPENALEX_KEY"),
            github_token=secret("MULTISEARCH_GITHUB_TOKEN"),
            crossref_email=secret("MULTISEARCH_CROSSREF_EMAIL"),
            crawl4ai_url=crawl4ai_url,
            crawl4ai_token=crawl4ai_token,
            enable_local_index=os.getenv("MULTISEARCH_ENABLE_LOCAL_INDEX", "false").lower()
            == "true",
            enable_research=os.getenv("MULTISEARCH_ENABLE_DEEP_RESEARCH", "false").lower()
            == "true",
            research_model_backend=os.getenv("MULTISEARCH_RESEARCH_MODEL_BACKEND", "").lower(),
            research_model_base_url=os.getenv("MULTISEARCH_RESEARCH_MODEL_BASE_URL", ""),
            research_model_name=os.getenv("MULTISEARCH_RESEARCH_MODEL_NAME", ""),
            research_model_api_key=secret("MULTISEARCH_RESEARCH_MODEL_API_KEY"),
            research_cost_currency=os.getenv("MULTISEARCH_RESEARCH_COST_CURRENCY", ""),
            research_input_per_million_tokens=os.getenv(
                "MULTISEARCH_RESEARCH_INPUT_PER_MILLION_TOKENS", ""
            ),
            research_output_per_million_tokens=os.getenv(
                "MULTISEARCH_RESEARCH_OUTPUT_PER_MILLION_TOKENS", ""
            ),
            deployment_policy=load_deployment_policy(os.getenv("MULTISEARCH_POLICY_FILE", "")),
            dynamic_fetch_domains=dynamic_domains,
            dynamic_fetch_resource_domains=resource_domains,
            enable_dynamic_fetch=dynamic_enabled,
            renderer_token=secret("MULTISEARCH_RENDERER_TOKEN") if dynamic_enabled else "",
            dynamic_fetch_resources=dynamic_resources,
        )
