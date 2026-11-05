from pathlib import Path

import pytest

from multisearch.contracts import Mode, SearchRequest
from multisearch.deployment_policy import DeploymentPolicy, load_deployment_policy
from multisearch.routing_policy import DEFAULT_ROUTING
from multisearch.search import SearchService
from multisearch.settings import Settings

ROOT = Path(__file__).resolve().parents[2]


def test_default_policy_file_preserves_current_routing_and_image_requests():
    policy = load_deployment_policy(str(ROOT / "config.example.yaml"))
    request = SearchRequest(query="example", modalities=["image"])
    assert policy.baseline() == DEFAULT_ROUTING
    assert policy.provider("ddgs") == DeploymentPolicy().provider("ddgs")
    assert policy.provider("wikipedia").requests_per_minute == 10
    assert (
        DeploymentPolicy.model_validate({"providers": {"wikipedia": {"priority": 2}}})
        .provider("wikipedia")
        .requests_per_minute
        == 10
    )
    assert policy.apply_image_options(request) is request


def test_deployment_policy_merges_explicit_image_options_and_separates_caches(tmp_path):
    source = tmp_path / "config.yaml"
    source.write_text(
        "routing:\n  quick_fallback: false\n  cache_ttl_seconds: {web: 90}\n"
        "providers:\n  searxng: {priority: 10}\n"
        "image_options: {min_width: 640, license_policy: verified_only}\n",
        encoding="utf-8",
    )
    policy = load_deployment_policy(str(source))
    request = SearchRequest(
        query="example", modalities=["image"], image_options={"min_width": None}
    )
    effective = policy.apply_image_options(request)
    assert effective.image_options.license_policy == "verified_only"
    assert effective.image_options.min_width is None
    assert request.image_options.license_policy == "any_with_status"
    assert policy.provider("searxng").priority == 10
    assert policy.provider("ddgs").priority == 0

    text = SearchRequest(query="example")
    assert policy.apply_image_options(text) is text
    settings = Settings("postgres://ignored", "redis://ignored", "pepper", "http://ignored")
    search = SearchService(None, [], settings)
    assert search._cache_key(
        text, "tenant", Mode.QUICK, policy.baseline(), {}
    ) != search._cache_key(text, "tenant", Mode.QUICK, DEFAULT_ROUTING, {})


@pytest.mark.parametrize(
    "payload",
    [
        "providers:\n  unknown: {}\n",
        "routing: {quick_fallback: true, quick_fallback: false}\n",
        "routing: &rule {quick_fallback: true}\nimage_options: *rule\n",
        "routing: !!python/object/new:os.system [echo unused]\n",
        "routing:\n  budgets:\n    quick: {max_latency_ms: 8000, max_results: 10}\n",
        "providers: {ddgs: {requests_per_minute: 121}}\n",
        "#" + "x" * 32768,
    ],
)
def test_deployment_policy_rejects_ambiguous_or_out_of_budget_yaml(tmp_path, payload):
    source = tmp_path / "config.yaml"
    source.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError, match="部署策略"):
        load_deployment_policy(str(source))
