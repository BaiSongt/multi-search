import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from hashlib import sha256
from ipaddress import ip_address
from urllib.parse import unquote_plus, urlsplit, urlunsplit

from .contracts import (
    ImageAttribution,
    ImageOptions,
    ImageSearchResult,
    Modality,
    SearchRequest,
    SearchResult,
    Source,
    TextSearchResult,
)

_TRACKING_KEYS = {"fbclid", "gclid", "msclkid", "dclid"}
_LOCAL_SUFFIXES = {"localhost", "local", "internal", "lan", "test", "invalid", "onion"}
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_RESOLUTION = re.compile(r"(\d{1,6})\s*[xX×]\s*(\d{1,6})")
_IMAGE_FORMATS = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "avif": "image/avif",
    "bmp": "image/bmp",
    "tiff": "image/tiff",
}


def public_link(value: object) -> str | None:
    """检查结果链接格式；此处仅返回元数据，不解析 DNS 或访问候选 URL。"""
    if not isinstance(value, str) or len(value) > 4096:
        return None
    value = value.strip()
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c == "\\" for c in value):
        return None
    try:
        value.encode("utf-8")
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
        if parts.scheme not in {"http", "https"} or not host or parts.username is not None:
            return None
        if "%" in host:
            return None
        host = host.rstrip(".").encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return None
    try:
        address = ip_address(host)
    except ValueError:
        labels = host.split(".")
        if (
            len(labels) < 2
            or len(host) > 253
            or not all(_LABEL.fullmatch(label) for label in labels)
            or not labels[-1][0].isalpha()
            or labels[-1] in _LOCAL_SUFFIXES
        ):
            return None
    else:
        if not address.is_global or address.is_multicast:
            return None
        if address.version == 6:
            host = f"[{address.compressed}]"
    netloc = host if port in {None, {"http": 80, "https": 443}[parts.scheme]} else f"{host}:{port}"
    return urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, parts.fragment))


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    query = []
    for parameter in parts.query.split("&"):
        name = unquote_plus(parameter.partition("=")[0]).lower()
        if not name.startswith("utm_") and name not in _TRACKING_KEYS:
            query.append(parameter)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(query), ""))


def _matches_domain(url: str, domains: list[str]) -> bool:
    host = urlsplit(url).hostname or ""
    return any(host == domain.lower() or host.endswith("." + domain.lower()) for domain in domains)


def _text(value: object, limit: int) -> str | None:
    if isinstance(value, str):
        value = value[:limit].encode("utf-8", errors="replace").decode("utf-8")
        return " ".join(value.split()) or None
    return None


def _dimension(value: object) -> int | None:
    if type(value) is int and 0 < value <= 100_000:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,6}", value):
        return _dimension(int(value))
    return None


