import os
from dataclasses import dataclass
from pathlib import Path


def secret(name: str) -> str:
    if file := os.getenv(f"{name}_FILE"):
        return Path(file).read_text(encoding="utf-8").strip()
    return os.getenv(name, "")


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    key_pepper: str
    searxng_url: str
    enable_ddgs: bool = True
    enable_images: bool = True
    max_concurrent: int = 20
    requests_per_minute: int = 60

    @classmethod
    def from_env(cls) -> "Settings":
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            password = secret("POSTGRES_PASSWORD")
            if not password:
                raise ValueError("缺少 PostgreSQL 连接配置")
            from urllib.parse import quote

            database_url = f"postgresql+asyncpg://multisearch:{quote(password, safe='')}@postgres/multisearch"
        pepper = secret("API_KEY_HASH_PEPPER")
        if len(pepper.encode()) < 32:
            raise ValueError("API Key pepper 至少需要 32 字节")
        return cls(
            database_url=database_url,
            redis_url=os.getenv("REDIS_URL", "redis://redis:6379/0"),
            key_pepper=pepper,
            searxng_url=os.getenv("SEARXNG_BASE_URL", "http://searxng:8080"),
            enable_ddgs=os.getenv("MULTISEARCH_ENABLE_DDGS", "true").lower() == "true",
            enable_images=os.getenv("MULTISEARCH_ENABLE_IMAGE_SEARCH", "true").lower() == "true",
        )
