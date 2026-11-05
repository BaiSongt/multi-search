import asyncio

import httpx
import pytest

from multisearch import safe_fetch
from multisearch.render_resources import MAX_RESOURCE_BYTES, FetchedResource, ResourceSpec
from multisearch.safe_fetch import FetchError, SafeFetcher

PUBLIC_IP = "8.8.8.8"
SCRIPT = ResourceSpec("https://one.example/app.js", "script")
RESOURCE_OPTIONS = {
    "page_url": "https://one.example/page",
    "timeout_s": 1,
    "authorized_hosts": frozenset({"one.example"}),
}


@pytest.fixture
def network(monkeypatch):
    monkeypatch.setattr(safe_fetch, "_MIN_HOST_DELAY", 0)
    dns, requests, answers, responses = [], [], {}, {}

    async def resolve(host, port):
        dns.append((host, port))
        answer = answers.get(host, [PUBLIC_IP])
        return answer() if callable(answer) else answer

    async def send(request):
        requests.append(request)
        response = responses.get((request.headers["host"], request.url.path))
        if callable(response):
            return await response(request)
        if response is not None:
            return response
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, headers={"Content-Type": "text/plain"}, text="public text")

    monkeypatch.setattr(safe_fetch, "_resolve", resolve)
    monkeypatch.setattr(safe_fetch, "_transport", lambda: httpx.MockTransport(send))
    return dns, requests, answers, responses


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost/",
        "http://127.0.0.1/",
        "http://127.1/",
        "http://2130706433/",
        "http://0x7f000001/",
        "http://169.254.169.254/latest/meta-data",
        "http://168.63.129.16/",
        "http://100.100.100.200/",
        "http://metadata.google.internal/",
        "http://224.0.0.1/",
        "http://[::ffff:127.0.0.1]/",
        "http://[64:ff9b::a9fe:a9fe]/",
        "http://[2002:7f00:1::]/",
        "http://[fe80::1%25en0]/",
        "https://user:pass@example.org/",
        "https://example.org:8443/",
        "https://example.org\\@evil.example/",
    ],
)
async def test_blocked_inputs_never_contact_dns_or_transport(network, url):
    dns, requests, *_ = network
    with pytest.raises(FetchError) as raised:
        await SafeFetcher().fetch(url, timeout_s=1)
    assert raised.value.code == "FETCH_TARGET_BLOCKED"
    assert dns == requests == []


async def test_dns_mixed_records_and_private_scope_fail_closed(network):
    dns, requests, answers, _ = network
    answers["public.example"] = [PUBLIC_IP, "10.0.0.1"]
    with pytest.raises(FetchError) as denied:
        await SafeFetcher().fetch("https://public.example", timeout_s=1)
    assert denied.value.code == "FETCH_TARGET_BLOCKED"
    for scope in ("offline", "private"):
        with pytest.raises(FetchError):
            await SafeFetcher().fetch("https://public.example", timeout_s=1, network_scope=scope)
    assert dns == [("public.example", 443)] and not requests


async def test_pins_ip_host_sni_and_revalidates_dns_on_every_hop(network):
    dns, requests, answers, responses = network
    answers["two.example"] = ["2606:4700:4700::1111"]
    responses[("one.example", "/")] = httpx.Response(
        302, headers={"Location": "https://two.example/public?x=1"}
    )
    result = await SafeFetcher().fetch("https://one.example/#hidden", timeout_s=2)
    assert result.origin_url == "https://one.example/"
    assert result.url == "https://two.example/public?x=1"
    assert dns == [("one.example", 443)] * 2 + [("two.example", 443)] * 2
    assert [item.url.host for item in requests] == [PUBLIC_IP] * 2 + ["2606:4700:4700::1111"] * 2
    assert [item.extensions["sni_hostname"] for item in requests] == ["one.example"] * 2 + [
        "two.example"
    ] * 2
    assert [item.headers["host"] for item in requests] == ["one.example"] * 2 + ["two.example"] * 2
    assert all(
        "cookie" not in item.headers and "authorization" not in item.headers for item in requests
    )
    assert requests[-1].url.raw_path == b"/public?x=1"


async def test_dns_rebinding_between_robots_and_page_is_blocked(network):
    dns, requests, answers, _ = network
    records = iter([[PUBLIC_IP], ["127.0.0.1"]])
    answers["rebind.example"] = lambda: next(records)
    with pytest.raises(FetchError) as error:
        await SafeFetcher().fetch("https://rebind.example", timeout_s=1)
    assert error.value.code == "FETCH_TARGET_BLOCKED"
    assert len(dns) == 2 and [item.url.path for item in requests] == ["/robots.txt"]


