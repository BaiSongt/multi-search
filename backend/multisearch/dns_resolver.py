"""Optional HTTPS DNS for deployments whose system DNS returns proxy Fake-IP."""

import httpx

# Fixed HTTPS endpoints prevent this deployment option becoming a general fetch
# proxy. TLS verification stays enabled; SafeFetcher validates every returned IP.
DOH_ENDPOINTS = frozenset(
    {
        "https://cloudflare-dns.com/dns-query",
        "https://dns.google/resolve",
        "https://dns.alidns.com/resolve",
    }
)


async def resolve_over_https(host: str, endpoint: str) -> list[str]:
    if endpoint not in DOH_ENDPOINTS:
        raise OSError("Unsupported HTTPS DNS endpoint")
    addresses: list[str] = []
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=3) as client:
        for record_type in ("A", "AAAA"):
            try:
                response = await client.get(
                    endpoint,
                    params={"name": host, "type": record_type},
                    headers={"Accept": "application/dns-json"},
                )
                response.raise_for_status()
                if len(response.content) > 65_536:
                    raise OSError("Oversized HTTPS DNS response")
                data = response.json()
                questions = data.get("Question", [])
                if data.get("Status") != 0 or not questions:
                    raise OSError("HTTPS DNS lookup failed")
                if questions[0].get("name", "").rstrip(".").lower() != host.rstrip(".").lower():
                    raise OSError("HTTPS DNS question mismatch")
                for answer in data.get("Answer", []):
                    if answer.get("type") in (1, 28) and isinstance(answer.get("data"), str):
                        addresses.append(answer["data"])
            except (httpx.HTTPError, ValueError, TypeError, AttributeError) as error:
                raise OSError("HTTPS DNS unavailable") from error
    if not addresses:
        raise OSError("HTTPS DNS returned no addresses")
    return list(dict.fromkeys(addresses))
