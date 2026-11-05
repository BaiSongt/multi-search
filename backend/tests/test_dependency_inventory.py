import json
import subprocess
import sys
from pathlib import Path


def test_dependency_inventory_includes_multistage_images_and_renderer_lock():
    root = Path(__file__).resolve().parents[2]
    document = json.loads(
        subprocess.check_output([sys.executable, root / "scripts/ops/dependency_inventory.py"])
    )
    assert document["bomFormat"] == "CycloneDX"
    images = [item for item in document["components"] if item["type"] == "container"]
    assert images and all(len(item["hashes"][0]["content"]) == 64 for item in images)
    assert any(
        {"name": "multisearch:manifest", "value": "deploy/renderer/Dockerfile"}
        in item["properties"]
        for item in images
    )
    playwright = next(
        item for item in document["components"] if item.get("purl") == "pkg:pypi/playwright@1.64.1"
    )
    assert {"name": "multisearch:manifest", "value": "deploy/renderer/uv.lock"} in playwright[
        "properties"
    ]
