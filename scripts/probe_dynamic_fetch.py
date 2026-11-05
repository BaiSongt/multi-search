"""用 Compose 的隔离浏览器和临时 PostgreSQL 验收动态正文，源站使用无外网夹具。"""

import asyncio
import json
import os
import subprocess
from hashlib import sha256
from html import escape
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import httpx2
from fastapi import FastAPI
from lxml import html
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from multisearch import safe_fetch
from multisearch.auth import authenticate_machine
from multisearch.dynamic_renderer import DynamicRenderer
from multisearch.errors import install_errors
from multisearch.evidence import EvidenceBuilder
from multisearch.evidence_api import router as evidence_router
from multisearch.evidence_contracts import EvidenceRecord
from multisearch.identity import IdentityStore
from multisearch.job_api import router as job_router
from multisearch.job_application import JobApplication
from multisearch.job_contracts import FetchRequest, JobKind
from multisearch.job_store import JobStore
from multisearch.mcp_api import MCPJobs, create_mcp, create_mcp_app
from multisearch.metering import SearchPrincipal
from multisearch.observability import install_observability
from multisearch.render_resources import parse_resource_manifest
from multisearch.settings import Settings
from multisearch.worker import Worker

SOCKET = "/run/multisearch-renderer/render.sock"
BASE = "http://127.0.0.1:8765"
HOST = "fixture.example.org"
ORIGIN = f"https://{HOST}"
CDN_HOST = "cdn.assets.example.net"
API_HOST = "data.assets.example.net"
PRIVATE_CDN_HOST = "private.assets.example.net"
CDN_ORIGIN = f"https://{CDN_HOST}"
API_ORIGIN = f"https://{API_HOST}"
RESOURCE_HOSTS = frozenset({CDN_HOST, API_HOST, PRIVATE_CDN_HOST})
PUBLIC_FIXTURE_IPS = {HOST: "1.1.1.1", CDN_HOST: "1.0.0.1", API_HOST: "8.8.8.8"}
SECTION = "Rendered section"
PARAGRAPH = "Dynamic paragraph assembled by offline JavaScript."
BUNDLE_PARAGRAPH = "Approved module and JSON produce this offline paragraph."
CROSS_PARAGRAPH = "Approved cross-origin module and JSON produce this offline paragraph."
HOSTS = frozenset({HOST, "blocked.fixture.example.org"})
BUNDLE_PATHS = ("/bundle", "/bundle-script-csp", "/bundle-connect-csp")
CROSS_PAGE = f"{ORIGIN}/cross-bundle"
CROSS_COEP_PAGE = f"{ORIGIN}/cross-coep"
CROSS_CSP = f"script-src {CDN_ORIGIN}; connect-src {API_ORIGIN}"
CROSS_SCRIPT = f"{CDN_ORIGIN}/cross-module.js"
CROSS_CLASSIC = f"{CDN_ORIGIN}/cross-classic.js"
CROSS_JSON = f"{API_ORIGIN}/cross-data"
AGGREGATE_ASSETS = tuple(f"/aggregate-{index}.js" for index in range(3))
MODULE = """
import { marker } from '/helper.js';
const main = document.querySelector('main');
const root = document.documentElement;
root.dataset.started = 'yes';
root.dataset.origin = location.origin;
const mark = (text) => {
  const node = document.createElement('p');
  node.className = 'unexpected-egress';
  node.textContent = text;
  main.append(node);
};
try {
  const response = await fetch('/api/paragraph', {credentials: 'omit'});
  if (response.ok) {
    const data = await response.json();
    const paragraph = document.createElement('p');
    paragraph.id = 'generated';
    paragraph.textContent = marker + data.paragraph;
    main.append(paragraph);
    if (document.cookie) mark('upstream-set-cookie');
  }
} catch {}
const prohibited = async (target, options, label) => {
  try { if ((await fetch(target, options)).ok) mark(label); } catch {}
};
await Promise.all([
  prohibited('/unapproved', {}, 'unapproved'),
  prohibited('/api/paragraph?other=1', {}, 'other-query'),
  prohibited('/api/paragraph', {method: 'POST', body: '{}'}, 'post'),
  prohibited('/api/paragraph', {headers: {Authorization: 'Bearer fixture'}}, 'authorization'),
  prohibited('/api/paragraph', {headers: {Range: 'bytes=0-1'}}, 'range'),
  prohibited('/helper.js', {}, 'wrong-type-fetch'),
  prohibited('https://outside.example.org/api/paragraph', {}, 'other-origin'),
  new Promise(resolve => {
    const script = document.createElement('script');
    script.src = '/api/paragraph';
    script.onload = () => { mark('wrong-type-script'); resolve(); };
    script.onerror = resolve;
    document.body.append(script);
  }),
]);
try {
  document.cookie = 'probe=private; Path=/; Secure';
  if (document.cookie.includes('probe=')) {
    await prohibited('/api/paragraph', {}, 'cookie');
  }
} catch {}
root.dataset.checked = 'yes';
"""

