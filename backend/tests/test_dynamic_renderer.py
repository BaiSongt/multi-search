import asyncio
import gzip
import json
from unittest.mock import patch

import httpx
import pytest

from multisearch.dynamic_renderer import DynamicRenderer, RenderedHTML
from multisearch.render_resources import FetchedResource
from multisearch.safe_fetch import FetchError
from multisearch.settings import Settings

TOKEN = "a" * 32


async def test_renderer_sends_only_html_and_enforced_csp_to_fixed_authenticated_routes():
    seen = []

    def service(request):
        seen.append(str(request.url))
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert request.headers["accept-encoding"] == "identity"
        if request.method == "GET":
            assert request.content == b""
            assert "x-multisearch-source-csp" not in request.headers
            return httpx.Response(200)
        assert request.method == "POST"
        assert request.headers["content-type"] == "text/html; charset=utf-8"
        assert request.headers["x-multisearch-source-csp"] == "script-src 'none'"
        assert request.content == "<p>文本</p>".encode()
        return httpx.Response(200, json={"html": "<p>正文</p>", "version": "chromium.1.renderer.1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(service)) as client:
        renderer = DynamicRenderer(client, TOKEN)
        assert await renderer.ready()
        assert await renderer.render(
            "<p>文本</p>", timeout_s=1, source_csp="script-src 'none'"
        ) == RenderedHTML("<p>正文</p>", "chromium.1.renderer.1")
    assert seen == ["http://renderer/health/ready", "http://renderer/render"]


async def test_renderer_sends_bounded_bundle_and_omits_resource_policy_metadata():
    page = "https://example.org/guide"
    assets = [
        FetchedResource("https://example.org/app.js", "script", "text/javascript", "\ufeffx=1", 9),
        FetchedResource("https://example.org/data", "json", "application/json", '{"title":"正文"}'),
    ]
    seen = []

    def service(request):
        seen.append(request.url.path)
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert request.headers["content-type"] == "application/json"
        assert "x-multisearch-source-csp" not in request.headers
        payload = json.loads(request.content)
        assert payload == {
            "url": page,
            "html": "<p>文本</p>",
            "source_csp": "script-src 'self'",
            "resources": [
                {
                    "url": asset.url,
                    "kind": asset.kind,
                    "content_type": asset.content_type,
                    "body": asset.body,
                }
                for asset in assets
            ],
        }
        return httpx.Response(
            200, json={"html": "<p>正文</p>", "version": "chromium.157.renderer.2"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(service)) as client:
        renderer = DynamicRenderer(client, TOKEN)
        assert await renderer.render_bundle(
            page, "<p>文本</p>", assets, timeout_s=1, source_csp="script-src 'self'"
        ) == RenderedHTML("<p>正文</p>", "chromium.157.renderer.2")
        oversized = FetchedResource(assets[0].url, "script", "text/javascript", "a" * 262_145)
        assert await renderer.render_bundle(page, "html", [oversized], timeout_s=1) is None
        assert (
            await renderer.render_bundle(page, "html", assets, timeout_s=1, source_csp="x\r\ny")
            is None
        )
    assert seen == ["/render-bundle"]


async def test_renderer_connect_uses_fixed_unix_socket_without_proxy_or_retries():
    transport = httpx.MockTransport(lambda _request: httpx.Response(200))
    with patch(
        "multisearch.dynamic_renderer.httpx.AsyncHTTPTransport", return_value=transport
    ) as init:
        async with DynamicRenderer.connect(None) as disabled:
            assert disabled is None
        init.assert_not_called()
        async with DynamicRenderer.connect(TOKEN) as renderer:
            assert renderer is not None and await renderer.ready()
        assert init.call_args.kwargs["uds"] == "/run/multisearch-renderer/render.sock"
        assert init.call_args.kwargs["trust_env"] is False
        assert init.call_args.kwargs["retries"] == 0


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"Location": "http://169.254.169.254"}),
        httpx.Response(200, json={"html": "ok", "version": "source-\nsecret"}),
        httpx.Response(200, json={"html": "x" * (1024 * 1024 + 1), "version": "renderer.1"}),
        httpx.Response(
            200,
            content=b'{"html":"\\ud800","version":"renderer.1"}',
            headers={"Content-Type": "application/json"},
        ),
        httpx.Response(200, text="x", headers={"Content-Type": "text/html"}),
        httpx.Response(
            200, content=b"x" * (6 * 1024 * 1024 + 1), headers={"Content-Type": "application/json"}
        ),
        httpx.Response(
            200,
            content=gzip.compress(b'{"html":"ok","version":"renderer.1"}'),
            headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
        ),
    ],
)
async def test_renderer_rejects_unsafe_or_invalid_response_without_following_redirect(response):
    seen = []

    def service(request):
        seen.append(str(request.url))
        return response

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(service), follow_redirects=True
    ) as client:
        renderer = DynamicRenderer(client, TOKEN)
        assert await renderer.render("<p>html</p>", timeout_s=1) is None
        assert await renderer.render("a" * (1024 * 1024 + 1), timeout_s=1) is None
        for csp in ("a\r\nb", "a" * 4097):
            assert await renderer.render("<p>html</p>", timeout_s=1, source_csp=csp) is None
    assert seen == ["http://renderer/render"]