@pytest.mark.parametrize(
    ("location", "policy"),
    [
        ("//169.254.169.254/latest", {}),
        ("http://one.example/unsafe", {}),
        ("https://two.example/blocked", {"domain_allowlist": ["one.example"]}),
        ("https://two.example/blocked", {"domain_denylist": ["two.example"]}),
        ("https://sub.one.example/blocked", {"authorized_hosts": frozenset({"one.example"})}),
    ],
)
async def test_rejects_disallowed_redirect_before_dns(network, location, policy):
    dns, requests, _, responses = network
    responses[("one.example", "/")] = httpx.Response(302, headers={"Location": location})
    with pytest.raises(FetchError) as error:
        await SafeFetcher().fetch("https://one.example", timeout_s=1, **policy)
    assert error.value.code == "FETCH_TARGET_BLOCKED"
    assert len(dns) == len(requests) == 2


async def test_dynamic_fetch_requires_exact_https_and_retains_enforced_csp(network):
    dns, requests, _, responses = network
    options = {"timeout_s": 1, "authorized_hosts": frozenset({"one.example"})}
    for blocked in ("http://one.example/", "https://sub.one.example/"):
        with pytest.raises(FetchError, match="公网访问策略"):
            await SafeFetcher().fetch(blocked, **options)
    assert not dns and not requests
    responses[("one.example", "/")] = httpx.Response(
        200,
        headers=[
            ("Content-Type", "text/html"),
            ("Content-Security-Policy", "script-src 'none'"),
            ("Content-Security-Policy", "base-uri 'none'"),
            ("Content-Security-Policy-Report-Only", "default-src 'none'"),
        ],
        text="<p>Text</p>",
    )
    result = await SafeFetcher().fetch("https://one.example/", **options)
    assert result.content_security_policy == ("script-src 'none'", "base-uri 'none'")


async def test_render_resources_use_the_pinned_credential_free_transport_and_preserve_utf8(network):
    dns, requests, _, responses = network
    responses[("one.example", "/app.js")] = httpx.Response(
        200,
        headers={
            "Content-Type": 'Application/JavaScript; charset="utf-8"',
            "Set-Cookie": "ignored=1",
            "X-Robots-Tag": "max-snippet: 17",
        },
        content=b"\xef\xbb\xbfconst value = 'value';",
    )
    responses[("one.example", "/data")] = httpx.Response(
        200, headers={"Content-Type": "application/json"}, content=b'{"value":"value"}'
    )
    fetcher = SafeFetcher()
    script = ResourceSpec("https://one.example/app.js?v=1", "script")
    data = ResourceSpec("https://one.example/data", "json")
    assert await fetcher.fetch_resource(script, **RESOURCE_OPTIONS) == FetchedResource(
        script.url, "script", "application/javascript", "\ufeffconst value = 'value';", 17
    )
    assert await fetcher.fetch_resource(data, **RESOURCE_OPTIONS) == FetchedResource(
        data.url, "json", "application/json", '{"value":"value"}'
    )
    assert [item.url.raw_path for item in requests] == [b"/robots.txt", b"/app.js?v=1", b"/data"]
    assert dns == [("one.example", 443)] * 3
    assert all(
        item.url.host == PUBLIC_IP
        and item.extensions["sni_hostname"] == "one.example"
        and "cookie" not in item.headers
        and "authorization" not in item.headers
        for item in requests
    )


async def test_render_resource_obeys_robots_disallow(network):
    _, requests, _, responses = network
    responses[("one.example", "/robots.txt")] = httpx.Response(
        200, headers={"Content-Type": "text/plain"}, text="User-agent: *\nDisallow: /app.js\n"
    )
    with pytest.raises(FetchError) as error:
        await SafeFetcher().fetch_resource(SCRIPT, **RESOURCE_OPTIONS)
    assert error.value.code == "FETCH_ROBOTS_DENIED"
    assert [item.url.path for item in requests] == ["/robots.txt"]


async def test_render_resource_rejects_private_dns_after_robots(network):
    _, requests, answers, _ = network
    records = iter([[PUBLIC_IP], ["127.0.0.1"]])
    answers["one.example"] = lambda: next(records)
    with pytest.raises(FetchError) as error:
        await SafeFetcher().fetch_resource(SCRIPT, **RESOURCE_OPTIONS)
    assert error.value.code == "FETCH_TARGET_BLOCKED"
    assert [item.url.path for item in requests] == ["/robots.txt"]


async def test_render_resource_rejects_even_same_origin_redirect(network):
    dns, requests, _, responses = network
    responses[("one.example", "/app.js")] = httpx.Response(302, headers={"Location": "/other.js"})
    with pytest.raises(FetchError) as error:
        await SafeFetcher().fetch_resource(SCRIPT, **RESOURCE_OPTIONS)
    assert error.value.code == "FETCH_RESOURCE_REDIRECT"
    assert [item.url.path for item in requests] == ["/robots.txt", "/app.js"]
    assert dns == [("one.example", 443)] * 2


