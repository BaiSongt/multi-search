"""备份 Compose PostgreSQL，或在独立临时数据库中演练恢复。"""

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
COUNTS = """
SELECT jsonb_build_object(
    'schema_revision', (SELECT version_num FROM alembic_version),
    'tenants', (SELECT count(*) FROM tenants),
    'api_keys', (SELECT count(*) FROM api_keys),
    'usage_events', (SELECT count(*) FROM usage_events),
    'jobs', (SELECT count(*) FROM search_jobs),
    'job_results', (SELECT count(*) FROM search_jobs WHERE result IS NOT NULL),
    'evidence_records', (SELECT count(*) FROM evidence_records)
)
"""


def postgres(project: str | None, *arguments: str, **streams):
    compose = ["docker", "compose"]
    if project:
        compose += ["--project-name", project]
    return subprocess.run(
        [*compose, "exec", "-T", "postgres", *arguments],
        cwd=ROOT,
        check=True,
        timeout=600,
        **streams,
    )


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def sidecar(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".sha256")


def exclusive(path: Path, *, binary: bool):
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600
    )
    return os.fdopen(descriptor, "wb" if binary else "w", encoding=None if binary else "ascii")


def backup(project: str | None, archive: Path) -> None:
    archive.parent.mkdir(parents=True, exist_ok=True)
    if sidecar(archive).exists():
        raise FileExistsError("目标 SHA-256 文件已存在")
    with exclusive(archive, binary=True) as stream:
        try:
            postgres(
                project,
                "pg_dump",
                "--username=multisearch",
                "--dbname=multisearch",
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                stdout=stream,
            )
        except (OSError, subprocess.SubprocessError):
            stream.close()
            archive.unlink()
            raise
    checksum = digest(archive)
    with exclusive(sidecar(archive), binary=False) as stream:
        stream.write(checksum + "\n")
    print(
        json.dumps({"archive": str(archive), "bytes": archive.stat().st_size, "sha256": checksum})
    )


def restore_check(project: str | None, archive: Path) -> None:
    expected = sidecar(archive).read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[a-f0-9]{64}", expected) or digest(archive) != expected:
        raise ValueError("归档 SHA-256 校验失败")
    # 恢复目标只由当前进程生成；永远不会写入 multisearch 业务数据库。
    scratch = f"multisearch_restore_{uuid4().hex[:16]}"
    if not re.fullmatch(r"multisearch_restore_[a-f0-9]{16}", scratch):
        raise RuntimeError("临时数据库名不符合约束")
    postgres(
        project,
        "createdb",
        "--username=multisearch",
        "--maintenance-db=postgres",
        "--template=template0",
        scratch,
    )
    try:
        with archive.open("rb") as source:
            postgres(
                project,
                "pg_restore",
                "--username=multisearch",
                f"--dbname={scratch}",
                "--single-transaction",
                "--exit-on-error",
                "--no-owner",
                "--no-privileges",
                stdin=source,
            )
        summary = postgres(
            project,
            "psql",
            "--username=multisearch",
            f"--dbname={scratch}",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-A",
            "-t",
            "-c",
            COUNTS,
            stdout=subprocess.PIPE,
        )
        report = json.loads(summary.stdout)
        if not report.get("schema_revision"):
            raise RuntimeError("恢复库缺少迁移版本")
    finally:
        postgres(project, "dropdb", "--username=multisearch", "--maintenance-db=postgres", scratch)
    print(json.dumps({"restore_verified": True, **report}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("backup", "restore-check"))
    parser.add_argument("archive", type=Path)
    parser.add_argument("--project")
    options = parser.parse_args()
    action = backup if options.operation == "backup" else restore_check
    action(options.project, options.archive.resolve())


if __name__ == "__main__":
    main()