async def test_renderer_preserves_timeout_and_unready_semantics():
    async def service(request):
        if request.url.path == "/health/ready":
            return httpx.Response(503)
        if request.content == b"slow":
            await asyncio.sleep(5)
        return httpx.Response(504)

    async with httpx.AsyncClient(transport=httpx.MockTransport(service)) as client:
        renderer = DynamicRenderer(client, TOKEN)
        assert not await renderer.ready()
        for html, timeout_s in [("expired", 0), ("remote timeout", 1), ("slow", 0.01)]:
            with pytest.raises(FetchError) as error:
                await renderer.render(html, timeout_s=timeout_s)
            assert error.value.code == "TIME_BUDGET_EXCEEDED" and error.value.status == 504


def test_dynamic_fetch_settings_enable_only_with_an_explicit_domain_and_worker_flag(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://fixture/fixture")
    monkeypatch.delenv("API_KEY_HASH_PEPPER_FILE", raising=False)
    monkeypatch.setenv("API_KEY_HASH_PEPPER", TOKEN)
    monkeypatch.setenv("MULTISEARCH_DYNAMIC_FETCH_DOMAINS", " Doc.example.com,help.example.org ")
    monkeypatch.setenv("MULTISEARCH_ENABLE_DYNAMIC_FETCH", "false")
    monkeypatch.delenv("MULTISEARCH_RENDERER_TOKEN_FILE", raising=False)
    monkeypatch.setenv("MULTISEARCH_RENDERER_TOKEN", TOKEN)
    settings = Settings.from_env()
    assert settings.dynamic_fetch_domains == frozenset({"doc.example.com", "help.example.org"})
    assert not settings.enable_dynamic_fetch and not settings.renderer_token
    monkeypatch.setenv("MULTISEARCH_ENABLE_DYNAMIC_FETCH", "true")
    settings = Settings.from_env()
    assert settings.enable_dynamic_fetch and settings.renderer_token == TOKEN
    assert TOKEN not in repr(settings)
    monkeypatch.setenv("MULTISEARCH_DYNAMIC_FETCH_DOMAINS", "")
    assert not Settings.from_env().enable_dynamic_fetch
    for invalid in (
        "*.example.org",
        "https://example.org",
        "example.org:443",
        "8.8.8.8",
        "foo.local",
        "foo.home.arpa",
        "broken domain.example.org",
        "K.example.org",
        "example.org.",
        "example.org,",
    ):
        monkeypatch.setenv("MULTISEARCH_DYNAMIC_FETCH_DOMAINS", invalid)
        with pytest.raises(ValueError, match="MULTISEARCH_DYNAMIC_FETCH_DOMAINS"):
            Settings.from_env()


def test_dynamic_manifest_is_scoped_to_deployment_domains(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://fixture/fixture")
    monkeypatch.delenv("API_KEY_HASH_PEPPER_FILE", raising=False)
    monkeypatch.setenv("API_KEY_HASH_PEPPER", TOKEN)
    monkeypatch.setenv("MULTISEARCH_DYNAMIC_FETCH_DOMAINS", "docs.example.org")
    monkeypatch.setenv("MULTISEARCH_ENABLE_DYNAMIC_FETCH", "false")
    page = "https://docs.example.org/guide"
    asset = "https://docs.example.org/app.js"
    monkeypatch.setenv(
        "MULTISEARCH_DYNAMIC_FETCH_RESOURCES",
        json.dumps({page: [{"url": asset, "kind": "script"}]}),
    )
    settings = Settings.from_env()
    assert settings.dynamic_fetch_resources[page][0].url == asset
    assert asset not in repr(settings)
    monkeypatch.setenv("MULTISEARCH_DYNAMIC_FETCH_DOMAINS", "other.example.org")
    with pytest.raises(ValueError, match="MULTISEARCH_DYNAMIC_FETCH_DOMAINS"):
        Settings.from_env()
