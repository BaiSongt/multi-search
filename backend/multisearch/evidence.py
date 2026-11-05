import codecs
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from time import monotonic
from typing import Literal
from unicodedata import category, normalize

from lxml import etree, html

from .contracts import Scope
from .crawl_parser import CRAWL4AI_VERSION, CrawlParser
from .evidence_contracts import STATIC_EXTRACTION_VERSION
from .evidence_contracts import DocumentIdentifier as DocumentIdentifier
from .evidence_contracts import EvidenceLocator as EvidenceLocator
from .evidence_contracts import EvidenceRecord as EvidenceRecord
from .safe_fetch import FetchedPage, FetchError, SafeFetcher, snippet_limit

_MAX_EXCERPT = 512
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_BLOCKS = {
    "address",
    "article",
    "blockquote",
    "br",
    "dd",
    "div",
    "dl",
    "dt",
    "figcaption",
    "figure",
    "li",
    "main",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "td",
    "th",
    "tr",
    "ul",
}
_HIDDEN = {
    "aside",
    "audio",
    "button",
    "canvas",
    "footer",
    "form",
    "head",
    "header",
    "iframe",
    "img",
    "input",
    "nav",
    "noscript",
    "object",
    "script",
    "select",
    "style",
    "svg",
    "template",
    "textarea",
    "video",
}
_BOT_NAMES = {"robots", "multisearchbot"}
_DOI = re.compile(r"10\.\d{4,9}/[!-~]+", re.IGNORECASE)


@dataclass(frozen=True)
class _Block:
    text: str
    section: str | None
    heading: bool = False


def _clean(value: str) -> str:
    text = normalize("NFC", value)
    return " ".join(
        "".join(c for c in text if c.isspace() or not category(c).startswith("C")).split()
    )


def _charset(name: str | None) -> str | None:
    if not name or len(name) > 40:
        return None
    try:
        codec = codecs.lookup(name).name
    except LookupError:
        return None
    prefixes = ("utf-", "iso8859-", "cp12", "gb", "big5", "shift_jis", "euc", "koi8")
    return codec if codec.startswith(prefixes) or codec in {"ascii", "cp949"} else None


def _html_blocks(root: html.HtmlElement) -> list[_Block]:
    blocks: list[_Block] = []
    current: list[str] = []
    section: str | None = None

    def flush() -> None:
        text = _clean("".join(current))
        if text:
            blocks.append(_Block(text, section))
        current.clear()

    def walk(node: html.HtmlElement) -> None:
        nonlocal section
        tag = node.tag
        if (
            not isinstance(tag, str)
            or tag in _HIDDEN
            or "hidden" in node.attrib
            or node.get("aria-hidden", "").casefold() == "true"
        ):
            return
        if tag in _HEADINGS:
            flush()
            heading = _clean(node.text_content())
            if heading:
                section = heading[:200]
                blocks.append(_Block(heading, section, heading=True))
            return
        if tag in _BLOCKS:
            flush()
        if node.text:
            current.append(node.text)
        for child in node:
            walk(child)
            if child.tail:
                current.append(child.tail)
        if tag in _BLOCKS:
            flush()

    walk(root)
    flush()
    return blocks


def parse_static_html(page: FetchedPage) -> html.HtmlElement:
    try:
        parser = html.HTMLParser(
            encoding=_charset(page.charset), no_network=True, recover=True, huge_tree=False
        )
        return html.document_fromstring(page.body, parser=parser)
    except (etree.LxmlError, LookupError, ValueError):
        raise FetchError(422, "FETCH_NO_EXCERPT", "来源未包含可提取正文") from None


def _html_page(page: FetchedPage) -> tuple[list[_Block], dict[str, str], int | None]:
    document = parse_static_html(page)
    directives: list[str] = []
    metadata: dict[str, str] = {}
    for meta in document.iter("meta"):
        name = (meta.get("name") or meta.get("property") or "").casefold()
        value = meta.get("content")
        if not value:
            continue
        if name in _BOT_NAMES:
            directives.append(value)
        elif name in {"citation_doi", "article:published_time", "og:title"}:
            metadata.setdefault(name, _clean(value)[:512])
    title = document.find(".//title")
    if title is not None:
        metadata.setdefault("title", _clean(title.text_content())[:512])
    # 选择主区域，忽略导航和脚本；空主区域回退到正文。
    body = document.find("body")
    if body is None:
        return [], metadata, snippet_limit(directives)
    main = next((element for element in body.iter() if element.tag in {"main", "article"}), None)
    blocks = _html_blocks(main) if main is not None else []
    if not any(not block.heading for block in blocks):
        blocks = _html_blocks(body)
    return blocks, metadata, snippet_limit(directives)


