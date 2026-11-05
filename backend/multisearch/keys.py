"""由本地部署操作员创建用户、签发或吊销验收密钥。"""

import argparse
import asyncio
import getpass
import re
import sys
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import create_async_engine

from .contracts import Role
from .identity import SEARCH_SCOPES, IdentityStore
from .sessions import EMAIL_PATTERN, TENANT_PATTERN
from .settings import Settings


async def run(args: argparse.Namespace) -> int:
    config = Settings.from_env()
    db = create_async_engine(config.database_url, hide_parameters=True)
    store = IdentityStore(db, config.key_pepper)
    try:
        if args.action == "user-add":
            await store.sessions.provision(args.tenant, args.email, Role(args.role), args.password)
            return 0
        if args.action == "user-disable":
            return 0 if await store.sessions.disable(args.tenant, args.email) else 1
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
    user = commands.add_parser("user-add")
    user.add_argument("--tenant", required=True)
    user.add_argument("--email", required=True, type=str.lower)
    user.add_argument("--role", choices=list(Role), default=Role.DEVELOPER.value)
    password_source = user.add_mutually_exclusive_group()
    password_source.add_argument("--password-file", type=Path)
    password_source.add_argument("--password-stdin", action="store_true")
    disable = commands.add_parser("user-disable")
    disable.add_argument("--tenant", required=True)
    disable.add_argument("--email", required=True, type=str.lower)
    args = parser.parse_args()
    if args.action in {"issue", "user-add", "user-disable"}:
        if not re.fullmatch(TENANT_PATTERN, args.tenant):
            parser.error("--tenant 需要 1–63 位小写字母、数字或连字符")
        if len(args.email) > 254 or not re.fullmatch(EMAIL_PATTERN, args.email):
            parser.error("--email 格式无效")
    if args.action == "issue" and not 1 <= args.ttl_hours <= 720:
        parser.error("--ttl-hours 需要在 1–720 之间")
    try:
        if args.action == "user-add":
            if args.password_file:
                with args.password_file.open(encoding="utf-8") as source:
                    args.password = source.read(1024).rstrip("\r\n")
            elif args.password_stdin:
                args.password = sys.stdin.read(1024).rstrip("\r\n")
            elif sys.stdin.isatty():
                args.password = getpass.getpass("账户密码：")
            else:
                parser.error("非交互模式需要 --password-file 或 --password-stdin")
        return asyncio.run(run(args))
    except ValueError as error:
        parser.exit(2, f"{error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
