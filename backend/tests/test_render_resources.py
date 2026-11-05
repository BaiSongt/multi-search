import json

import pytest

from multisearch.render_resources import (
    MAX_RESOURCES_PER_PAGE,
    ResourceSpec,
    parse_resource_manifest,
)

PAGE = "https://one.example/docs?lang=zh"
ASSET = "https://one.example/app.js?v=2"


def test_resource_manifest_preserves_exact_page_and_resource_queries():
    manifest = parse_resource_manifest(
        json.dumps(
            {
                PAGE: [
                    {"url": ASSET, "kind": "script"},
                    {"url": "https://one.example/api/data", "kind": "json"},
                ]
            }
        )
    )
    assert manifest == {
        PAGE: (
            ResourceSpec(ASSET, "script"),
            ResourceSpec("https://one.example/api/data", "json"),
        )
    }
    assert parse_resource_manifest("") == {}


@pytest.mark.parametrize(
    "url",
    [
        PAGE,
        "https://sub.one.example/app.js",
        "http://one.example/app.js",
        "https://user:password@one.example/app.js",
        "https://one.example/app.js#",
        "https://one.example:443/app.js",
        "https://ONE.example/app.js",
        "https://one.example/%2e%2e/app.js",
        "https://one.example/%zz",
        "https://one.example",
    ],
)
def test_resource_manifest_rejects_inexact_or_unapproved_urls(url):
    with pytest.raises(ValueError, match="MULTISEARCH_DYNAMIC_FETCH_RESOURCES"):
        parse_resource_manifest(json.dumps({PAGE: [{"url": url, "kind": "script"}]}))


@pytest.mark.parametrize(
    "value",
    [
        f'{{"{PAGE}":[],"{PAGE}":[]}}',
        f'{{"{PAGE}":[{{"url":"{ASSET}","kind":"script","kind":"json"}}]}}',
        json.dumps({PAGE: [{"url": ASSET, "kind": "script"}, {"url": ASSET, "kind": "json"}]}),
        json.dumps({PAGE: [{"url": ASSET, "kind": "image"}]}),
        json.dumps(
            {
                PAGE: [
                    {"url": f"https://one.example/{index}.js", "kind": "script"}
                    for index in range(MAX_RESOURCES_PER_PAGE + 1)
                ]
            }
        ),
        " " * (64 * 1024 + 1),
    ],
)
def test_resource_manifest_rejects_duplicate_or_over_limit_configuration(value):
    with pytest.raises(ValueError, match="MULTISEARCH_DYNAMIC_FETCH_RESOURCES"):
        parse_resource_manifest(value)
