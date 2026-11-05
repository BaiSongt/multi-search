"""由本地部署操作员签发或吊销验收密钥。"""

import argparse
import asyncio
import re
from uuid import UUID

from sqlalchemy.ext.asyncio import create_async_engine

from .identity import SEARCH_SCOPES, IdentityStore
from .settings import Settings


async def run(args: argparse.Namespace) -> int:
    config = Settings.from_env()
    db = create_async_engine(config.database_url, hide_parameters=True)
    store = IdentityStore(db, config.key_pepper)
    try:
        if args.action == "issue":
            print(
                await store.issue(args.tenant, args.email, sorted(set(args.scope)), args.ttl_hours)
            )
            return 0
        return 0 if await store.revoke(args.id) else 1
    finally:
        await db.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    issue = commands.add_parser("issue")
    issue.add_argument("--tenant", required=True)
    issue.add_argument("--email", required=True, type=str.lower)
    issue.add_argument("--scope", required=True, choices=SEARCH_SCOPES, action="append")
    issue.add_argument("--ttl-hours", type=int, default=24)
    revoke = commands.add_parser("revoke")
    revoke.add_argument("--id", required=True, type=UUID)
    args = parser.parse_args()
    if args.action == "issue":
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", args.tenant):
            parser.error("--tenant 需要 1–63 位小写字母、数字或连字符")
        if len(args.email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", args.email):
            parser.error("--email 格式无效")
        if not 1 <= args.ttl_hours <= 720:
            parser.error("--ttl-hours 需要在 1–720 之间")
    try:
        return asyncio.run(run(args))
    except ValueError as error:
        parser.exit(2, f"{error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
