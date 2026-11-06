import httpx
import pytest

from multisearch import dns_resolver, safe_fetch


@pytest.mark.parametrize("addresses", [["104.18.20.81"], ["104.18.20.81", "10.0.0.1"]])
async def test_https_dns_keeps_private_address_validation(monkeypatch, addresses):
    original_client = httpx.AsyncClient

    async def serve(request):
        assert request.url.host == "cloudflare-dns.com"
        return httpx.Response(
            200,
            json={
                "Status": 0,
                "Question": [{"name": "public.example."}],
                "Answer": [{"type": 1, "data": address} for address in addresses],
            },
        )

    monkeypatch.setattr(
        dns_resolver.httpx,
        "AsyncClient",
        lambda **options: original_client(**options, transport=httpx.MockTransport(serve)),
    )
    result = await dns_resolver.resolve_over_https(
        "public.example", "https://cloudflare-dns.com/dns-query"
    )
    assert result == addresses
    if len(addresses) > 1:
        with pytest.raises(safe_fetch.FetchError):
            [safe_fetch._public_ip(address) for address in result]
    else:
        assert str(safe_fetch._public_ip(result[0])) == addresses[0]


async def test_https_dns_rejects_custom_endpoints_and_question_mismatch(monkeypatch):
    with pytest.raises(OSError):
        await dns_resolver.resolve_over_https("public.example", "http://127.0.0.1/resolve")
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        dns_resolver.httpx,
        "AsyncClient",
        lambda **options: original_client(
            **options,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"Status": 0, "Question": [{"name": "different.example"}]}
                )
            ),
        ),
    )
    with pytest.raises(OSError, match="question mismatch"):
        await dns_resolver.resolve_over_https("public.example", "https://dns.google/resolve")