CROSS_MODULE = """
const endpoint = ENDPOINT;
const root = document.documentElement;
root.dataset.crossModule = location.origin;
try {
  const result = await fetch(endpoint, {credentials: 'omit'});
  if (result.ok) {
    const paragraph = document.createElement('p');
    paragraph.id = 'generated';
    paragraph.textContent = (await result.json()).paragraph;
    document.querySelector('main').append(paragraph);
  }
} catch {}
const prohibited = async (url, options, key) => {
  try { if ((await fetch(url, options)).ok) root.dataset[key] = 'unexpected'; } catch {}
};
await Promise.all([
  prohibited(endpoint, {credentials: 'include'}, 'credentialed'),
  prohibited(endpoint, {headers: {Authorization: 'Bearer fixture'}}, 'authorized'),
  prohibited(endpoint, {headers: {'X-Probe': 'fixture'}}, 'customHeader'),
  prohibited(endpoint, {method: 'POST', body: '{}'}, 'post'),
  prohibited(endpoint, {headers: {Range: 'bytes=0-1'}}, 'ranged'),
  prohibited(endpoint + '?changed=1', {}, 'changedQuery'),
  fetch(endpoint, {mode: 'no-cors'}).then(() => { root.dataset.noCors = 'unexpected'; }, () => {}),
]);
root.dataset.crossChecked = 'yes';
""".replace("ENDPOINT", json.dumps(CROSS_JSON))
CROSS_CLASSIC_JS = """
const crossRoot = document.documentElement;
crossRoot.dataset.crossClassic = 'yes';
if (crossRoot.dataset.classicOnly) {
  document.addEventListener('DOMContentLoaded', () => {
    const paragraph = document.createElement('p');
    paragraph.id = 'generated';
    paragraph.textContent = CLASSIC_PARAGRAPH;
    document.querySelector('main').append(paragraph);
  });
}
""".replace("CLASSIC_PARAGRAPH", json.dumps(CROSS_PARAGRAPH))


def cross_html(*, classic_only: bool = False) -> str:
    document = '<html data-classic-only="yes">' if classic_only else "<html>"
    module = "" if classic_only else f'<script type="module" src="{CROSS_SCRIPT}"></script>'
    return (
        f"<!doctype html>{document}<head><meta charset=utf-8><title>Cross bundle fixture</title>"
        f'<script src="{CROSS_CLASSIC}"></script>'
        f"{module}</head>"
        f"<body><main><h1>{SECTION}</h1></main></body></html>"
    )


def cross_resources() -> list[dict[str, str]]:
    return [
        {
            "url": CROSS_SCRIPT,
            "kind": "script",
            "content_type": "text/javascript",
            "body": CROSS_MODULE,
            "allow_origin": ORIGIN,
        },
        {
            "url": CROSS_CLASSIC,
            "kind": "script",
            "content_type": "text/javascript",
            "body": CROSS_CLASSIC_JS,
        },
        {
            "url": CROSS_JSON,
            "kind": "json",
            "content_type": "application/json",
            "body": json.dumps({"paragraph": CROSS_PARAGRAPH}),
            "allow_origin": "*",
        },
    ]


def bundle_html(*, meta_csp: str = "") -> str:
    policy = (
        f'<meta http-equiv="Content-Security-Policy" content="{escape(meta_csp)}">'
        if meta_csp
        else ""
    )
    return (
        f"<!doctype html><html><head><meta charset=utf-8>{policy}<title>Bundle fixture</title>"
        '<script type="module" src="/app.js"></script></head>'
        f"<body><main><h1>{SECTION}</h1></main></body></html>"
    )


def bundle_resources() -> list[dict[str, str]]:
    return [
        {
            "url": f"{ORIGIN}/app.js",
            "kind": "script",
            "content_type": "text/javascript",
            "body": MODULE,
        },
        {
            "url": f"{ORIGIN}/helper.js",
            "kind": "script",
            "content_type": "application/javascript",
            "body": "export const marker = 'Approved module and JSON ';",
        },
        {
            "url": f"{ORIGIN}/api/paragraph",
            "kind": "json",
            "content_type": "application/json",
            "body": json.dumps({"paragraph": "produce this offline paragraph."}),
        },
    ]


def bundle_manifest():
    entries = [{"url": item["url"], "kind": item["kind"]} for item in bundle_resources()]
    pages = {f"{ORIGIN}{path}": entries for path in BUNDLE_PATHS}
    pages[f"{ORIGIN}/bundle-denied-resource"] = [{"url": f"{ORIGIN}/denied.js", "kind": "script"}]
    pages[f"{ORIGIN}/bundle-aggregate"] = [
        {"url": f"{ORIGIN}{path}", "kind": "script"} for path in AGGREGATE_ASSETS
    ]
    pages[CROSS_PAGE] = [{"url": item["url"], "kind": item["kind"]} for item in cross_resources()]
    pages[CROSS_COEP_PAGE] = [{"url": CROSS_CLASSIC, "kind": "script"}]
    return parse_resource_manifest(json.dumps(pages), authorized_resource_hosts=RESOURCE_HOSTS)


def source_html(*, original="", rendered="", meta_csp=""):
    policy = f'<meta id="original" name="robots" content="{escape(original)}">' if original else ""
    csp = (
        f'<meta http-equiv="Content-Security-Policy" content="{escape(meta_csp)}">'
        if meta_csp
        else ""
    )
    return (
        f"<!doctype html><html><head><meta charset=utf-8><title>Dynamic fixture</title>{policy}{csp}"
        "</head><body><main><h1>Rendered section</h1></main>"
        """
        <script>
        document.addEventListener('DOMContentLoaded', () => {
          const main = document.querySelector('main');
          const paragraph = document.createElement('p');
          paragraph.id = 'generated';
          paragraph.textContent = 'Dynamic paragraph assembled by offline JavaScript.';
          main.append(paragraph);
          document.querySelector('#original')?.remove();
          const mark = (name) => {
            const node = document.createElement('p');
            node.className = 'unexpected-egress';
            node.textContent = name;
            main.append(node);
          };
          for (const target of ['/blocked-subresource', 'http://127.0.0.1:8765/health/live',
                                'https://192.0.2.1/blocked']) {
            fetch(target).then(response => { if (response.ok) mark('fetch'); }).catch(() => {});
          }
          const script = document.createElement('script');
          script.src = '/blocked-script';
          script.onload = () => mark('script');
          document.body.append(script);
          const frame = document.createElement('iframe');
          frame.src = 'file:///etc/passwd';
          frame.onload = () => {
            try { if (frame.contentDocument?.body.textContent.includes('root:')) mark('file'); }
            catch {}
          };
          document.body.append(frame);
          try {
            const connection = new WebSocket('ws://127.0.0.1:8765/blocked');
            connection.onopen = () => mark('websocket');
          } catch {}
          const directive = """
        + json.dumps(rendered)
        + """;
          if (directive) {
            const meta = document.createElement('meta');
            meta.name = 'robots';
            meta.content = directive;
            document.head.append(meta);
          }
        });
        </script></body></html>
        """
    )


