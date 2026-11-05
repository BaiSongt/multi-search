import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from multisearch.contracts import ImageAttribution, ImageSearchResult, Source
from multisearch.image_attribution import ImageAttributionResolver

PAGE = "https://commons.wikimedia.org/wiki/File:Common_Kingfisher_Alcedo_atthis.jpg"
ASSET = "https://upload.wikimedia.org/wikipedia/commons/c/cc/Common_Kingfisher_Alcedo_atthis.jpg"
TITLE = "File:Common Kingfisher Alcedo atthis.jpg"
LICENSE = "https://creativecommons.org/licenses/by-sa/2.5/"
ASSET_HASH = "0123456789abcdef0123456789abcdef01234567"


def hit(page=PAGE, asset=ASSET):
    return ImageSearchResult(
        result_id="kingfisher",
        title="Common Kingfisher",
        url=page,
        canonical_url=page,
        source_type=Source.WEB,
        provider_ids=["test"],
        retrieved_at=datetime(2026, 11, 5, tzinfo=UTC),
        warnings=["image_rights_unknown"],
        image=ImageAttribution(source_page_url=page, original_url=asset),
    )


def metadata_value(value, source="commons-desc-page"):
    return {"value": value, "source": source}


def wikimedia_response(*, metadata=None, image_updates=None):
    info = {
        "canonicaltitle": TITLE,
        "url": f"{ASSET}?utm_source=commons.wikimedia.org&utm_campaign=imageinfo&utm_content=original",
        "descriptionurl": PAGE,
        "sha1": ASSET_HASH,
        "extmetadata": metadata
        if metadata is not None
        else {
            "LicenseUrl": metadata_value(LICENSE.rstrip("/")),
            "LicenseShortName": metadata_value("CC BY-SA 2.5"),
            "License": metadata_value("cc-by-sa-2.5", "commons-templates"),
            "Artist": metadata_value(
                '<a href="/wiki/Author">Andreas <b>Trepte</b></a><script>bad</script>'
            ),
            "Attribution": metadata_value("<i>Andreas Trepte</i> &amp; contributors"),
        },
    }
    info.update(image_updates or {})
    return {
        "query": {
            "pages": [{"ns": 6, "title": TITLE, "lastrevid": 1265385258, "imageinfo": [info]}]
        }
    }


async def test_resolves_matching_commons_original_and_machine_readable_license():
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(200, json=wikimedia_response())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(upstream), trust_env=False
    ) as client:
        resolver = ImageAttributionResolver(client, user_agent="MultiSearchBot/0.1 (local test)")
        assert resolver.supports(hit())
        response = await resolver.resolve(hit(), 2)
    [request] = calls
    assert request.url.copy_with(query=None) == "https://commons.wikimedia.org/w/api.php"
    assert request.url.params["titles"] == TITLE
    assert request.url.params["prop"] == "info|imageinfo"
    assert request.headers["user-agent"] == "MultiSearchBot/0.1 (local test)"
    assert request.headers["accept-encoding"] == "identity"
    assert response.image.source_page_verified
    assert response.image.source_page_status == "checked"
    assert response.image.source_checked_at == response.image.rights_checked_at
    assert response.image.asset_hash == ASSET_HASH
    assert response.image.asset_hash_algorithm == "sha1"
    assert response.image.asset_hash_source == "wikimedia_commons"
    assert response.image.rights_status == "verified_license"
    assert response.image.license_name == "CC BY-SA 2.5"
    assert response.image.license_url == LICENSE
    assert (
        response.image.rights_evidence_url
        == "https://commons.wikimedia.org/w/index.php?oldid=1265385258"
    )
    assert response.image.rights_checked_at.utcoffset().total_seconds() == 0
    assert response.image.creator == "Andreas Trepte"
    assert response.image.copyright_holder is None
    assert response.image.attribution_text == "Andreas Trepte & contributors"
    assert "image_rights_unknown" not in response.warnings


