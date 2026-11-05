"""仅创建缺失的本地 Compose 密钥。"""

import os
import secrets
from pathlib import Path

directory = Path(__file__).resolve().parents[1] / ".secrets"
directory.mkdir(mode=0o700, exist_ok=True)
for name in (
    "postgres_password",
    "api_key_hash_pepper",
    "searxng_secret",
    "crawl4ai_api_token",
    "metrics_token",
):
    try:
        # Compose 以文件挂载密钥，容器内的非 root 用户需要读取权限。
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    except FileExistsError:
        continue
    with os.fdopen(fd, "w", encoding="ascii") as output:
        output.write(secrets.token_hex(32))