def upstream(request: httpx.Request) -> httpx.Response:
    host = request.headers["host"]
    assert (
        request.url.host == PUBLIC_FIXTURE_IPS[host] and request.extensions["sni_hostname"] == host
    )
    assert request.method == "GET" and not request.content
    assert {"authorization", "proxy-authorization", "cookie", "range"}.isdisjoint(request.headers)
    path = request.url.path
    if path == "/robots.txt":
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            text="User-agent: *\nDisallow: /denied\n",
        )
    if host in RESOURCE_HOSTS:
        assert request.headers["origin"] == ORIGIN
        if path == "/cross-redirect.js":
            return httpx.Response(302, headers={"location": "/cross-module.js"})
        if path in {"/missing-cors", "/mismatched-cors"}:
            headers = {"content-type": "application/json"}
            if path == "/mismatched-cors":
                headers["access-control-allow-origin"] = "https://other.example.org"
            return httpx.Response(200, headers=headers, text="{}")
        for item in cross_resources():
            if item["url"] != f"https://{host}{path}":
                continue
            headers = {
                "content-type": item["content_type"],
                "access-control-allow-credentials": "true",
                "set-cookie": "fixture=forbidden; Path=/; Secure",
            }
            if "allow_origin" in item:
                headers["access-control-allow-origin"] = item["allow_origin"]
            return httpx.Response(200, headers=headers, text=item["body"])
        raise AssertionError(f"未预期的跨源预取 {host}{path}")
    assert host == HOST and "origin" not in request.headers
    if path == "/redirect":
        return httpx.Response(302, headers={"location": "https://outside.example.org/"})
    if path == "/static":
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text="<p>Static control</p>"
        )
    if path in {"/cross-bundle", "/cross-coep"}:
        classic_only = path == "/cross-coep"
        headers = {"content-type": "text/html", "content-security-policy": CROSS_CSP}
        if classic_only:
            headers["cross-origin-embedder-policy"] = "require-corp"
        return httpx.Response(200, headers=headers, text=cross_html(classic_only=classic_only))
    if path in BUNDLE_PATHS:
        policies = {
            "/bundle": "script-src 'self'; connect-src 'self'",
            "/bundle-script-csp": "script-src 'none'; connect-src 'self'",
            "/bundle-connect-csp": "script-src 'self'; connect-src 'none'",
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/html", "content-security-policy": policies[path]},
            text=bundle_html(),
        )
    if path in {"/bundle-denied-resource", "/bundle-aggregate"}:
        return httpx.Response(200, headers={"content-type": "text/html"}, text=bundle_html())
    for resource in bundle_resources():
        if resource["url"] == f"{ORIGIN}{path}":
            return httpx.Response(
                200,
                headers={
                    "content-type": resource["content_type"],
                    "set-cookie": "fixture=forbidden; Path=/; Secure",
                },
                text=resource["body"],
            )
    if path == "/wrong.js":
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<p>html</p>")
    if path == "/large.js":
        return httpx.Response(
            200, headers={"content-type": "text/javascript"}, content=b"x" * (256 * 1024 + 1)
        )
    if path in AGGREGATE_ASSETS:
        return httpx.Response(
            200, headers={"content-type": "text/javascript"}, content=b"x" * (180 * 1024)
        )
    options = {
        "/render": ({}, {}),
        "/blocked-meta": ({"original": "nosnippet"}, {}),
        "/blocked-rendered": ({"rendered": "noindex"}, {}),
        "/blocked-header": ({}, {"x-robots-tag": "noarchive"}),
        "/snippet": (
            {"original": "max-snippet:7", "rendered": "max-snippet:9"},
            {"x-robots-tag": "max-snippet:12"},
        ),
        "/header-csp": ({}, {"content-security-policy": "script-src 'none'"}),
        "/meta-csp": ({"meta_csp": "script-src 'none'"}, {}),
    }
    arguments, headers = options[path]
    return httpx.Response(
        200,
        headers={"content-type": "text/html; charset=utf-8", **headers},
        text=source_html(**arguments),
    )


async def resolve_fixture(host: str, port: int) -> list[str]:
    assert host in HOSTS | RESOURCE_HOSTS and port == 443, "授权主机之外不应执行 DNS 查询"
    return [PUBLIC_FIXTURE_IPS.get(host, "198.18.0.1")]


