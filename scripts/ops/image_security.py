"""扫描当前 Compose 服务运行的精确镜像，保存 CycloneDX 和漏洞报告。"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
PLATFORM = re.compile(r"[a-z0-9]+/[a-z0-9_]+(?:/[a-z0-9._-]+)?")
SEVERITIES = ("UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL")


def run(command: list[str], cwd: Path, *, capture: bool = False, env=None) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE if capture else None,
        encoding="utf-8",
        timeout=360,
        check=True,
    )
    return result.stdout or ""


def running_images(compose: list[str]) -> dict[tuple[str, str, str], list[str]]:
    expected = set(run([*compose, "config", "--services"], ROOT, capture=True).splitlines())
    containers = run([*compose, "ps", "--status", "running", "-q"], ROOT, capture=True).splitlines()
    if not expected or not containers:
        raise ValueError("所选 Compose 配置没有运行中的服务")
    details = run(
        [
            "docker",
            "inspect",
            "--type",
            "container",
            "--format",
            '{{index .Config.Labels "com.docker.compose.service"}}|{{.Image}}|{{.State.Running}}'
            '|{{json (index . "ImageManifestDescriptor")}}',
            *containers,
        ],
        ROOT,
        capture=True,
    )
    images = defaultdict(set)
    services = set()
    for line in details.splitlines():
        service, identifier, running, descriptor_json = line.split("|", 3)
        if service not in expected:
            continue
        if running != "true" or not IMAGE_ID.fullmatch(identifier):
            raise ValueError(f"无法确认运行镜像：{service}")
        descriptor = json.loads(descriptor_json)
        manifest = ""
        if descriptor:
            manifest = descriptor["digest"]
            selected = descriptor["platform"]
            platform = "/".join(
                filter(None, (selected["os"], selected["architecture"], selected.get("variant")))
            )
            if not IMAGE_ID.fullmatch(manifest):
                raise ValueError(f"无法确认运行镜像清单：{service}")
        else:
            platform = run(
                ["docker", "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", identifier],
                ROOT,
                capture=True,
            ).strip()
        if not PLATFORM.fullmatch(platform):
            raise ValueError(f"无法确认运行镜像平台：{service}")
        images[(identifier, platform, manifest)].add(service)
        services.add(service)
    if missing := expected - services:
        raise ValueError(f"所选配置的服务尚未启动：{', '.join(sorted(missing))}")
    return {target: sorted(names) for target, names in sorted(images.items())}


def image_summary(report: dict, identifier: str, platform: str) -> dict:
    metadata = report["Metadata"]
    if report.get("ArtifactType") != "container_image" or metadata.get("ImageID") != identifier:
        raise ValueError(f"扫描报告未对应预期的本地镜像：{identifier}")
    config = metadata.get("ImageConfig", {})
    if f"{config.get('os')}/{config.get('architecture')}" != "/".join(platform.split("/")[:2]):
        raise ValueError(f"扫描报告的平台与运行镜像不一致：{identifier}")
    packages: Counter[str] = Counter()
    findings = dict.fromkeys(SEVERITIES, 0)
    fixed = dict.fromkeys(SEVERITIES, 0)
    for result in report["Results"]:
        packages[result["Type"]] += len(result.get("Packages") or [])
        for vulnerability in result.get("Vulnerabilities") or []:
            severity = vulnerability.get("Severity", "UNKNOWN")
            if severity not in findings:
                severity = "UNKNOWN"
            findings[severity] += 1
            fixed[severity] += bool(vulnerability.get("FixedVersion"))
    return {
        "os": metadata.get("OS"),
        "packages_by_type": dict(packages),
        "findings_by_severity": findings,
        "findings_with_fix_by_severity": fixed,
    }


def audit(args: argparse.Namespace) -> int:
    compose = ["docker", "compose"]
    if args.project:
        compose.extend(("--project-name", args.project))
    for profile in args.profile:
        compose.extend(("--profile", profile))
    images = running_images(compose)
    scanner = shutil.which(args.trivy)
    if not scanner:
        raise ValueError("找不到 Trivy 可执行文件，请先安装或使用 --trivy 指定路径")

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    cache = (args.cache_dir or output.parent / "trivy-cache").resolve()
    # 使用新建的工作目录和固定参数，防止本地忽略规则或环境变量影响扫描口径。
    env = {
        name: value for name, value in os.environ.items() if not name.upper().startswith("TRIVY_")
    }
    trivy = [str(Path(scanner).resolve()), "--cache-dir", str(cache), "--timeout", "5m"]
    started = datetime.now(UTC).isoformat()
    run(
        [
            *trivy,
            "image",
            "--download-db-only",
            "--no-progress",
            "--disable-telemetry",
            "--skip-version-check",
        ],
        output,
        env=env,
    )
    version = json.loads(
        run([*trivy, "version", "--format", "json"], output, capture=True, env=env)
    )
    if not version.get("VulnerabilityDB", {}).get("UpdatedAt"):
        raise ValueError("Trivy 未报告漏洞数据库更新时间")

    reports = []
    for index, ((identifier, platform, manifest), services) in enumerate(images.items(), start=1):
        print(f"扫描 {', '.join(services)} · {platform} · {identifier}", flush=True)
        key = f"{index:02d}-{identifier.removeprefix('sha256:')}"
        report = f"{key}.trivy.json"
        sbom = f"{key}.cdx.json"
        run(
            [
                *trivy,
                "image",
                "--image-src",
                "docker",
                "--platform",
                platform,
                "--scanners",
                "vuln",
                "--offline-scan",
                "--skip-db-update",
                "--skip-version-check",
                "--disable-telemetry",
                "--no-progress",
                "--list-all-pkgs",
                "--format",
                "json",
                "--output",
                report,
                identifier,
            ],
            output,
            env=env,
        )
        details = image_summary(
            json.loads((output / report).read_text(encoding="utf-8")), identifier, platform
        )
        run(
            [*trivy, "convert", "--format", "cyclonedx", "--output", sbom, report],
            output,
            env=env,
        )
        document = json.loads((output / sbom).read_text(encoding="utf-8"))
        if document.get("bomFormat") != "CycloneDX" or not document.get("components"):
            raise ValueError(f"CycloneDX 报告未包含组件：{identifier}")
        reports.append(
            {
                "image_id": identifier,
                "image_manifest_digest": manifest or None,
                "platform": platform,
                "services": services,
                "vulnerability_report": report,
                "sbom": sbom,
                "sbom_components": len(document["components"]),
                **details,
            }
        )
    if running_images(compose) != images:
        raise ValueError("扫描期间 Compose 服务镜像发生变化，请用新目录重新扫描")
    if (
        json.loads(run([*trivy, "version", "--format", "json"], output, capture=True, env=env))
        != version
    ):
        raise ValueError("扫描期间漏洞数据库发生变化，请用新目录重新扫描")
    high_findings = any(
        item["findings_by_severity"]["HIGH"] or item["findings_by_severity"]["CRITICAL"]
        for item in reports
    )
    summary = {
        "schema_version": 1,
        "started_at": started,
        "completed_at": datetime.now(UTC).isoformat(),
        "compose_project": args.project,
        "compose_profiles": args.profile,
        "scanner": {"name": "Trivy", **version},
        "high_or_critical_findings": high_findings,
        "images": reports,
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"已扫描 {len(images)} 个运行镜像，完整摘要见 {output / 'summary.json'}")
    if args.fail_on_high and high_findings:
        print("扫描发现高危或严重漏洞", file=sys.stderr)
        return 2
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", help="Compose 项目名称")
    parser.add_argument(
        "--profile", action="append", default=[], help="启用的 Compose 配置，可重复"
    )
    parser.add_argument("--output", type=Path, required=True, help="本次扫描使用的新报告目录")
    parser.add_argument(
        "--cache-dir", type=Path, help="Trivy 缓存路径，默认为报告目录旁的 trivy-cache"
    )
    parser.add_argument("--trivy", default="trivy", help="Trivy 可执行文件路径")
    parser.add_argument("--fail-on-high", action="store_true", help="发现高危或严重漏洞时返回 2")
    try:
        return audit(parser.parse_args())
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        print(f"镜像扫描未完成：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
