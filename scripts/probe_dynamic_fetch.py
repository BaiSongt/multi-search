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
from multisearch.settings import Settings
from multisearch.worker import Worker

SOCKET = "/run/multisearch-renderer/render.sock"
BASE = "http://127.0.0.1:8765"
HOST = "fixture.example.org"
ORIGIN = f"https://{HOST}"
SECTION = "Rendered section"
PARAGRAPH = "Dynamic paragraph assembled by offline JavaScript."
HOSTS = frozenset({HOST, "blocked.fixture.example.org"})


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
    assert request.url.host == "1.1.1.1"
    assert request.headers["host"] == HOST and request.extensions["sni_hostname"] == HOST
    path = request.url.path
    if path == "/robots.txt":
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            text="User-agent: *\nDisallow: /denied\n",
        )
    if path == "/redirect":
        return httpx.Response(302, headers={"location": "https://outside.example.org/"})
    if path == "/static":
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text="<p>Static control</p>"
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
    assert host in HOSTS and port == 443, "授权主机之外不应执行 DNS 查询"
    return ["1.1.1.1" if host == HOST else "198.18.0.1"]


def validate_record(record: EvidenceRecord) -> None:
    assert record.origin_url == record.canonical_url == f"{ORIGIN}/render"
    assert record.extraction_method == "browser_dom" and record.extraction_version.endswith(
        "+normalizer.1"
    )
    assert record.evidence_text == PARAGRAPH and record.verification_status == "content_extracted"
    assert record.locator.kind == "rendered_html_text" and record.locator.section == SECTION
    assert record.locator.text_offset_start == len(SECTION) + 1
    assert record.locator.text_offset_end == len(SECTION) + 1 + len(PARAGRAPH)
    assert record.content_sha256 == sha256(f"{SECTION}\n{PARAGRAPH}".encode()).hexdigest()


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


async def verify_queue(db, pepper: str, builder: EvidenceBuilder) -> None:
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
        "url": f"{ORIGIN}/render",
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
        validate_record(record)
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
                builder = EvidenceBuilder(
                    safe_fetch.SafeFetcher(), renderer=renderer, dynamic_hosts=HOSTS
                )
                await verify_policy(builder)
                await verify_queue(scratch, settings.key_pepper, builder)
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