def validate_record(record: EvidenceRecord, *, bundle: bool = False, cross: bool = False) -> None:
    url = CROSS_PAGE if cross else f"{ORIGIN}/bundle" if bundle else f"{ORIGIN}/render"
    paragraph = CROSS_PARAGRAPH if cross else BUNDLE_PARAGRAPH if bundle else PARAGRAPH
    assert record.origin_url == record.canonical_url == url
    version = 3 if cross else 2 if bundle else 1
    suffix = f".renderer.{version}+normalizer.1"
    assert record.extraction_method == "browser_dom" and record.extraction_version.endswith(suffix)
    assert record.evidence_text == paragraph and record.verification_status == "content_extracted"
    assert record.locator.kind == "rendered_html_text" and record.locator.section == SECTION
    assert record.locator.text_offset_start == len(SECTION) + 1
    assert record.locator.text_offset_end == len(SECTION) + 1 + len(paragraph)
    assert record.content_sha256 == sha256(f"{SECTION}\n{paragraph}".encode()).hexdigest()
    digest = record.render_bundle_sha256
    if bundle or cross:
        assert digest is not None and len(digest) == 64 and len(bytes.fromhex(digest)) == 32
    else:
        assert digest is None


async def verify_renderer(token: str, renderer: DynamicRenderer) -> None:
    headers = {"Content-Type": "text/html; charset=utf-8"}
    async with httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=SOCKET),
        base_url="http://renderer",
        trust_env=False,
        timeout=12,
    ) as http:
        denied = await http.post("/render", headers=headers, content=b"<p>test</p>")
        assert denied.status_code == 401 and denied.json()["code"] == "AUTH_REQUIRED"
        headers["Authorization"] = f"Bearer {token}"
        ready = await http.get("/health/ready", headers=headers)
        assert ready.status_code == 200 and ready.json()["status"] == "ready"
        over_limit = await http.post("/render", headers=headers, content=b"x" * (1024 * 1024 + 1))
        assert over_limit.status_code == 413 and over_limit.json()["code"] == "BODY_TOO_LARGE"
        timed_out = await http.post(
            "/render", headers=headers, content=b"<script>while(true){}</script>"
        )
        assert timed_out.status_code == 504 and timed_out.json()["code"] == "RENDER_TIMEOUT"
    rendered = await renderer.render(source_html(), timeout_s=12)
    assert rendered is not None, "隔离浏览器没有返回 DOM"
    tree = html.fromstring(rendered.html)
    assert tree.xpath('string(//*[@id="generated"])') == PARAGRAPH
    assert not tree.xpath('//*[@class="unexpected-egress"]'), "浏览器执行了预期外的子请求"
    print(
        f"隔离浏览器 {rendered.version}：鉴权、1 MiB 上限、脚本超时恢复、内联脚本与子请求隔离通过"
    )


async def verify_bundle_renderer(token: str) -> None:
    resources = bundle_resources()
    bundle = {
        "url": f"{ORIGIN}/bundle",
        "html": bundle_html(),
        "source_csp": "script-src 'self'; connect-src 'self'",
        "resources": resources,
    }
    async with httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=SOCKET),
        base_url="http://renderer",
        trust_env=False,
        timeout=12,
    ) as http:
        denied = await http.post("/render-bundle", json=bundle)
        assert denied.status_code == 401 and denied.json()["code"] == "AUTH_REQUIRED"
        http.headers["Authorization"] = f"Bearer {token}"

        invalid_resources = (
            (422, [{**resources[2], "url": "https://outside.example.org/data"}]),
            (422, [resources[0], resources[0]]),
            (422, [{**resources[0], "content_type": "text/html"}]),
            (413, [{**resources[0], "body": "x" * (256 * 1024 + 1)}]),
            (
                413,
                [
                    {**resources[0], "url": f"{ORIGIN}/asset{index}.js", "body": "x" * (180 * 1024)}
                    for index in range(3)
                ],
            ),
            (413, [{**resources[0], "url": f"{ORIGIN}/asset{index}.js"} for index in range(9)]),
        )
        for expected, items in invalid_resources:
            response = await http.post("/render-bundle", json={**bundle, "resources": items})
            assert response.status_code == expected, (expected, response.status_code, response.text)

        async def render(changes):
            response = await http.post("/render-bundle", json={**bundle, **changes})
            assert response.status_code == 200, response.text
            rendered = response.json()
            assert rendered["version"].endswith(".renderer.2")
            return html.fromstring(rendered["html"])

        tree = await render({})
        assert tree.attrib.get("data-started") == tree.attrib.get("data-checked") == "yes"
        assert tree.attrib.get("data-origin") == ORIGIN
        assert tree.xpath('string(//*[@id="generated"])') == BUNDLE_PARAGRAPH
        assert not tree.xpath('//*[@class="unexpected-egress"]'), tree.xpath(
            '//*[@class="unexpected-egress"]/text()'
        )

        for changes in (
            {"source_csp": "script-src 'none'; connect-src 'self'"},
            {"html": bundle_html(meta_csp="script-src 'none'")},
        ):
            denied_script = await render(changes)
            assert "data-started" not in denied_script.attrib
            assert not denied_script.xpath('//*[@id="generated"]')
        denied_json = await render({"source_csp": "script-src 'self'; connect-src 'none'"})
        assert denied_json.attrib.get("data-started") == "yes"
        assert not denied_json.xpath('//*[@id="generated"]')

    print("同源回放通过：模块和 JSON 生成正文，保留原始 CSP 并拦截未授权资源、方法和凭据")