@pytest.mark.parametrize(
    ("page", "asset"),
    [
        (None, ASSET),
        ("https://example.org/wiki/File:Kingfisher.jpg", ASSET),
        ("https://commons.wikimedia.org@127.0.0.1/wiki/File:Kingfisher.jpg", ASSET),
        ("https://commons.wikimedia.org.evil.example/wiki/File:Kingfisher.jpg", ASSET),
        (PAGE.replace("https://", "http://"), ASSET),
        (PAGE + "?oldid=123", ASSET),
        (PAGE + "%7CFile:Other.jpg", ASSET),
        (PAGE, ASSET.replace("/commons/", "/commons/thumb/")),
        (PAGE, ASSET.replace("upload.wikimedia.org", "evil.example")),
        (PAGE, ASSET + "?download=1"),
    ],
)
async def test_unsafe_or_non_original_links_never_trigger_http(page, asset):
    def unexpected(request):
        pytest.fail("拒绝的候选不应触发 HTTP 请求")

    original = hit(page, asset)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(unexpected), trust_env=False
    ) as client:
        resolver = ImageAttributionResolver(client)
        assert not resolver.supports(original)
        assert await resolver.resolve(original, 2) == original


@pytest.mark.parametrize(
    "image_updates",
    [
        {"url": ASSET.replace(".jpg", "-copy.jpg")},
        {"url": ASSET + "?version=other"},
        {"descriptionurl": PAGE.replace(".jpg", "-copy.jpg")},
        {"canonicaltitle": "File:Another file.jpg"},
        {"filehidden": True},
    ],
)
async def test_different_file_never_grants_verification(image_updates):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json=wikimedia_response(image_updates=image_updates)
            )
        ),
        trust_env=False,
    ) as client:
        result = await ImageAttributionResolver(client).resolve(hit(), 2)
    assert not result.image.source_page_verified
    assert result.image.rights_status == "unknown"
    assert "image_source_not_matched" in result.warnings


@pytest.mark.parametrize(
    ("license_uri", "label", "template", "restrictions", "status"),
    [
        (
            "https://creativecommons.org.evil.example/licenses/by/4.0/",
            "CC BY 4.0",
            "cc-by-4.0",
            "",
            "unknown",
        ),
        (LICENSE, "CC BY 4.0", "cc-by-sa-2.5", "", "conflict"),
        (LICENSE, "CC BY-SA 2.5", "cc-by-4.0", "", "conflict"),
        (LICENSE, None, "cc-by-sa-2.5", "", "declared_unverified"),
        (LICENSE, "CC BY-SA 2.5", None, "", "declared_unverified"),
        (LICENSE, "CC BY-SA 2.5", "cc-by-sa-2.5", ["trademark"], "declared_unverified"),
        (
            "http://creativecommons.org/publicdomain/zero/1.0/",
            "CC0",
            "cc-zero",
            "",
            "verified_license",
        ),
    ],
)
async def test_verifies_source_independently_from_rights(
    license_uri, label, template, restrictions, status
):
    metadata = {
        "LicenseUrl": metadata_value(license_uri),
        "LicenseShortName": metadata_value(label),
        "License": metadata_value(template, "commons-templates"),
        "Restrictions": metadata_value(restrictions),
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=wikimedia_response(metadata=metadata))
        ),
        trust_env=False,
    ) as client:
        result = await ImageAttributionResolver(client).resolve(hit(), 2)
    assert result.image.source_page_verified
    assert result.image.rights_status == status
    if status in {"unknown", "conflict"}:
        assert result.image.license_url is None


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data"}),
        httpx.Response(429),
        httpx.Response(200, json={"data": "a" * 65_536}),
        httpx.Response(200, content=b"not json", headers={"Content-Type": "application/json"}),
    ],
)
async def test_rejects_unusable_responses_without_redirect_or_retry(response):
    calls = []

    def upstream(request):
        calls.append(request)
        return response

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(upstream), trust_env=False, follow_redirects=True
    ) as client:
        result = await ImageAttributionResolver(client).resolve(hit(), 2)
    assert len(calls) == 1 and calls[0].url.host == "commons.wikimedia.org"
    assert not result.image.source_page_verified
    assert "image_source_verification_failed" in result.warnings


async def test_verifier_wait_counts_against_timeout_and_limits_concurrency():
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def upstream(request):
        calls.append(request)
        started.set()
        await release.wait()
        return httpx.Response(200, json=wikimedia_response())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(upstream), trust_env=False
    ) as client:
        resolver = ImageAttributionResolver(client)
        first = asyncio.create_task(resolver.resolve(hit(), 2))
        try:
            await started.wait()
            late = await resolver.resolve(hit(), 0.01)
            assert len(calls) == 1
            assert "image_source_verification_failed" in late.warnings
        finally:
            release.set()
            verified = await first
        assert verified.image.source_page_verified