@pytest.mark.parametrize(
    ("script", "options"),
    [
        (SCRIPT, {"authorized_hosts": frozenset()}),
        (SCRIPT, {"domain_denylist": ["one.example"]}),
        (SCRIPT, {"network_scope": "offline"}),
        (
            ResourceSpec("https://two.example/app.js", "script"),
            {"authorized_hosts": frozenset({"one.example", "two.example"})},
        ),
    ],
)
async def test_render_resource_requires_origin_and_scope_authorization_before_dns(
    network, script, options
):
    dns, requests, *_ = network
    with pytest.raises(FetchError) as error:
        await SafeFetcher().fetch_resource(script, **{**RESOURCE_OPTIONS, **options})
    assert error.value.code == "FETCH_TARGET_BLOCKED"
    assert dns == requests == []


@pytest.mark.parametrize(
    ("headers", "content"),
    [
        ({"Content-Type": "text/javascript", "Content-Encoding": "gzip"}, None),
        ({"Content-Type": "text/html"}, b"<p>wrong MIME</p>"),
        ([("Content-Type", "text/javascript; charset=utf-8"), ("Content-Type", "text/html")], b""),
        ({"Content-Type": "text/javascript; charset=windows-1252"}, b"const x = 1;"),
        ({"Content-Type": "text/javascript"}, b"\xff"),
    ],
    ids=["compressed", "mime", "duplicate-mime", "charset", "non-utf8"],
)
async def test_render_resource_rejects_unsupported_content(network, headers, content):
    _, _, _, responses = network
    responses[("one.example", "/app.js")] = httpx.Response(
        200, headers=headers, content=content, stream=Stream(0) if content is None else None
    )
    with pytest.raises(FetchError) as error:
        await SafeFetcher().fetch_resource(SCRIPT, **RESOURCE_OPTIONS)
    assert error.value.code == "FETCH_CONTENT_UNSUPPORTED"


async def test_render_resource_enforces_absolute_and_remaining_byte_caps(network):
    _, _, _, responses = network
    headers = {"Content-Type": "text/javascript"}
    responses[("one.example", "/app.js")] = httpx.Response(
        200, headers=headers, content=b"a" * (MAX_RESOURCE_BYTES + 1)
    )
    fetcher = SafeFetcher()
    with pytest.raises(FetchError) as absolute:
        await fetcher.fetch_resource(
            SCRIPT, **RESOURCE_OPTIONS, max_body_bytes=MAX_RESOURCE_BYTES * 2
        )
    assert absolute.value.code == "FETCH_BODY_TOO_LARGE"

    stream = Stream(0)
    responses[("one.example", "/app.js")] = httpx.Response(200, headers=headers, stream=stream)
    with pytest.raises(FetchError) as remaining:
        await fetcher.fetch_resource(SCRIPT, **RESOURCE_OPTIONS, max_body_bytes=15)
    assert remaining.value.code == "FETCH_BODY_TOO_LARGE" and stream.closed


async def test_redirect_cycle_stops_and_robots_is_cached(network):
    dns, requests, _, responses = network
    responses[("one.example", "/")] = httpx.Response(302, headers={"Location": "/b"})
    responses[("one.example", "/b")] = httpx.Response(302, headers={"Location": "/"})
    with pytest.raises(FetchError) as error:
        await SafeFetcher().fetch("https://one.example", timeout_s=1)
    assert error.value.code == "FETCH_REDIRECT_LIMIT"
    assert [item.url.path for item in requests] == ["/robots.txt", "/", "/b"]
    assert len(dns) == 3


async def test_robots_specific_allow_wildcard_and_crawl_delay(network):
    _, requests, _, responses = network
    responses[("one.example", "/robots.txt")] = httpx.Response(
        200,
        headers={"Content-Type": "text/plain"},
        text="User-agent: MultiSearchBot\nDisallow: /private/*\nAllow: /private/public$\n",
    )
    fetcher = SafeFetcher()
    with pytest.raises(FetchError) as error:
        await fetcher.fetch("https://one.example/private/secret", timeout_s=1)
    assert error.value.code == "FETCH_ROBOTS_DENIED"
    await fetcher.fetch("https://one.example/private/public", timeout_s=1)
    assert [item.url.path for item in requests] == ["/robots.txt", "/private/public"]
    responses[("two.example", "/robots.txt")] = httpx.Response(
        200, headers={"Content-Type": "text/plain"}, text="User-agent: *\nCrawl-delay: 3\n"
    )
    with pytest.raises(FetchError) as exhausted:
        await fetcher.fetch("https://two.example", timeout_s=0.5)
    assert exhausted.value.code == "TIME_BUDGET_EXCEEDED"
    assert requests[-1].url.path == "/robots.txt"


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
async def test_unreachable_robots_fails_closed(network, status):
    _, requests, _, responses = network
    responses[("one.example", "/robots.txt")] = httpx.Response(status)
    with pytest.raises(FetchError) as error:
        await SafeFetcher().fetch("https://one.example", timeout_s=1)
    assert error.value.code == "FETCH_ROBOTS_UNAVAILABLE"
    assert [item.url.path for item in requests] == ["/robots.txt"]


