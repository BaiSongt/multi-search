from __future__ import annotations

import asyncio
import codecs
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from time import monotonic
from typing import TYPE_CHECKING, Literal
from unicodedata import category, normalize
from urllib.parse import urlsplit

from lxml import etree, html

from .contracts import Scope
from .crawl_parser import CRAWL4AI_VERSION, CrawlParser
from .evidence_contracts import STATIC_EXTRACTION_VERSION, ExtractionMode, FetchStatus
from .evidence_contracts import DocumentIdentifier as DocumentIdentifier
from .evidence_contracts import EvidenceLocator as EvidenceLocator
from .evidence_contracts import EvidenceRecord as EvidenceRecord
from .pdf_parser import PDF_EXTRACTION_VERSION, OCRPending, PdfParser
from .render_resources import MAX_RESOURCE_BUNDLE_BYTES, MAX_RESOURCE_BYTES, ResourceSpec
from .safe_fetch import FetchedPage, FetchError, SafeFetcher, snippet_limit
from .tracing import trace_event

if TYPE_CHECKING:
    from .dynamic_renderer import DynamicRenderer
    from .render_resources import FetchedResource

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
_COEP = re.compile(
    r'(unsafe-none|require-corp|credentialless)(?:[ \t]*;[ \t]*report-to="[\x20\x21\x23-\x5b\x5d-\x7e]{1,256}")?'
)
_BLOCKED_FETCH_CODES = {
    "FETCH_TARGET_BLOCKED",
    "FETCH_ROBOTS_DENIED",
    "FETCH_ROBOTS_UNAVAILABLE",
    "FETCH_RENDER_POLICY_UNSUPPORTED",
    "FETCH_RESOURCE_REDIRECT",
    "FETCH_RESOURCE_CORS_DENIED",
    "FETCH_RESOURCE_POLICY_UNSUPPORTED",
    "FETCH_SITE_RESTRICTED",
}


@dataclass(frozen=True)
class _Block:
    text: str
    section: str | None
    heading: bool = False
    page_number: int | None = None


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


def _html_metadata(document: html.HtmlElement) -> tuple[dict[str, str], int | None]:
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
    return metadata, snippet_limit(directives)


def _html_body(document: html.HtmlElement) -> list[_Block]:
    # 选择主区域，忽略导航和脚本；空主区域回退到正文。
    body = document.find("body")
    if body is None:
        return []
    main = next((element for element in body.iter() if element.tag in {"main", "article"}), None)
    blocks = _html_blocks(main) if main is not None else []
    if not any(not block.heading for block in blocks):
        blocks = _html_blocks(body)
    return blocks


def _publication(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(UTC) if parsed.tzinfo else None
    except (ValueError, OverflowError):
        return None


def _content(
    blocks: list[_Block],
    kind: Literal["html_text", "rendered_html_text", "plain_text", "pdf_text"],
    query: str | None,
    excerpt_limit: int,
    hints: tuple[str, ...],
) -> tuple[str, EvidenceLocator, str, bool]:
    terms = list(dict.fromkeys(re.findall(r"\w{2,}", query or "")))[:16]
    choices: list[tuple[int, bool, int, _Block]] = []
    offset = 0
    for block in blocks:
        if not block.heading and block.text:
            folded = block.text.casefold()
            score = sum(term.casefold() in folded for term in terms)
            selected = bool(hints) and block.text[:512] in hints
            choices.append((score, selected, 0 if block.page_number is not None else offset, block))
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
        page_number=block.page_number,
    )
    text_sha = sha256("\n".join(block.text for block in blocks).encode("utf-8")).hexdigest()
    return excerpt, locator, text_sha, selected


