"""检查应用版本、包版本和可选的 Git 发布标签。"""

import argparse
import json
import re
import subprocess
import tomllib
from pathlib import Path

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--tag", action="store_true", help="同时检查当前提交的发布标签")
args = parser.parse_args()

version = (root / "VERSION").read_text(encoding="utf-8").strip()
match = re.fullmatch(
    r"((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))(?:-(alpha|beta|rc)\.(0|[1-9]\d*))?",
    version,
    flags=re.ASCII,
)
if not match:
    raise SystemExit("VERSION 必须是 MAJOR.MINOR.PATCH 或带 alpha/beta/rc 序号的预发布版本")

core, phase, number = match.groups()
pep440_phases = {"alpha": "a", "beta": "b", "rc": "rc"}
python_version = core if phase is None else f"{core}{pep440_phases[phase]}{number}"
with (root / "backend/pyproject.toml").open("rb") as source:
    backend = tomllib.load(source)["project"]["version"]
with (root / "backend/uv.lock").open("rb") as source:
    backend_lock = next(
        package["version"]
        for package in tomllib.load(source)["package"]
        if package["name"] == "multisearch"
    )
console = json.loads((root / "console/package.json").read_text(encoding="utf-8"))
console_lock = json.loads((root / "console/package-lock.json").read_text(encoding="utf-8"))
changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
if backend != python_version or backend_lock != python_version:
    raise SystemExit("Python 包版本和 uv.lock 必须与 VERSION 对齐")
if any(
    value != version
    for value in (
        console["version"],
        console_lock["version"],
        console_lock["packages"][""]["version"],
    )
):
    raise SystemExit("Console 包版本和 package-lock.json 必须与 VERSION 对齐")
if not re.search(rf"^## {re.escape(version)}(?:\s|$)", changelog, re.MULTILINE):
    raise SystemExit("CHANGELOG.md 缺少当前版本")
if args.tag:
    tags = subprocess.check_output(["git", "tag", "--points-at", "HEAD"], cwd=root, text=True)
    if f"v{version}" not in tags.splitlines():
        raise SystemExit(f"当前提交缺少发布标签 v{version}")
print(f"MultiSearch {version}")