def _publication_date(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 80:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
    try:
        return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (OverflowError, ValueError):
        return None


def _dimensions(row: dict) -> tuple[int | None, int | None]:
    width, height = _dimension(row.get("width")), _dimension(row.get("height"))
    resolution = row.get("resolution")
    if isinstance(resolution, str) and (matched := _RESOLUTION.fullmatch(resolution.strip())):
        width = width or _dimension(matched[1])
        height = height or _dimension(matched[2])
    return width, height


def _image_matches(width: int | None, height: int | None, options: ImageOptions) -> bool:
    if options.min_width and (width is None or width < options.min_width):
        return False
    if options.min_height and (height is None or height < options.min_height):
        return False
    if not options.orientation:
        return True
    if width is None or height is None:
        return False
    if width == height:
        return options.orientation == "square"
    return options.orientation == ("landscape" if width > height else "portrait")


def _engine_names(row: dict, ddgs_engine: str | None) -> list[str]:
    if ddgs_engine:
        return [ddgs_engine]
    names = row.get("engines")
    names = list(names[:15]) if isinstance(names, list) else []
    names.append(row.get("engine"))
    return sorted({name for value in names if (name := _text(value, 60))})


def _id(*parts: object) -> str:
    material = "\0".join(str(value) if value is not None else "" for value in parts)
    return sha256(material.encode("utf-8", errors="replace")).hexdigest()[:32]


def normalize(
    rows: list,
    request: SearchRequest,
    provider_id: str,
    source: Source,
    modality: Modality,
    limit: int,
    *,
    ddgs_engine: str | None = None,
) -> tuple[list[SearchResult], list[str]]:
    """将上游候选转换为外部契约并执行可核实的元数据过滤。"""
    results: list[SearchResult] = []
    missing_date = missing_landing = missing_dimensions = 0
    retrieved = datetime.now(UTC)
    options = request.image_options or ImageOptions()
    for row in rows:
        if not isinstance(row, dict):
            continue
        published = _publication_date(row.get("publishedDate") or row.get("date"))
        if request.time_range is not None:
            if published is None:
                missing_date += 1
                continue
            if not request.time_range.start <= published.date() <= request.time_range.end:
                continue
        title = _text(row.get("title"), 512)
        source_page = public_link(row.get("url") or row.get("href"))
        canonical_page = canonical_url(source_page) if source_page else None
        common = {
            "title": title
            or (urlsplit(source_page).hostname if source_page else None)
            or "Untitled",
            "url": source_page,
            "canonical_url": canonical_page,
            "source_type": source,
            "provider_ids": [provider_id],
            "published_at": published,
            "retrieved_at": retrieved,
            "snippet": _text(row.get("content") or row.get("body"), 2048),
        }
        if modality == Modality.TEXT:
            if not source_page or _matches_domain(source_page, request.domain_denylist):
                continue
            if request.domain_allowlist and not _matches_domain(
                source_page, request.domain_allowlist
            ):
                continue
            results.append(TextSearchResult(result_id=_id("text", canonical_page), **common))
            continue
        original = public_link(row.get("image" if ddgs_engine else "img_src"))
        if not original or _matches_domain(original, request.domain_denylist):
            continue
        canonical_original = canonical_url(original)
        if canonical_page == canonical_original:
            source_page = canonical_page = None
            common.update(url=None, canonical_url=None)
        if source_page is None:
            missing_landing += 1
            if options.source_page_required:
                continue
        elif _matches_domain(source_page, request.domain_denylist):
            continue
        if request.domain_allowlist and not _matches_domain(
            source_page or original,
            request.domain_allowlist,
        ):
            continue
        if options.license_policy != "any_with_status":
            continue
        width, height = _dimensions(row)
        if (width is None and (options.min_width or options.orientation)) or (
            height is None and (options.min_height or options.orientation)
        ):
            missing_dimensions += 1
        if not _image_matches(width, height, options):
            continue
        thumbnail = public_link(row.get("thumbnail_src")) or public_link(row.get("thumbnail"))
        if thumbnail and _matches_domain(thumbnail, request.domain_denylist):
            thumbnail = None
        image = ImageAttribution(
            original_url=original,
            thumbnail_url=thumbnail,
            source_page_url=source_page,
            source_site_domain=urlsplit(source_page).hostname if source_page else None,
            search_engines=_engine_names(row, ddgs_engine),
            width=width,
            height=height,
            mime_type=_IMAGE_FORMATS.get((_text(row.get("img_format"), 20) or "").lower()),
        )
        warnings = ["image_rights_unknown"]
        if source_page is None:
            warnings.append("image_source_page_missing")
        results.append(
            ImageSearchResult(
                result_id=_id("image", canonical_original, canonical_page, width, height),
                image=image,
                warnings=warnings,
                **common,
            ),
        )
    warnings = []
    if missing_date:
        warnings.append(f"{provider_id}:publication_date_missing:{missing_date}")
    if missing_landing:
        warnings.append(f"{provider_id}:image_source_page_missing:{missing_landing}")
    if missing_dimensions:
        warnings.append(f"{provider_id}:image_dimensions_missing:{missing_dimensions}")
    if modality == Modality.IMAGE and options.license_policy != "any_with_status":
        warnings.append(f"{provider_id}:image_license_verification_unavailable")
    return results[:limit], warnings


@dataclass
class _Group:
    result: SearchResult
    votes: dict[str, float] = field(default_factory=dict)

    def add(self, other: SearchResult, position: int) -> None:
        for provider_id in other.provider_ids:
            self.votes[provider_id] = max(self.votes.get(provider_id, 0.0), 1 / (60 + position))
        result = self.result
        updates = {
            "provider_ids": sorted(set(result.provider_ids) | set(other.provider_ids)),
            "warnings": sorted(set(result.warnings) | set(other.warnings)),
            "published_at": result.published_at or other.published_at,
        }
        if isinstance(result, ImageSearchResult) and isinstance(other, ImageSearchResult):
            first, second = result.image, other.image
            updates["image"] = first.model_copy(
                update={
                    "thumbnail_url": first.thumbnail_url or second.thumbnail_url,
                    "search_engines": sorted(
                        set(first.search_engines) | set(second.search_engines)
                    ),
                    "width": first.width or second.width,
                    "height": first.height or second.height,
                    "mime_type": first.mime_type or second.mime_type,
                }
            )
        self.result = result.model_copy(update=updates)


def _identity(result: SearchResult) -> tuple:
    if isinstance(result, ImageSearchResult):
        return ("image", canonical_url(result.image.original_url), result.canonical_url)
    return ("text", result.doi.casefold() if result.doi else result.canonical_url)


def _same_size(result: SearchResult, candidate: SearchResult) -> bool:
    if not isinstance(result, ImageSearchResult) or not isinstance(candidate, ImageSearchResult):
        return True
    return all(
        left is None or right is None or left == right
        for left, right in (
            (result.image.width, candidate.image.width),
            (result.image.height, candidate.image.height),
        )
    )


def fuse(
    batches: Iterable[list[SearchResult]],
    limit: int,
    image_limit: int,
) -> list[SearchResult]:
    """用倒数排名融合，每个 Provider 对同一结果最多贡献一次。"""
    groups: dict[tuple, list[_Group]] = {}
    for batch in batches:
        for position, item in enumerate(batch, start=1):
            candidates = groups.setdefault(_identity(item), [])
            matching = next((group for group in candidates if _same_size(group.result, item)), None)
            if matching is None:
                matching = _Group(item)
                candidates.append(matching)
            matching.add(item, position)
    ranked = sorted(
        (group for candidates in groups.values() for group in candidates),
        key=lambda group: -sum(group.votes.values()),
    )
    eligible: list[_Group] = []
    images = 0
    for group in ranked:
        if group.result.kind == "image":
            if images >= image_limit:
                continue
            images += 1
        eligible.append(group)
    selected = eligible[:limit]
    if len(selected) >= 2 and len({group.result.kind for group in selected}) == 1:
        other = next(
            (group for group in eligible[limit:] if group.result.kind != selected[0].result.kind),
            None,
        )
        if other is not None:
            selected[-1] = other
    return [
        group.result.model_copy(update={"relevance_score": round(sum(group.votes.values()), 6)})
        for group in selected
    ]
