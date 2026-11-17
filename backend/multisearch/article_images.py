import json
import re
from collections.abc import Iterator
from itertools import islice
from urllib.parse import urljoin

from lxml import html

from .evidence_contracts import MAX_ARTICLE_IMAGES, ArticleImage
from .pipeline import canonical_url, public_link
from .safe_fetch import FetchError, validate_fetch_url

IMAGE_META = {
    "og:image",
    "og:image:url",
    "og:image:secure_url",
    "twitter:image",
    "twitter:image:src",
}
_ARTICLE_TYPES = {
    "AnalysisNewsArticle",
    "Article",
    "BackgroundNewsArticle",
    "BlogPosting",
    "LiveBlogPosting",
    "NewsArticle",
    "OpinionNewsArticle",
    "ReportageNewsArticle",
    "ReviewNewsArticle",
    "ScholarlyArticle",
    "TechArticle",
}
_DESCRIPTOR = re.compile(r"(?:[1-9][0-9]*w|(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)x)")
_URL_TOKEN = re.compile(r"[^ \t\r\n\f]+")
_SKIP = re.compile(r"[ \t\r\n\f,]*")
_NON_CONTENT = {"aside", "footer", "form", "nav", "noscript", "template"}
_LAZY_SRC = ("data-src", "data-original", "data-lazy-src")
_SRCSETS = ("data-srcset", "data-lazy-srcset", "srcset")


def srcset_candidates(value: str) -> Iterator[tuple[str, str]]:
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
            yield url.rstrip(","), ""
            continue
        end = value.find(",", cursor)
        end = len(value) if end < 0 else end
        descriptor = value[cursor:end].strip()
        if not descriptor or _DESCRIPTOR.fullmatch(descriptor):
            yield url, descriptor
        cursor = end + 1


def _image_url(value: object, base: str) -> str | None:
    if not isinstance(value, str) or len(value) > 4096:
        return None
    value = value.strip()
    if not value or value.startswith("#") or any(ord(char) < 32 for char in value):
        return None
    try:
        candidate = public_link(urljoin(base, value))
        if candidate is not None:
            validate_fetch_url(candidate)
        return candidate
    except (FetchError, ValueError):
        return None


def _base_url(document: html.HtmlElement, page_url: str) -> str:
    head = document.find("head")
    base = head.find("base[@href]") if head is not None else None
    if base is None or not base.get("href").strip():
        return page_url
    # 不合规的 <base> 仍允许不依赖它的绝对地址。
    return _image_url(base.get("href"), page_url) or ""


