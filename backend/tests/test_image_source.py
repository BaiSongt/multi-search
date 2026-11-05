import asyncio
import json
from datetime import UTC, datetime
from time import monotonic
from types import SimpleNamespace

import pytest

from multisearch.contracts import ImageAttribution, ImageSearchResult, SearchRequest, Source
from multisearch.evidence_store import _image_metadata
from multisearch.image_source import PublicImageSourceResolver
from multisearch.safe_fetch import FetchedPage, FetchError
from multisearch.search import SearchService

PAGE = "https://source.example/path/page.html"
ORIGINAL = "https://source.example/path/original.jpg"
WHEN = datetime(2026, 11, 5, tzinfo=UTC)


def hit():
    return ImageSearchResult(
        result_id="img_test",
        title="Test",
        url=PAGE,
        canonical_url=PAGE,
        source_type=Source.WEB,
        provider_ids=["mock"],
        retrieved_at=WHEN,
        image=ImageAttribution(
            original_url=ORIGINAL,
            source_page_url=PAGE,
            rights_status="unknown",
            license_name=None,
        ),
    )


class Fetcher:
    def __init__(self, body="", *, error=None):
        self.body = body
        self.error = error
        self.calls = []

    async def fetch(self, url, **options):
        self.calls.append((url, options))
        if self.error:
            raise self.error
        return FetchedPage(PAGE, PAGE, "text/html", self.body.encode(), WHEN, "utf-8")


@pytest.mark.parametrize(
    "markup",
    [
        '<img src="original.jpg">',
        '<img srcset="other.jpg 1x, original.jpg 2x">',
        '<picture><source srcset="original.jpg 1600w"><img src="thumb.jpg"></picture>',
        '<meta property="og:image" content="https://source.example/path/original.jpg?utm_source=x">',
        '<link rel="image_src" href="https://source.example/path/original.jpg">',
        '<base href="https://different.example/"><img src="https://source.example/path/original.jpg">',
    ],
)
async def test_explicit_matching_source_image_is_checked_without_fetching_original(markup):
    fetcher = Fetcher(markup + '<a href="http://169.254.169.254/">other link</a>')
    result = await PublicImageSourceResolver(fetcher).resolve(
        hit(), timeout_s=1, domain_allowlist=["source.example"]
    )
    assert result.image.source_page_verified
    assert result.image.source_page_status == "checked"
    assert result.image.source_checked_at == WHEN
    assert result.image.rights_status == "unknown"
    assert result.image.license_url is None and result.image.rights_checked_at is None
    assert fetcher.calls == [
        (PAGE, {"timeout_s": 1, "domain_allowlist": ["source.example"], "domain_denylist": ()})
    ]


@pytest.mark.parametrize(
    "markup",
    [
        '<a href="original.jpg">A link</a>',
        '<base href="https://different.example/"><img src="original.jpg">',
        '<img srcset="https://source.example/path/original.jpg,renamed.jpg 1x">',
        '<img src="https://source.example/path/original.jpg?version=other">',
        '<template><img src="original.jpg"></template>',
        '<script type="application/ld+json">{"image":"original.jpg"}</script>',
    ],
)
async def test_nonmatching_markup_reports_checked_without_verifying_source(markup):
    result = await PublicImageSourceResolver(Fetcher(markup)).resolve(hit(), timeout_s=1)
    assert result.image.source_page_status == "checked"
    assert not result.image.source_page_verified
    assert "image_source_not_matched" in result.warnings


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (404, "FETCH_NOT_FOUND", "invalid"),
        (410, "FETCH_NOT_FOUND", "invalid"),
        (403, "FETCH_TARGET_BLOCKED", "unverified"),
        (403, "FETCH_ROBOTS_DENIED", "unverified"),
        (504, "TIME_BUDGET_EXCEEDED", "unverified"),
    ],
)
async def test_only_definitive_missing_source_is_invalid(status, code, expected):
    fetcher = Fetcher(error=FetchError(status, code, "fixture"))
    result = await PublicImageSourceResolver(fetcher).resolve(hit(), timeout_s=1)
    assert result.image.source_page_status == expected
    assert (result.image.source_checked_at is not None) == (expected == "invalid")
    assert result.image.rights_status == "unknown"


@pytest.mark.parametrize(
    ("warnings", "verification"),
    [
        (["image_source_verification_failed", "omitted_detail"], "failed"),
        (["image_source_not_matched"], "not_matched"),
        ([], None),
    ],
)
def test_image_observation_persists_only_source_verification_outcome(warnings, verification):
    result = hit().model_copy(update={"warnings": warnings})
    metadata = json.loads(_image_metadata(result))
    assert metadata.pop("source_verification", None) == verification
    assert metadata == result.image.model_dump(mode="json")


async def test_only_attempted_image_verification_is_recorded_on_outer_timeout():
    class Unavailable:
        async def resolve(self, *args, **kwargs):
            raise TimeoutError

    result = hit()
    request = SearchRequest(query="fixture")
    service = SimpleNamespace(attribution_slots=asyncio.Semaphore(), image_source=Unavailable())
    timed_out, attempts = await SearchService._resolve_image(
        service, result, monotonic() + 1, False, request
    )
    assert attempts == 1
    assert timed_out.image.source_page_status == "unverified"
    assert "image_source_verification_failed" in timed_out.warnings

    service.attribution_slots = asyncio.Semaphore(0)
    not_started, attempts = await SearchService._resolve_image(
        service, result, monotonic(), False, request
    )
    assert attempts == 0 and not_started == result