async def verify_cross_renderer(token: str) -> None:
    resources = cross_resources()
    bundle = {
        "url": CROSS_PAGE,
        "html": cross_html(),
        "source_csp": CROSS_CSP,
        "resources": resources,
    }
    async with httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=SOCKET),
        base_url="http://renderer",
        trust_env=False,
        timeout=12,
        headers={"Authorization": f"Bearer {token}"},
    ) as http:
        for policy in (None, "https://outside.example.org"):
            invalid_json = [*resources[:2], {**resources[2], "allow_origin": policy}]
            response = await http.post("/render-bundle", json={**bundle, "resources": invalid_json})
            assert response.status_code == 422, response.text

        async def render(**changes):
            response = await http.post("/render-bundle", json={**bundle, **changes})
            assert response.status_code == 200, response.text
            rendered = response.json()
            assert rendered["version"].endswith(".renderer.3")
            return html.fromstring(rendered["html"])

        for policy in ("*", ORIGIN):
            tree = await render(
                resources=[*resources[:2], {**resources[2], "allow_origin": policy}]
            )
            assert tree.attrib.get("data-cross-module") == ORIGIN
            assert (
                tree.attrib.get("data-cross-classic")
                == tree.attrib.get("data-cross-checked")
                == "yes"
            )
            assert tree.xpath('string(//*[@id="generated"])') == CROSS_PARAGRAPH
            forbidden = {
                "data-credentialed",
                "data-authorized",
                "data-custom-header",
                "data-post",
                "data-ranged",
                "data-changed-query",
                "data-no-cors",
            }
            assert forbidden.isdisjoint(tree.attrib)

        script_without_cors = {**resources[0], "allow_origin": None}
        tree = await render(resources=[script_without_cors, *resources[1:]])
        assert tree.attrib.get("data-cross-classic") == "yes"
        assert "data-cross-module" not in tree.attrib
        assert not tree.xpath('//*[@id="generated"]')

        tree = await render(source_csp=f"script-src {CDN_ORIGIN}; connect-src 'none'")
        assert tree.attrib.get("data-cross-module") == ORIGIN
        assert not tree.xpath('//*[@id="generated"]')
        tree = await render(source_csp=f"script-src 'self'; connect-src {API_ORIGIN}")
        assert {"data-cross-module", "data-cross-classic"}.isdisjoint(tree.attrib)

        tree = await render(
            resources=[
                resources[0],
                {**resources[1], "resource_policy": "same-origin"},
                resources[2],
            ]
        )
        assert "data-cross-classic" not in tree.attrib
        assert tree.xpath('string(//*[@id="generated"])') == CROSS_PARAGRAPH

        sibling_origin = f"https://cdn.{HOST}"
        sibling_classic = {
            **resources[1],
            "url": CROSS_CLASSIC.replace(CDN_ORIGIN, sibling_origin),
            "resource_policy": "same-site",
        }
        tree = await render(
            html=cross_html(classic_only=True).replace(CDN_ORIGIN, sibling_origin),
            source_csp=f"script-src {sibling_origin}",
            resources=[sibling_classic],
        )
        assert "data-cross-classic" not in tree.attrib

        tree = await render(source_coep="require-corp")
        assert "data-cross-classic" not in tree.attrib
        assert tree.xpath('string(//*[@id="generated"])') == CROSS_PARAGRAPH
        tree = await render(
            source_coep="require-corp",
            resources=[
                resources[0],
                {**resources[1], "resource_policy": "cross-origin"},
                resources[2],
            ],
        )
        assert tree.attrib.get("data-cross-classic") == "yes"
        tree = await render(
            html=cross_html(classic_only=True),
            source_coep="credentialless",
            resources=[resources[1]],
        )
        assert tree.xpath('string(//*[@id="generated"])') == CROSS_PARAGRAPH

    print("跨源回放通过：经典脚本与模块、JSON 的 CORS，以及 CSP、CORP、COEP 和凭据限制")


async def verify_asset_fetcher(fetcher: safe_fetch.SafeFetcher) -> None:
    page_url = f"{ORIGIN}/bundle"

    async def fetch(path):
        resources = parse_resource_manifest(
            json.dumps({page_url: [{"url": f"{ORIGIN}{path}", "kind": "script"}]})
        )
        return await fetcher.fetch_resource(
            resources[page_url][0], page_url=page_url, timeout_s=5, authorized_hosts=HOSTS
        )

    async def denied(path, expected):
        try:
            await fetch(path)
        except safe_fetch.FetchError as error:
            assert error.code == expected, (path, error.code, expected)
        else:
            raise AssertionError(f"预期受限资源返回了内容：{path}")

    first = await fetch("/app.js")
    assert first.url == f"{ORIGIN}/app.js" and first.content_type == "text/javascript"
    assert first.body == MODULE
    for path, code in (
        ("/denied.js", "FETCH_ROBOTS_DENIED"),
        ("/redirect", "FETCH_RESOURCE_REDIRECT"),
        ("/wrong.js", "FETCH_CONTENT_UNSUPPORTED"),
        ("/large.js", "FETCH_BODY_TOO_LARGE"),
    ):
        await denied(path, code)

    async def private_dns(host: str, port: int) -> list[str]:
        assert host == HOST and port == 443
        return ["127.0.0.1"]

    with patch.object(safe_fetch, "_resolve", private_dns):
        await denied("/app.js", "FETCH_TARGET_BLOCKED")
    print("资源预取通过 DNS 固定、robots、重定向、MIME 和单资源体积校验")


