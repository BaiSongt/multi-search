"""Export the repository's pinned dependency inventory as CycloneDX 1.6 JSON."""

import json
import re
import tomllib
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
PYTHON_LOCKS = ("backend/uv.lock", "deploy/crawl4ai/uv.lock", "deploy/renderer/uv.lock")
NPM_LOCK = "console/package-lock.json"
IMAGE_FILES = (
    "compose.yml",
    "compose.dynamic.yml",
    "backend/Dockerfile",
    "console/Dockerfile",
    "deploy/crawl4ai/Dockerfile",
    "deploy/postgres/Dockerfile",
    "deploy/renderer/Dockerfile",
)
COMPOSE_IMAGE = re.compile(r"^[ \t]*image:[ \t]*(\S+)", re.MULTILINE)
DOCKER_FROM = re.compile(
    r"^[ \t]*FROM[ \t]+(?:--platform=\S+[ \t]+)?(\S+)(?:[ \t]+AS[ \t]+(\S+))?",
    re.IGNORECASE | re.MULTILINE,
)
IMAGE_REF = re.compile(
    r"(?P<name>(?:[^/\s]+/)*[^:@/\s]+)(?::(?P<tag>[^:@/\s]+))?@sha256:(?P<digest>[a-f0-9]{64})"
)


def python_components() -> list[dict]:
    components = []
    for filename in PYTHON_LOCKS:
        with (ROOT / filename).open("rb") as file:
            lock = tomllib.load(file)
        for package in lock["package"]:
            if not package["source"].get("registry"):
                continue
            name, version = package["name"], package["version"]
            normalized = re.sub(r"[-_.]+", "-", name).lower()
            components.append(
                {
                    "type": "library",
                    "name": name,
                    "version": version,
                    "purl": f"pkg:pypi/{quote(normalized, safe='')}@{quote(version, safe='')}",
                    "properties": [{"name": "multisearch:manifest", "value": filename}],
                }
            )
    return components


def npm_components() -> list[dict]:
    lock = json.loads((ROOT / NPM_LOCK).read_text(encoding="utf-8"))
    components = []
    for path, package in lock["packages"].items():
        if not path.startswith("node_modules/") or not package.get("version"):
            continue
        name = package.get("name", path.rsplit("node_modules/", 1)[-1])
        version = package["version"]
        properties = [{"name": "multisearch:manifest", "value": NPM_LOCK}]
        if isinstance(license_value := package.get("license"), str):
            properties.append({"name": "multisearch:license:declared", "value": license_value})
        components.append(
            {
                "type": "library",
                "name": name,
                "version": version,
                "purl": f"pkg:npm/{quote(name, safe='/')}@{quote(version, safe='')}",
                "properties": properties,
            }
        )
    return components


def image_components() -> list[dict]:
    components = []
    for filename in IMAGE_FILES:
        contents = (ROOT / filename).read_text(encoding="utf-8")
        if filename.endswith("Dockerfile"):
            aliases = {"scratch"}
            images = []
            for image, alias in DOCKER_FROM.findall(contents):
                if image.casefold() not in aliases:
                    images.append(image)
                if alias:
                    aliases.add(alias.casefold())
        else:
            images = [image.strip("\"'") for image in COMPOSE_IMAGE.findall(contents)]
        for image in images:
            match = IMAGE_REF.fullmatch(image)
            if match is None:
                raise ValueError(f"{filename} contains an unpinned image reference: {image}")
            components.append(
                {
                    "type": "container",
                    "name": match["name"],
                    "version": match["tag"] or f"sha256:{match['digest']}",
                    "bom-ref": f"image:{image}",
                    "hashes": [{"alg": "SHA-256", "content": match["digest"]}],
                    "properties": [{"name": "multisearch:manifest", "value": filename}],
                }
            )
    return components


def inventory() -> dict:
    components = {}
    for entry in python_components() + npm_components() + image_components():
        ref = entry["purl"] if "purl" in entry else entry["bom-ref"]
        if ref not in components:
            components[ref] = {**entry, "bom-ref": ref, "properties": []}
        properties = components[ref]["properties"]
        for item in entry["properties"]:
            if item not in properties:
                properties.append(item)

    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "lifecycles": [{"phase": "pre-build"}],
            "component": {
                "type": "application",
                "name": "MultiSearch",
                "version": (ROOT / "VERSION").read_text(encoding="utf-8").strip(),
                "licenses": [{"license": {"id": "Apache-2.0"}}],
            },
            "properties": [
                {
                    "name": "multisearch:inventory:scope",
                    "value": "Lockfile packages and declared OCI image digests; "
                    "excludes installed image layers, build tools and OS packages.",
                }
            ],
        },
        "components": [components[ref] for ref in sorted(components)],
    }


if __name__ == "__main__":
    print(json.dumps(inventory(), ensure_ascii=True, indent=2))