@pytest.mark.parametrize(
    ("headers", "code"),
    [
        ({"Content-Type": "application/pdf"}, "FETCH_CONTENT_UNSUPPORTED"),
        ({"Content-Type": "not-a-media-type"}, "FETCH_CONTENT_UNSUPPORTED"),
        ({"Content-Type": "text/plain", "Content-Encoding": "gzip"}, "FETCH_CONTENT_UNSUPPORTED"),
        ({"Content-Type": "text/html", "X-Robots-Tag": "nosnippet"}, "FETCH_SITE_RESTRICTED"),
        ({"Content-Type": "text/html", "cf-mitigated": "challenge"}, "FETCH_SITE_RESTRICTED"),
        ({"Content-Type": "text/html", "Content-Length": "9001"}, "FETCH_BODY_TOO_LARGE"),
    ],
)
async def test_unsupported_content_and_site_headers(network, headers, code):
    _, _, _, responses = network
    responses[("one.example", "/")] = httpx.Response(200, headers=headers, stream=Stream(0))
    with pytest.raises(FetchError) as error:
        await SafeFetcher(max_body_bytes=512).fetch("https://one.example", timeout_s=1)
    assert error.value.code == code


@pytest.mark.parametrize(
    ("content_type", "max_body_bytes", "content", "expected"),
    [
        ("application/pdf", 512, b"%PDF-1.7\nfixture", None),
        ("text/plain", 512, b"%PDF-1.7\nfixture", "FETCH_CONTENT_UNSUPPORTED"),
        ("application/octet-stream", 512, b"%PDF-1.7\nfixture", "FETCH_CONTENT_UNSUPPORTED"),
        ("application/pdf", 8, b"%PDF-1.7\nfixture", "FETCH_BODY_TOO_LARGE"),
        (
            "application/pdf",
            2 * 1024 * 1024,
            b"%PDF-" + b"x" * (1024 * 1024),
            "FETCH_BODY_TOO_LARGE",
        ),
    ],
)
async def test_pdf_fetch_requires_declared_mime_and_matching_bounded_signature(
    network, content_type, max_body_bytes, content, expected
):
    _, requests, _, responses = network
    responses[("one.example", "/reference.pdf")] = httpx.Response(
        200, headers={"Content-Type": content_type}, content=content
    )
    fetcher = SafeFetcher(max_body_bytes=max_body_bytes)
    if expected is not None:
        with pytest.raises(FetchError) as error:
            await fetcher.fetch("https://one.example/reference.pdf", timeout_s=2)
        assert error.value.code == expected
    else:
        result = await fetcher.fetch("https://one.example/reference.pdf", timeout_s=2)
        assert result.body == content and result.content_type == content_type
    assert [request.url.path for request in requests] == ["/robots.txt", "/reference.pdf"]


class Stream(httpx.AsyncByteStream):
    def __init__(self, delay):
        self.delay = delay
        self.closed = False

    async def __aiter__(self):
        if self.delay:
            await asyncio.sleep(self.delay)
        yield b"a" * 10
        yield b"b" * 10

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize(
    ("delay", "code"), [(0, "FETCH_BODY_TOO_LARGE"), (0.2, "TIME_BUDGET_EXCEEDED")]
)
async def test_stream_body_and_time_limits_close_transport(network, delay, code):
    _, _, _, responses = network
    body = Stream(delay)
    responses[("one.example", "/")] = httpx.Response(
        200, headers={"Content-Type": "text/plain"}, stream=body
    )
    with pytest.raises(FetchError) as error:
        await SafeFetcher(max_body_bytes=15).fetch("https://one.example", timeout_s=0.1)
    assert error.value.code == code and body.closed


async def test_host_concurrency_is_bounded_across_requests(network):
    _, _, _, responses = network
    active, peak = 0, 0

    async def page(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.01)
            return httpx.Response(200, headers={"Content-Type": "text/plain"}, text="public")
        finally:
            active -= 1

    responses.update({("one.example", f"/{index}"): page for index in range(8)})
    fetcher = SafeFetcher(max_parallel=4)
    await asyncio.gather(
        *(fetcher.fetch(f"https://one.example/{index}", timeout_s=1) for index in range(8))
    )
    assert peak == 1