class EvidenceBuilder:
    def __init__(
        self,
        fetcher: SafeFetcher,
        parser: CrawlParser | None = None,
        *,
        record_attempt: Callable[[str | None, FetchStatus], None] | None = None,
        renderer: DynamicRenderer | None = None,
        dynamic_hosts: frozenset[str] = frozenset(),
        dynamic_resource_hosts: frozenset[str] = frozenset(),
        dynamic_resources: Mapping[str, tuple[ResourceSpec, ...]] | None = None,
    ):
        self.fetcher = fetcher
        self.parser = parser
        self.pdf = PdfParser()
        self.record_attempt = record_attempt
        self.renderer = renderer
        self.dynamic_hosts = dynamic_hosts
        self.dynamic_resource_hosts = dynamic_resource_hosts
        self.dynamic_resources = dict(dynamic_resources or {})

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
        extraction_mode: ExtractionMode = "static",
    ) -> EvidenceRecord:
        started = monotonic()
        deadline = started + timeout_s
        page = None
        status: FetchStatus = "failed"
        error_code = None
        dynamic = extraction_mode == "dynamic"
        try:
            if dynamic and (self.renderer is None or not self.dynamic_hosts):
                raise FetchError(
                    503, "FETCH_RENDER_UNAVAILABLE", "动态渲染暂不可用", retryable=True
                )
            page = await self.fetcher.fetch(
                url,
                timeout_s=timeout_s,
                network_scope=network_scope,
                domain_allowlist=domain_allowlist,
                domain_denylist=domain_denylist,
                authorized_hosts=self.dynamic_hosts if dynamic else None,
            )
            evidence = await self._extract_page(
                page,
                deadline,
                result_id,
                query,
                dynamic=dynamic,
                domain_allowlist=domain_allowlist,
                domain_denylist=domain_denylist,
            )
        except OCRPending as error:
            error_code = error.code
            status = "ocr_pending"
            raise
        except FetchError as error:
            error_code = error.code
            if error.code == "TIME_BUDGET_EXCEEDED":
                status = "timeout"
            elif error.code in _BLOCKED_FETCH_CODES:
                status = "blocked"
            raise
        except TimeoutError:
            status = "timeout"
            raise
        except asyncio.CancelledError:
            status = "timeout" if monotonic() >= deadline else "cancelled"
            raise
        else:
            status = "completed"
            return evidence
        finally:
            if self.record_attempt is not None:
                self.record_attempt(page.content_type if page is not None else None, status)
            trace_event(
                "fetch.extract",
                status=status,
                elapsed_ms=int((monotonic() - started) * 1000),
                error_code=error_code,
            )

    async def _extract_page(
        self,
        page: FetchedPage,
        deadline: float,
        result_id: str | None,
        query: str | None,
        *,
        dynamic: bool,
        domain_allowlist: Sequence[str],
        domain_denylist: Sequence[str],
    ) -> EvidenceRecord:
        warnings: list[str] = []
        hints: tuple[str, ...] = ()
        render_version = None
        bundle_sha = None
        resource_limit = None
        if dynamic and page.content_type != "text/html":
            raise FetchError(415, "FETCH_CONTENT_UNSUPPORTED", "动态渲染仅支持 HTML")
        if page.content_type == "application/pdf":
            if page.max_snippet == 0:
                raise FetchError(422, "FETCH_NO_EXCERPT", "来源未包含可提取正文")
            parsed_pdf = await self.pdf.parse(page.body, timeout_s=deadline - monotonic())
            if parsed_pdf.has_visual_content and not any(parsed_pdf.pages):
                raise OCRPending()
            blocks = [
                _Block(text, None, page_number=index)
                for index, text in enumerate(parsed_pdf.pages, start=1)
            ]
            metadata, meta_limit = {}, None
            kind = "pdf_text"
            if parsed_pdf.truncated:
                warnings.append("pdf_page_or_text_limit_reached")
        elif page.content_type in {"text/html", "application/xhtml+xml"}:
            document = parse_static_html(page)
            metadata, meta_limit = _html_metadata(document)
            kind = "html_text"
            if dynamic:
                document, render_version, bundle_sha, resource_limit = await self._render_document(
                    page, deadline, domain_allowlist, domain_denylist
                )
                _, rendered_limit = _html_metadata(document)
                if rendered_limit is not None:
                    meta_limit = (
                        min(meta_limit, rendered_limit)
                        if meta_limit is not None
                        else rendered_limit
                    )
                kind = "rendered_html_text"
            blocks = _html_body(document)
            if not dynamic and self.parser and any(not block.heading for block in blocks):
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
            value
            for value in (_MAX_EXCERPT, page.max_snippet, meta_limit, resource_limit)
            if value is not None
        )
        excerpt, locator, text_sha, selected = _content(blocks, kind, query, excerpt_limit, hints)
        if monotonic() >= deadline:
            raise FetchError(504, "TIME_BUDGET_EXCEEDED", "正文提取时限已耗尽", retryable=True)
        doi = metadata.get("citation_doi", "").removeprefix("https://doi.org/")
        identity = f"{page.url}\n{text_sha}\n{locator.text_offset_start}:{locator.text_offset_end}"
        if locator.page_number is not None:
            identity += f"\npage:{locator.page_number}"
        if render_version is not None:
            method = "browser_dom"
            extraction_version = f"{render_version}+normalizer.{STATIC_EXTRACTION_VERSION}"
            identity += f"\nrender:{extraction_version}"
            if bundle_sha is not None:
                identity += f"\nbundle:{bundle_sha}"
        elif locator.kind == "pdf_text":
            method, extraction_version = "pypdf", PDF_EXTRACTION_VERSION
        elif selected:
            method = "crawl4ai_static"
            extraction_version = f"{CRAWL4AI_VERSION}+normalizer.{STATIC_EXTRACTION_VERSION}"
        else:
            method = "static_html" if locator.kind == "html_text" else "plain_text"
            extraction_version = STATIC_EXTRACTION_VERSION
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
            extraction_method=method,
            extraction_version=extraction_version,
            render_bundle_sha256=bundle_sha,
            warnings=warnings,
        )

    async def _render_document(
        self,
        page: FetchedPage,
        deadline: float,
        domain_allowlist: Sequence[str],
        domain_denylist: Sequence[str],
    ) -> tuple[html.HtmlElement, str, str | None, int | None]:
        assert self.renderer is not None
        source_csp = ", ".join(
            value
            for header in page.content_security_policy
            if (value := header.replace("\t", " ").strip())
        )
        if source_csp and (
            len(source_csp) > 4096 or not source_csp.isascii() or not source_csp.isprintable()
        ):
            raise FetchError(
                403, "FETCH_RENDER_POLICY_UNSUPPORTED", "无法安全保留来源页面的内容策略"
            )
        charset = _charset(page.charset) if page.charset else "utf-8-sig"
        if charset not in {"ascii", "utf-8", "utf-8-sig"}:
            raise FetchError(415, "FETCH_CONTENT_UNSUPPORTED", "动态渲染需要 UTF-8 或 ASCII HTML")
        try:
            raw_html = page.body.decode("ascii" if charset == "ascii" else "utf-8-sig")
        except UnicodeDecodeError:
            raise FetchError(
                415, "FETCH_CONTENT_UNSUPPORTED", "动态渲染需要有效的 UTF-8 HTML"
            ) from None
        specs = self.dynamic_resources.get(page.url, ())
        page_host = urlsplit(page.url).netloc
        cross_origin = any(urlsplit(spec.url).netloc != page_host for spec in specs)
        source_coep = None
        if cross_origin and (policies := page.cross_origin_embedder_policy):
            parsed = _COEP.fullmatch(policies[0].strip()) if len(policies) == 1 else None
            if parsed is None:
                raise FetchError(
                    403, "FETCH_RENDER_POLICY_UNSUPPORTED", "无法安全保留来源页面的跨源嵌入策略"
                )
            source_coep = parsed[1]
        resources = await self._prefetch_resources(
            page.url, specs, deadline, domain_allowlist, domain_denylist
        )
        bundle_sha = None
        resource_limit = None
        if resources:
            rendered = await self.renderer.render_bundle(
                page.url,
                raw_html,
                resources,
                timeout_s=deadline - monotonic(),
                source_csp=source_csp or None,
                source_coep=source_coep,
            )
            bundle_sha = _bundle_digest(resources, cross_origin, source_coep)
            resource_limit = min(
                (asset.max_snippet for asset in resources if asset.max_snippet is not None),
                default=None,
            )
        else:
            rendered = await self.renderer.render(
                raw_html, timeout_s=deadline - monotonic(), source_csp=source_csp or None
            )
        if rendered is None:
            raise FetchError(503, "FETCH_RENDER_UNAVAILABLE", "动态渲染暂不可用", retryable=True)
        snapshot = replace(page, body=rendered.html.encode("utf-8"), charset="utf-8")
        return parse_static_html(snapshot), rendered.version, bundle_sha, resource_limit

    async def _prefetch_resources(
        self,
        page_url: str,
        specs: tuple[ResourceSpec, ...],
        deadline: float,
        domain_allowlist: Sequence[str],
        domain_denylist: Sequence[str],
    ) -> list[FetchedResource]:
        remaining_bytes = MAX_RESOURCE_BUNDLE_BYTES
        resources = []
        for spec in specs:
            if remaining_bytes <= 0:
                raise FetchError(413, "FETCH_BODY_TOO_LARGE", "动态资源总响应超过上限")
            asset = await self.fetcher.fetch_resource(
                spec,
                page_url=page_url,
                timeout_s=deadline - monotonic(),
                authorized_hosts=self.dynamic_hosts,
                authorized_resource_hosts=self.dynamic_resource_hosts,
                domain_allowlist=domain_allowlist,
                domain_denylist=domain_denylist,
                max_body_bytes=min(MAX_RESOURCE_BYTES, remaining_bytes),
            )
            remaining_bytes -= len(asset.body.encode("utf-8"))
            resources.append(asset)
        return resources


def _bundle_digest(
    resources: list[FetchedResource], cross_origin: bool, source_coep: str | None
) -> str:
    items = []
    for asset in sorted(resources, key=lambda item: item.url):
        body_sha = sha256(asset.body.encode("utf-8")).hexdigest()
        item: tuple[str | None, ...] = (asset.url, asset.kind, asset.content_type, body_sha)
        if cross_origin:
            item += (asset.allow_origin, asset.resource_policy)
        items.append(item)
    inputs = {"resources": items, "source_coep": source_coep} if cross_origin else items
    return sha256(json.dumps(inputs, separators=(",", ":")).encode("utf-8")).hexdigest()