async def verify_cross_fetcher(fetcher: safe_fetch.SafeFetcher) -> None:
    script = safe_fetch.ResourceSpec(CROSS_SCRIPT, "script")
    with_cors = await fetcher.fetch_resource(
        script,
        page_url=CROSS_PAGE,
        timeout_s=5,
        authorized_hosts=HOSTS,
        authorized_resource_hosts=RESOURCE_HOSTS,
    )
    assert with_cors.allow_origin == ORIGIN and with_cors.body == CROSS_MODULE
    json_asset = await fetcher.fetch_resource(
        safe_fetch.ResourceSpec(CROSS_JSON, "json"),
        page_url=CROSS_PAGE,
        timeout_s=5,
        authorized_hosts=HOSTS,
        authorized_resource_hosts=RESOURCE_HOSTS,
    )
    assert (
        json_asset.allow_origin == "*"
        and json.loads(json_asset.body)["paragraph"] == CROSS_PARAGRAPH
    )

    async def denied(spec, code, **options):
        authorization = {"authorized_hosts": HOSTS, "authorized_resource_hosts": RESOURCE_HOSTS}
        with_options = {**authorization, **options}
        try:
            await fetcher.fetch_resource(spec, page_url=CROSS_PAGE, timeout_s=5, **with_options)
        except safe_fetch.FetchError as error:
            assert error.code == code, (spec.url, error.code, code)
        else:
            raise AssertionError(f"预期受限的跨源资源返回了内容：{spec.url}")

    await denied(script, "FETCH_TARGET_BLOCKED", authorized_resource_hosts=frozenset())
    await denied(script, "FETCH_TARGET_BLOCKED", authorized_hosts=RESOURCE_HOSTS)
    await denied(script, "FETCH_TARGET_BLOCKED", domain_allowlist=[HOST])
    await denied(script, "FETCH_TARGET_BLOCKED", domain_denylist=[CDN_HOST])
    for url, kind, expected in (
        (f"https://{PRIVATE_CDN_HOST}/cross.js", "script", "FETCH_TARGET_BLOCKED"),
        (f"{CDN_ORIGIN}/denied.js", "script", "FETCH_ROBOTS_DENIED"),
        (f"{CDN_ORIGIN}/cross-redirect.js", "script", "FETCH_RESOURCE_REDIRECT"),
        (f"{API_ORIGIN}/missing-cors", "json", "FETCH_RESOURCE_CORS_DENIED"),
        (f"{API_ORIGIN}/mismatched-cors", "json", "FETCH_RESOURCE_CORS_DENIED"),
    ):
        await denied(safe_fetch.ResourceSpec(url, kind), expected)
    print("跨源预取通过独立域名清单、Origin、用户域名过滤、DNS、robots 和 CORS 检查")


async def verify_policy(builder: EvidenceBuilder) -> None:
    record = await builder.extract(f"{ORIGIN}/render", timeout_s=12, extraction_mode="dynamic")
    validate_record(record)
    excerpt = await builder.extract(f"{ORIGIN}/snippet", timeout_s=12, extraction_mode="dynamic")
    assert excerpt.evidence_text == "Dynamic"
    cases = {
        f"{ORIGIN}/denied": "FETCH_ROBOTS_DENIED",
        f"{ORIGIN}/blocked-meta": "FETCH_SITE_RESTRICTED",
        f"{ORIGIN}/blocked-rendered": "FETCH_SITE_RESTRICTED",
        f"{ORIGIN}/blocked-header": "FETCH_SITE_RESTRICTED",
        f"{ORIGIN}/header-csp": "FETCH_NO_EXCERPT",
        f"{ORIGIN}/meta-csp": "FETCH_NO_EXCERPT",
        f"{ORIGIN}/redirect": "FETCH_TARGET_BLOCKED",
        "https://blocked.fixture.example.org/": "FETCH_TARGET_BLOCKED",
    }
    for url, expected in cases.items():
        try:
            await builder.extract(url, timeout_s=12, extraction_mode="dynamic")
        except safe_fetch.FetchError as error:
            assert error.code == expected, (url, error.code, expected)
        else:
            raise AssertionError(f"预期受限内容返回了证据：{url}")
    print("动态证据的哈希和字符定位通过，且保留 robots、摘录限制、CSP、逐跳主机和 IP 策略")


async def verify_bundle_policy(builder: EvidenceBuilder) -> EvidenceRecord:
    record = await builder.extract(f"{ORIGIN}/bundle", timeout_s=12, extraction_mode="dynamic")
    validate_record(record, bundle=True)
    expected_inputs = [
        (item["url"], item["kind"], item["content_type"], sha256(item["body"].encode()).hexdigest())
        for item in sorted(bundle_resources(), key=lambda item: item["url"])
    ]
    assert (
        record.render_bundle_sha256
        == sha256(json.dumps(expected_inputs, separators=(",", ":")).encode()).hexdigest()
    )
    for path, expected in (
        ("/bundle-script-csp", "FETCH_NO_EXCERPT"),
        ("/bundle-connect-csp", "FETCH_NO_EXCERPT"),
        ("/bundle-denied-resource", "FETCH_ROBOTS_DENIED"),
        ("/bundle-aggregate", "FETCH_BODY_TOO_LARGE"),
    ):
        try:
            await builder.extract(f"{ORIGIN}{path}", timeout_s=12, extraction_mode="dynamic")
        except safe_fetch.FetchError as error:
            assert error.code == expected, (path, error.code, expected)
        else:
            raise AssertionError(f"预期受限的页面返回了正文：{path}")

    def changed(request):
        response = upstream(request)
        if request.url.path != "/helper.js":
            return response
        return httpx.Response(
            200,
            headers={"content-type": "application/javascript"},
            text=f"{response.text}\n// Fixture source revision",
        )

    with patch.object(safe_fetch, "_transport", lambda: httpx.MockTransport(changed)):
        revised = await builder.extract(f"{ORIGIN}/bundle", timeout_s=12, extraction_mode="dynamic")
    validate_record(revised, bundle=True)
    assert revised.content_sha256 == record.content_sha256
    assert revised.render_bundle_sha256 != record.render_bundle_sha256
    assert revised.evidence_id != record.evidence_id

    def limited(request):
        response = upstream(request)
        if request.url.path == "/api/paragraph":
            response.headers["x-robots-tag"] = "max-snippet:8"
        return response

    with patch.object(safe_fetch, "_transport", lambda: httpx.MockTransport(limited)):
        snippet = await builder.extract(f"{ORIGIN}/bundle", timeout_s=12, extraction_mode="dynamic")
    assert snippet.evidence_text == "Approved"
    assert snippet.content_sha256 == record.content_sha256
    assert snippet.render_bundle_sha256 == record.render_bundle_sha256
    print("页面级资源授权、累计体积、资源摘录限制和独立证据摘要通过")
    return record


