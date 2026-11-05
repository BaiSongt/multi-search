import re
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit

from lxml import html

from .contracts import ImageSearchResult
from .evidence import parse_static_html
from .pipeline import canonical_url, public_link
from .safe_fetch import FetchError, SafeFetcher, snippet_limit

_IMAGE_META = {
    "og:image",
    "og:image:url",
    "og:image:secure_url",
    "twitter:image",
    "twitter:image:src",
}
_DESCRIPTOR = re.compile(r"(?:[1-9][0-9]*w|(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)x)")
_URL_TOKEN = re.compile(r"[^ \t\r\n\f]+")
_SKIP = re.compile(r"[ \t\r\n\f,]*")
_INERT = {"script", "style", "template", "noscript"}


def _srcset(value: str) -> Iterator[str]:
    # HTML 将 URL 内的逗号视为 URL 字符。仅接受常规宽度/倍率描述符。
    if len(value) > 16_384:
        return
    cursor = 0
    while cursor < len(value):
        cursor = _SKIP.match(value, cursor).end()
        match = _URL_TOKEN.match(value, cursor)
        if match is None:
            break
        url = match[0]
        cursor = match.end()
        if url.endswith(","):
            yield url.rstrip(",")
            continue
        end = value.find(",", cursor)
        end = len(value) if end < 0 else end
        descriptor = value[cursor:end].strip()
        if not descriptor or _DESCRIPTOR.fullmatch(descriptor):
            yield url
        cursor = end + 1


def _references(document: html.HtmlElement) -> Iterator[str]:
    for element in document.iter("img", "source", "meta", "link"):
        if any(parent.tag in _INERT for parent in element.iterancestors()):
            continue
        if element.tag == "img":
            if src := element.get("src"):
                yield src
            yield from _srcset(element.get("srcset", ""))
        elif element.tag == "source":
            if (parent := element.getparent()) is not None and parent.tag == "picture":
                yield from _srcset(element.get("srcset", ""))
        elif element.tag == "meta":
            name = (element.get("property") or element.get("name") or "").casefold()
            if name in _IMAGE_META and (content := element.get("content")):
                yield content
        elif "image_src" in element.get("rel", "").casefold().split() and (
            href := element.get("href")
        ):
            yield href


def _matches(document: html.HtmlElement, page_url: str, original_url: str) -> bool:
    original = public_link(original_url)
    if original is None:
        return False
    identity = canonical_url(original)
    has_base = any(element.get("href") for element in document.iter("base"))
    for reference in _references(document):
        candidate = reference.strip()
        if not candidate or len(candidate) > 4096 or any(char.isspace() for char in candidate):
            continue
        try:
            if has_base and not urlsplit(candidate).scheme:
                continue
            linked = public_link(urljoin(page_url, candidate))
        except ValueError:
            continue
        if linked and canonical_url(linked) == identity:
            return True
    return False


class PublicImageSourceResolver:
    """L3 中核对来源网页是否明确引用原图，保留独立的版权状态。"""

    def __init__(self, fetcher: SafeFetcher):
        self._fetcher = fetcher

    async def resolve(
        self,
        result: ImageSearchResult,
        *,
        timeout_s: float,
        domain_allowlist: Sequence[str] = (),
        domain_denylist: Sequence[str] = (),
    ) -> ImageSearchResult:
        image = result.image
        if not image.source_page_url or image.source_page_verified or timeout_s <= 0:
            return result
        try:
            page = await self._fetcher.fetch(
                image.source_page_url,
                timeout_s=timeout_s,
                domain_allowlist=domain_allowlist,
                domain_denylist=domain_denylist,
            )
            matched = False
            if page.content_type in {"text/html", "application/xhtml+xml"}:
                document = parse_static_html(page)
                snippet_limit(
                    [
                        node.get("content", "")
                        for node in document.iter("meta")
                        if node.get("name", "").casefold() in {"robots", "multisearchbot"}
                    ]
                )
                matched = _matches(document, page.url, image.original_url)
        except FetchError as error:
            if error.code != "FETCH_NOT_FOUND" or error.status not in (404, 410):
                return result.model_copy(
                    update={
                        "warnings": list(
                            dict.fromkeys([*result.warnings, "image_source_verification_failed"])
                        )
                    }
                )
            checked = image.model_copy(
                update={"source_page_status": "invalid", "source_checked_at": datetime.now(UTC)}
            )
            return result.model_copy(
                update={
                    "image": checked,
                    "warnings": list(dict.fromkeys([*result.warnings, "image_source_invalid"])),
                }
            )
        checked = image.model_copy(
            update={
                "source_page_verified": matched,
                "source_page_status": "checked",
                "source_checked_at": page.retrieved_at,
            }
        )
        warnings = (
            [warning for warning in result.warnings if warning != "image_source_not_matched"]
            if matched
            else list(dict.fromkeys([*result.warnings, "image_source_not_matched"]))
        )
        return result.model_copy(update={"image": checked, "warnings": warnings})