def _annotation(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    bounded = value[: 4 * limit].encode("utf-8", errors="replace").decode("utf-8")
    return " ".join(bounded.split())[:limit].strip() or None


def _article_nodes(value: object, depth: int = 0) -> Iterator[dict]:
    if depth > 5:
        return
    if isinstance(value, list):
        for item in value[:64]:
            yield from _article_nodes(item, depth + 1)
    elif isinstance(value, dict):
        types = value.get("@type")
        types = types[:8] if isinstance(types, list) else [types]
        if any(
            isinstance(item, str)
            and item.removeprefix("https://schema.org/")
            .removeprefix("http://schema.org/")
            .removeprefix("schema:")
            in _ARTICLE_TYPES
            for item in types
        ):
            yield value
        for key in ("mainEntity", "@graph"):
            if key in value:
                yield from _article_nodes(value[key], depth + 1)


def _structured_images(document: html.HtmlElement, base: str, limit: int) -> Iterator[ArticleImage]:
    for script in document.iter("script"):
        if script.get("type", "").casefold() != "application/ld+json" or not script.text:
            continue
        if len(script.text) > 64 * 1024 or any(_excluded(node) for node in script.iterancestors()):
            continue
        try:
            structured = json.loads(script.text)
        except (ValueError, RecursionError):
            continue
        for article in _article_nodes(structured):
            images = article.get("image")
            for item in images[:32] if isinstance(images, list) else [images]:
                references = (
                    (item.get("contentUrl"), item.get("url")) if isinstance(item, dict) else (item,)
                )
                url = next(
                    (link for value in references if (link := _image_url(value, base))),
                    None,
                )
                if url:
                    yield ArticleImage(
                        url=url,
                        source="structured_data",
                        caption=_annotation(item.get("caption"), limit)
                        if isinstance(item, dict)
                        else None,
                    )


def _metadata_images(head: html.HtmlElement, base: str) -> Iterator[ArticleImage]:
    for element in head.iter("meta", "link"):
        if element.tag == "meta":
            key = (element.get("property") or element.get("name") or "").casefold()
            value = element.get("content") if key in IMAGE_META else None
        else:
            rel = element.get("rel", "").casefold().split()
            value = element.get("href") if "image_src" in rel else None
        if url := _image_url(value, base):
            yield ArticleImage(url=url, source="page_metadata")


def _excluded(node: html.HtmlElement) -> bool:
    return (
        node.tag in _NON_CONTENT
        or "hidden" in node.attrib
        or "data-nosnippet" in node.attrib
        or node.get("aria-hidden", "").casefold() == "true"
    )


def _responsive_url(node: html.HtmlElement, base: str) -> str | None:
    for attr in _SRCSETS:
        best_score = (False, 0.0)
        selected = None
        for value, descriptor in srcset_candidates(node.get(attr, "")):
            score = (descriptor.endswith("w"), float(descriptor[:-1]) if descriptor else 1.0)
            if score > best_score and (url := _image_url(value, base)):
                best_score, selected = score, url
        if selected:
            return selected
    return None


def _body_image_url(
    node: html.HtmlElement, parents: list[html.HtmlElement], base: str
) -> str | None:
    # lxml 可能把 HTML5 的 <source> 解析成 <img> 的父元素。
    picture = next((parent for parent in parents if parent.tag == "picture"), None)
    if picture is not None:
        for source in picture.iter("source"):
            if not _excluded(source) and (url := _responsive_url(source, base)):
                return url
    if url := _responsive_url(node, base):
        return url
    return next(
        (url for attr in (*_LAZY_SRC, "src") if (url := _image_url(node.get(attr), base))),
        None,
    )


def _body_images(document: html.HtmlElement, base: str, limit: int) -> Iterator[ArticleImage]:
    body = document.find("body")
    if body is None:
        return
    main = next(body.iter("main"), body)
    root = next(
        (
            node
            for node in main.iter("article")
            if not _excluded(node)
            and not any(
                _excluded(parent) or parent.tag == "header" for parent in node.iterancestors()
            )
        ),
        main,
    )
    for node in islice(root.iter("img"), 256):
        parents = list(node.iterancestors())
        if _excluded(node) or any(
            _excluded(parent) or (parent.tag == "header" and root.tag != "article")
            for parent in parents
        ):
            continue
        dimensions = (node.get("width", ""), node.get("height", ""))
        if any(re.fullmatch(r"[0-9]{1,2}", value) and int(value) < 32 for value in dimensions):
            continue
        if url := _body_image_url(node, parents, base):
            figure = next((parent for parent in parents if parent.tag == "figure"), None)
            caption = figure.find("figcaption") if figure is not None else None
            yield ArticleImage(
                url=url,
                source="article_body",
                alt=_annotation(node.get("alt"), limit),
                caption=_annotation(caption.text_content(), limit)
                if caption is not None and not any(_excluded(child) for child in caption.iter())
                else None,
            )


def extract_article_images(
    original: html.HtmlElement, body: html.HtmlElement, page_url: str, text_limit: int
) -> list[ArticleImage]:
    limit = min(256, text_limit)
    original_base = _base_url(original, page_url)
    head = original.find("head")
    images: dict[str, ArticleImage] = {}
    for candidates in (
        _structured_images(original, original_base, limit),
        _metadata_images(head, original_base) if head is not None else (),
        _body_images(
            body, _base_url(body, page_url) if body is not original else original_base, limit
        ),
    ):
        for candidate in islice(candidates, 64):
            key = canonical_url(candidate.url)
            if existing := images.get(key):
                images[key] = existing.model_copy(
                    update={
                        "alt": existing.alt or candidate.alt,
                        "caption": existing.caption or candidate.caption,
                    }
                )
            elif len(images) < MAX_ARTICLE_IMAGES:
                images[key] = candidate
    return list(images.values())