async def verify_cross_policy(builder: EvidenceBuilder) -> EvidenceRecord:
    record = await builder.extract(CROSS_PAGE, timeout_s=12, extraction_mode="dynamic")
    validate_record(record, cross=True)
    inputs = [
        (
            item["url"],
            item["kind"],
            item["content_type"],
            sha256(item["body"].encode()).hexdigest(),
            item.get("allow_origin"),
            item.get("resource_policy"),
        )
        for item in sorted(cross_resources(), key=lambda item: item["url"])
    ]
    payload = {"resources": inputs, "source_coep": None}
    assert (
        record.render_bundle_sha256
        == sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()
    )

    def exact_cors(request):
        response = upstream(request)
        if request.url.path == "/cross-data":
            response.headers["access-control-allow-origin"] = ORIGIN
        return response

    with patch.object(safe_fetch, "_transport", lambda: httpx.MockTransport(exact_cors)):
        revised = await builder.extract(CROSS_PAGE, timeout_s=12, extraction_mode="dynamic")
    validate_record(revised, cross=True)
    assert revised.content_sha256 == record.content_sha256
    assert revised.render_bundle_sha256 != record.render_bundle_sha256
    assert revised.evidence_id != record.evidence_id

    try:
        await builder.extract(CROSS_COEP_PAGE, timeout_s=12, extraction_mode="dynamic")
    except safe_fetch.FetchError as error:
        assert error.code == "FETCH_NO_EXCERPT", error.code
    else:
        raise AssertionError("原站 COEP 应阻止缺少 CORP 的跨源经典脚本")

    def corp_allowed(request):
        response = upstream(request)
        if request.url.path == "/cross-classic.js":
            response.headers["cross-origin-resource-policy"] = "cross-origin"
        return response

    with patch.object(safe_fetch, "_transport", lambda: httpx.MockTransport(corp_allowed)):
        allowed = await builder.extract(CROSS_COEP_PAGE, timeout_s=12, extraction_mode="dynamic")
    assert allowed.evidence_text == CROSS_PARAGRAPH
    assert allowed.extraction_version.endswith(".renderer.3+normalizer.1")
    print("跨源证据保留原站 COEP 和资源 CORP，CORS 改变后更新资源包指纹及证据 ID")
    return record


async def legacy_static_job(jobs, identity, raw_key):
    actor = await identity.authenticate(raw_key)
    body = FetchRequest(url=f"{ORIGIN}/static", idempotency_key="legacy-static-probe")
    principal = SearchPrincipal(actor.tenant_id, actor.owner_id, actor.key_id, "key", actor.limits)
    previous = await jobs.create(
        principal,
        JobKind.FETCH,
        body.model_dump(mode="json", exclude={"request_id", "idempotency_key", "extraction_mode"}),
        "legacy-static-request",
        body.idempotency_key,
        body.max_latency_ms,
        1,
    )
    return previous.job_id, body.model_dump(mode="json", exclude={"request_id"})


class Readiness:
    def __init__(self):
        self.dynamic = False

    async def exists(self, key):
        assert key in {"multisearch:worker:fetch", "multisearch:worker:fetch_dynamic"}
        return int(key == "multisearch:worker:fetch" or self.dynamic)