def _publication(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(UTC) if parsed.tzinfo else None
    except (ValueError, OverflowError):
        return None


def _content(
    blocks: list[_Block],
    kind: Literal["html_text", "plain_text"],
    query: str | None,
    excerpt_limit: int,
    hints: tuple[str, ...],
) -> tuple[str, EvidenceLocator, str, bool]:
    terms = list(dict.fromkeys(re.findall(r"\w{2,}", query or "")))[:16]
    choices: list[tuple[int, bool, int, _Block]] = []
    offset = 0
    for block in blocks:
        if not block.heading:
            folded = block.text.casefold()
            score = sum(term.casefold() in folded for term in terms)
            selected = bool(hints) and block.text[:512] in hints
            choices.append((score, selected, offset, block))
        offset += len(block.text) + 1
    if not choices:
        raise FetchError(422, "FETCH_NO_EXCERPT", "来源未包含可提取正文")
    _, selected, offset, block = max(choices, key=lambda choice: choice[:2])
    relative_start = 0
    if terms:
        expression = "|".join(re.escape(term) for term in terms)
        match = re.search(expression, block.text, flags=re.IGNORECASE)
        if match is not None:
            relative_start = max(0, match.start() - min(80, excerpt_limit // 3))
    window = block.text[relative_start : relative_start + excerpt_limit]
    excerpt = window.strip()
    if not excerpt:
        raise FetchError(422, "FETCH_NO_EXCERPT", "来源未包含可提取正文")
    start = offset + relative_start + len(window) - len(window.lstrip())
    locator = EvidenceLocator(
        kind=kind,
        section=block.section,
        text_offset_start=start,
        text_offset_end=start + len(excerpt),
    )
    text_sha = sha256("\n".join(block.text for block in blocks).encode("utf-8")).hexdigest()
    return excerpt, locator, text_sha, selected


class EvidenceBuilder:
    def __init__(self, fetcher: SafeFetcher, parser: CrawlParser | None = None):
        self.fetcher = fetcher
        self.parser = parser

    async def extract(
        self,
        url: str,
        *,
        timeout_s: float,
        result_id: str | None = None,
        query: str | None = None,
        network_scope: Scope | str = Scope.PUBLIC,
        domain_allowlist: Sequence[str] = (),
        domain_denylist: Sequence[str] = (),
    ) -> EvidenceRecord:
        deadline = monotonic() + timeout_s
        page = await self.fetcher.fetch(
            url,
            timeout_s=timeout_s,
            network_scope=network_scope,
            domain_allowlist=domain_allowlist,
            domain_denylist=domain_denylist,
        )
        warnings: list[str] = []
        hints: tuple[str, ...] = ()
        if page.content_type in {"text/html", "application/xhtml+xml"}:
            blocks, metadata, meta_limit = _html_page(page)
            kind = "html_text"
            if self.parser and any(not block.heading for block in blocks):
                raw_html = page.body.decode(_charset(page.charset) or "utf-8-sig", errors="replace")
                parser_budget = min(4.5, (deadline - monotonic()) * 0.75)
                parsed = await self.parser.select(raw_html, timeout_s=parser_budget)
                if parsed is None:
                    warnings.append("crawl4ai_parser_unavailable")
                else:
                    hints = parsed
        else:
            text = page.body.decode(_charset(page.charset) or "utf-8-sig", errors="replace")
            blocks = [_Block(line, None) for value in text.splitlines() if (line := _clean(value))]
            metadata, meta_limit = {}, None
            kind = "plain_text"
        excerpt_limit = min(
            value for value in (_MAX_EXCERPT, page.max_snippet, meta_limit) if value is not None
        )
        excerpt, locator, text_sha, selected = _content(blocks, kind, query, excerpt_limit, hints)
        if monotonic() >= deadline:
            raise FetchError(504, "TIME_BUDGET_EXCEEDED", "正文提取时限已耗尽", retryable=True)
        doi = metadata.get("citation_doi", "").removeprefix("https://doi.org/")
        identity = f"{page.url}\n{text_sha}\n{locator.text_offset_start}:{locator.text_offset_end}"
        return EvidenceRecord(
            evidence_id=f"ev_{sha256(identity.encode('utf-8')).hexdigest()[:32]}",
            result_id=result_id,
            origin_url=page.origin_url,
            canonical_url=page.url,
            document_identifier=DocumentIdentifier(type="doi", value=doi)
            if _DOI.fullmatch(doi)
            else None,
            retrieved_at=page.retrieved_at,
            published_at=_publication(metadata.get("article:published_time", "")),
            title=metadata.get("title") or metadata.get("og:title"),
            content_type=page.content_type,
            evidence_text=excerpt,
            locator=locator,
            content_sha256=text_sha,
            extraction_method="crawl4ai_static"
            if selected
            else ("static_html" if locator.kind == "html_text" else "plain_text"),
            extraction_version=(
                f"{CRAWL4AI_VERSION}+normalizer.{STATIC_EXTRACTION_VERSION}"
                if selected
                else STATIC_EXTRACTION_VERSION
            ),
            warnings=warnings,
        )
