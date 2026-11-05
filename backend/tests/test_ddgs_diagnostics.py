import asyncio
import json

import httpx
import pytest
from ddgs.ddgs import DDGS
from ddgs.engines.duckduckgo import Duckduckgo
from ddgs.exceptions import DDGSException
from primp import ConnectError, DNSError
from primp import TimeoutError as RequestTimeout

from multisearch.contracts import Modality, ProviderReason, SearchRequest, Source
from multisearch.ddgs_child import _FixedEndpoint, _search
from multisearch.providers import DDGSProvider, ProviderFailure

_ARGS = {
    "method": "text",
    "backend": "duckduckgo",
    "query": "private fixture",
    "max_results": 3,
    "timeout": 1,
}


@pytest.mark.parametrize(
    ("upstream", "expected"),
    [
        (
            httpx.Response(200, text="<html><body>no matching items</body></html>"),
            {"status": "success", "results": []},
        ),
        (httpx.Response(200, text=" "), {"status": "failed"}),
        (httpx.Response(202, text="unverified challenge"), {"status": "upstream_unavailable"}),
        (
            httpx.Response(302, headers={"location": "https://127.0.0.1"}),
            {"status": "upstream_unavailable"},
        ),
        (httpx.Response(403, text="unverified challenge"), {"status": "upstream_unavailable"}),
        (httpx.Response(408), {"status": "upstream_timeout"}),
        (
            httpx.Response(429, headers={"retry-after": "17"}),
            {"status": "rate_limited", "retry_after": "17"},
        ),
        (
            httpx.Response(429, headers={"retry-after": "x" * 129}),
            {"status": "rate_limited", "retry_after": None},
        ),
        (httpx.Response(503), {"status": "upstream_unavailable"}),
        (httpx.Response(504), {"status": "upstream_timeout"}),
        (RequestTimeout("private fixture"), {"status": "upstream_timeout"}),
        (ConnectError("private fixture"), {"status": "upstream_unavailable"}),
        (DNSError("private fixture"), {"status": "upstream_unavailable"}),
        (TypeError("private fixture"), {"status": "failed"}),
    ],
)
def test_ddgs_child_reports_only_observed_upstream_failures(monkeypatch, upstream, expected):
    class Client:
        def request(self, method, url, **kwargs):
            assert method == "POST" and url == "https://html.duckduckgo.com/html/"
            assert kwargs["follow_redirects"] is False
            if isinstance(upstream, Exception):
                raise upstream
            return upstream

    engine = Duckduckgo(timeout=1)
    engine.http_client.client = Client()
    monkeypatch.setattr(DDGS, "_get_engines", lambda *_: [engine])
    result = _search(dict(_ARGS))
    assert result == expected
    assert "private fixture" not in json.dumps(result)


def test_ddgs_child_does_not_reuse_a_previous_endpoint_status():
    class Client:
        def request(self, *_args, **_kwargs):
            return httpx.Response(429, headers={"retry-after": "5"})

    endpoint = _FixedEndpoint(Client())
    with pytest.raises(DDGSException):
        endpoint.request("GET", "https://www.bing.com/images/async")
    assert endpoint.status == 429
    with pytest.raises(DDGSException):
        endpoint.request("GET", "https://127.0.0.1")
    assert endpoint.status is endpoint.transport_failure is endpoint.retry_after is None


@pytest.mark.parametrize(
    ("payload", "status", "retry_after", "reasons"),
    [
        ({"status": "timeout"}, "timeout", None, ()),
        ({"status": "upstream_timeout"}, "timeout", None, (ProviderReason.UPSTREAM_TIMEOUT,)),
        (
            {"status": "rate_limited", "retry_after": "17"},
            "rate_limited",
            17,
            (ProviderReason.UPSTREAM_RATE_LIMITED,),
        ),
        (
            {"status": "upstream_unavailable"},
            "failed",
            None,
            (ProviderReason.UPSTREAM_UNAVAILABLE,),
        ),
        (
            {"status": "failed", "reason_codes": ["upstream_captcha", "private fixture"]},
            "failed",
            None,
            (),
        ),
    ],
)
async def test_ddgs_parent_only_maps_approved_child_statuses(
    monkeypatch, payload, status, retry_after, reasons
):
    class Child:
        returncode = 0

        async def communicate(self, _args):
            return json.dumps(payload).encode(), b""

    async def spawn(*_args, **_kwargs):
        return Child()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(ProviderFailure) as raised:
        await DDGSProvider().search(
            SearchRequest(query="private fixture"), Source.WEB, Modality.TEXT, 3, 1
        )
    assert (raised.value.status, raised.value.retry_after, raised.value.reason_codes) == (
        status,
        retry_after,
        reasons,
    )
    assert "private fixture" not in str(raised.value)