async def verify_queue(db, pepper: str, builder: EvidenceBuilder, baseline: EvidenceRecord) -> None:
    identity = IdentityStore(db, pepper)
    key, other = [
        await identity.issue(slug, f"{slug}@example.invalid", ["fetch"], 1)
        for slug in ("dynamic-probe", "dynamic-other")
    ]
    readiness = Readiness()
    jobs = JobStore(db)
    search = SimpleNamespace(settings=SimpleNamespace(dynamic_fetch_domains=frozenset()))
    application = JobApplication(
        jobs,
        readiness,
        search,
        enable_research=False,
    )
    app = FastAPI()
    install_errors(app)
    install_observability(app, token="")
    app.state.db = db
    app.state.identity = identity
    app.state.job_application = application
    app.include_router(job_router)
    app.include_router(evidence_router)

    async def authenticate(raw):
        return await authenticate_machine(identity, raw)

    async def unused(*_):
        raise AssertionError("此探针仅验证抓取任务")

    mcp = create_mcp(
        unused,
        version="0.0.0",
        jobs=MCPJobs(application.fetch, unused, application.get, application.cancel),
    )
    app.router.add_route("/mcp", create_mcp_app(mcp, authenticate), include_in_schema=False)
    headers = {"Authorization": f"Bearer {key}"}
    payload = {
        "url": baseline.origin_url,
        "extraction_mode": "dynamic",
        "idempotency_key": "dynamic-probe",
    }
    async with (
        mcp.session_manager.run(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE, headers=headers
        ) as api,
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers=headers) as mcp_http,
        Client(streamable_http_client(f"{BASE}/mcp", http_client=mcp_http)) as client,
    ):
        disabled = await api.post("/api/v1/fetch", json=payload)
        assert disabled.status_code == 422 and disabled.json()["code"] == "MODE_UNAVAILABLE"
        search.settings.dynamic_fetch_domains = HOSTS
        unauthorized = await api.post(
            "/api/v1/fetch", json={**payload, "url": "https://outside.example.org/"}
        )
        assert (
            unauthorized.status_code == 403
            and unauthorized.json()["code"] == "FETCH_TARGET_BLOCKED"
        )
        pending = await api.post("/api/v1/fetch", json=payload)
        assert pending.status_code == 503, pending.json()
        readiness.dynamic = True
        first = await api.post("/api/v1/fetch", json=payload)
        assert first.status_code == 202, first.json()
        accepted = first.json()
        assert accepted["trace_id"] == first.headers["X-Trace-ID"]
        replay = await client.call_tool("web_fetch", payload)
        assert not replay.is_error and replay.structured_content["job_id"] == accepted["job_id"]
        conflict = await api.post("/api/v1/fetch", json={**payload, "extraction_mode": "static"})
        assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
        assert await jobs.claim((JobKind.FETCH,)) is None, "静态 Worker 领取了动态任务"
        static_id, static_body = await legacy_static_job(jobs, identity, key)
        static_replay = await api.post("/api/v1/fetch", json=static_body)
        assert static_replay.status_code == 202 and static_replay.json()["job_id"] == str(static_id)
        static_claim = await jobs.claim((JobKind.FETCH,))
        assert static_claim is not None and static_claim.job_id == static_id
        await Worker(jobs, builder).execute(static_claim)
        static_result = await api.get(static_replay.json()["status_url"])
        assert static_result.json()["status"] == "completed"
        (static_record,) = static_result.json()["result"]["evidence_records"]
        assert static_record["extraction_method"] == "static_html"
        claim = await jobs.claim((JobKind.FETCH,), allow_dynamic=True)
        assert claim is not None and str(claim.job_id) == accepted["job_id"]
        await Worker(jobs, builder).execute(claim)
        response = await api.get(accepted["status_url"])
        assert response.status_code == 200, response.json()
        state = response.json()
        assert state["status"] == "completed" and state["trace_id"] == accepted["trace_id"], state
        assert state["progress"]["pages_fetched"] == 1
        (raw,) = state["result"]["evidence_records"]
        record = EvidenceRecord.model_validate(raw)
        validate_record(record, cross=baseline.origin_url == CROSS_PAGE, bundle=True)
        assert record.render_bundle_sha256 == baseline.render_bundle_sha256
        assert record.evidence_id == baseline.evidence_id
        evidence_path = f"/api/v1/evidence/{record.evidence_id}"
        evidence = await api.get(evidence_path)
        assert (
            evidence.status_code == 200 and EvidenceRecord.model_validate(evidence.json()) == record
        )
        denied = await api.get(evidence_path, headers={"Authorization": f"Bearer {other}"})
        assert denied.status_code == 404
    async with db.connect() as connection:
        usage = (
            await connection.execute(
                text("""
                    SELECT u.pages_fetched, j.payload
                    FROM usage_events u JOIN search_jobs j ON j.id = u.id
                    WHERE u.id = :id AND u.operation = 'fetch'
                """),
                {"id": UUID(accepted["job_id"])},
            )
        ).one()
        assert usage.pages_fetched == 1 and usage.payload == {}
    print("独立 PostgreSQL 的 REST／MCP 幂等、Worker 能力隔离、证据归档、跨租户拒绝和单次计量通过")


async def main() -> None:
    settings = Settings.from_env()
    token = settings.renderer_token
    assert token, "请在启用动态抓取的 Worker 容器中执行"
    name = f"multisearch_dynamic_probe_{uuid4().hex[:16]}"
    root = create_async_engine(
        settings.database_url, isolation_level="AUTOCOMMIT", hide_parameters=True
    )
    url = make_url(settings.database_url).set(database=name).render_as_string(hide_password=False)
    scratch = create_async_engine(url, hide_parameters=True)
    created = False
    try:
        async with DynamicRenderer.connect(token) as renderer:
            await verify_renderer(token, renderer)
            await verify_bundle_renderer(token)
            await verify_cross_renderer(token)
            async with root.connect() as connection:
                await connection.execute(text(f'CREATE DATABASE "{name}"'))
                created = True
            await asyncio.to_thread(
                subprocess.run,
                ["alembic", "upgrade", "head"],
                check=True,
                timeout=30,
                env={**os.environ, "DATABASE_URL": url},
            )
            with (
                patch.object(safe_fetch, "_resolve", resolve_fixture),
                patch.object(safe_fetch, "_transport", lambda: httpx.MockTransport(upstream)),
            ):
                fetcher = safe_fetch.SafeFetcher()
                builder = EvidenceBuilder(
                    fetcher,
                    renderer=renderer,
                    dynamic_hosts=HOSTS,
                    dynamic_resource_hosts=RESOURCE_HOSTS,
                    dynamic_resources=bundle_manifest(),
                )
                await verify_asset_fetcher(fetcher)
                await verify_cross_fetcher(fetcher)
                await verify_policy(builder)
                await verify_bundle_policy(builder)
                baseline = await verify_cross_policy(builder)
                await verify_queue(scratch, settings.key_pepper, builder, baseline)
    finally:
        try:
            await scratch.dispose()
            if created:
                async with root.connect() as connection:
                    await connection.execute(text(f'DROP DATABASE "{name}"'))
        finally:
            await root.dispose()


if __name__ == "__main__":
    asyncio.run(main())
