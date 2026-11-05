import asyncio
import json
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from unicodedata import category, normalize
from urllib.parse import SplitResult, parse_qsl, unquote, unquote_to_bytes, urlsplit

import httpx

from .contracts import ImageSearchResult

_COMMONS = "commons.wikimedia.org"
_UPLOAD = "upload.wikimedia.org"
_API = f"https://{_COMMONS}/w/api.php"
_MAX_RESPONSE_BYTES = 65_536
_TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"}
_ORIGINAL_PATH = re.compile(rb"/wikipedia/commons/([0-9a-f])/\1[0-9a-f]/[^/]+")
_CC_PATH = re.compile(r"/licenses/(by|by-sa)/(1\.0|2\.0|2\.5|3\.0|4\.0)/?")
_ASSET_SHA1 = re.compile(r"[0-9a-fA-F]{40}")
_METADATA = (
    "Artist|Attribution|LicenseUrl|LicenseShortName|License|NonFree|Restrictions|DeletionReason"
)


def _url(value: object, hostname: str, schemes: tuple[str, ...] = ("https",)) -> SplitResult | None:
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    if any(char.isspace() or category(char).startswith("C") or char == "\\" for char in value):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    port = 443 if parts.scheme == "https" else 80
    if parts.scheme not in schemes or parts.netloc.lower() not in (hostname, f"{hostname}:{port}"):
        return None
    return parts


def _file_title(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    title = normalize("NFC", value.replace("_", " "))
    if not title.startswith("File:") or not 5 < len(title.encode("utf-8", errors="replace")) <= 260:
        return None
    name = title[5:]
    if name != name.strip() or any(
        char in "|[]{}<>#/\\?%" or category(char).startswith("C") for char in name
    ):
        return None
    return title


def _source_title(value: object) -> str | None:
    parts = _url(value, _COMMONS)
    if parts is None or not parts.path.startswith("/wiki/") or parts.query or parts.fragment:
        return None
    try:
        return _file_title(unquote(parts.path.removeprefix("/wiki/"), errors="strict"))
    except UnicodeError:
        return None


def _asset_path(value: object) -> bytes | None:
    parts = _url(value, _UPLOAD)
    if parts is None or parts.fragment:
        return None
    try:
        if any(
            key not in _TRACKING
            for key, _ in parse_qsl(parts.query, strict_parsing=True, max_num_fields=10)
        ):
            return None
        path = unquote_to_bytes(parts.path)
    except (UnicodeError, ValueError):
        return None
    if not _ORIGINAL_PATH.fullmatch(path) or any(
        byte <= 32 or byte in b"\\?#\x7f" for byte in path
    ):
        return None
    return path


class _PlainText(HTMLParser):
    _HIDDEN = {"script", "style", "template"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs):
        if tag in self._HIDDEN:
            self.hidden += 1

    def handle_endtag(self, tag: str):
        if tag in self._HIDDEN and self.hidden:
            self.hidden -= 1

    def handle_data(self, data: str):
        if not self.hidden:
            self.parts.append(data)


def _plain_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or len(value) > 8192:
        return None
    parser = _PlainText()
    try:
        parser.feed(value)
        parser.close()
    except (AssertionError, ValueError):
        return None
    raw = " ".join(parser.parts)
    safe = "".join(char for char in raw if char.isspace() or not category(char).startswith("C"))
    return " ".join(safe.split())[:limit] or None


def _metadata(metadata: dict, key: str, source: str = "commons-desc-page") -> object:
    entry = metadata.get(key)
    return entry.get("value") if isinstance(entry, dict) and entry.get("source") == source else None


def _license(value: object) -> tuple[str, str, str] | None:
    parts = _url(value, "creativecommons.org", ("http", "https"))
    if parts is None or parts.query or parts.fragment:
        return None
    if parts.path in ("/publicdomain/zero/1.0", "/publicdomain/zero/1.0/"):
        return "CC0 1.0", "https://creativecommons.org/publicdomain/zero/1.0/", "cc-zero"
    matched = _CC_PATH.fullmatch(parts.path)
    if matched:
        family, version = matched.groups()
        return (
            f"CC {family.upper()} {version}",
            f"https://creativecommons.org/licenses/{family}/{version}/",
            f"cc-{family}-{version}",
        )
    return None


def _normalized_license_name(value: str) -> str:
    return " ".join(value.casefold().replace("-", " ").split())


def _matched_page(payload: object, title: str, asset: bytes) -> tuple[dict, dict] | None:
    query = payload.get("query") if isinstance(payload, dict) else None
    pages = query.get("pages") if isinstance(query, dict) else None
    if not isinstance(pages, list) or len(pages) != 1 or not isinstance(page := pages[0], dict):
        return None
    if page.get("ns") != 6 or "missing" in page or _file_title(page.get("title")) != title:
        return None
    images = page.get("imageinfo")
    if not isinstance(images, list) or len(images) != 1 or not isinstance(info := images[0], dict):
        return None
    if (
        "filehidden" in info
        or _file_title(info.get("canonicaltitle")) != title
        or _source_title(info.get("descriptionurl")) != title
        or _asset_path(info.get("url")) != asset
    ):
        return None
    return page, info


def _with_warning(result: ImageSearchResult, warning: str) -> ImageSearchResult:
    return result.model_copy(update={"warnings": list(dict.fromkeys([*result.warnings, warning]))})


def _apply_attribution(result: ImageSearchResult, page: dict, info: dict) -> ImageSearchResult:
    image = result.image
    metadata = info.get("extmetadata")
    if not isinstance(metadata, dict):
        metadata = {}
    revision = page.get("lastrevid")
    evidence = (
        f"https://{_COMMONS}/w/index.php?oldid={revision}"
        if type(revision) is int and revision > 0
        else info["descriptionurl"]
    )
    checked_at = datetime.now(UTC)
    update = {
        "source_page_verified": True,
        "source_page_status": "checked",
        "source_checked_at": checked_at,
        "source_site_name": "Wikimedia Commons",
        "source_site_domain": _COMMONS,
        "creator": _plain_text(_metadata(metadata, "Artist"), 512) or image.creator,
        "attribution_text": _plain_text(_metadata(metadata, "Attribution"), 1024)
        or image.attribution_text,
        "rights_checked_at": checked_at,
        "rights_evidence_url": evidence,
    }
    asset_hash = info.get("sha1")
    if isinstance(asset_hash, str) and _ASSET_SHA1.fullmatch(asset_hash):
        update.update(
            asset_hash=asset_hash.lower(),
            asset_hash_algorithm="sha1",
            asset_hash_source="wikimedia_commons",
        )
    warning = None
    declared = _license(_metadata(metadata, "LicenseUrl"))
    if declared is not None:
        name, url, template = declared
        short_name = _plain_text(_metadata(metadata, "LicenseShortName"), 100)
        valid_names = {_normalized_license_name(name)}
        valid_templates = {template}
        if template == "cc-zero":
            valid_names.add("cc0")
            valid_templates.update(("cc-zero-1.0", "cc0", "cc0-1.0"))
        actual_template = _metadata(metadata, "License", "commons-templates")
        prior = _license(image.license_url)
        nonfree = str(_metadata(metadata, "NonFree")).casefold() in {"true", "1"}
        if (
            image.rights_status == "conflict"
            or (short_name is not None and _normalized_license_name(short_name) not in valid_names)
            or (isinstance(actual_template, str) and actual_template not in valid_templates)
            or (prior is not None and prior[1] != url)
            or nonfree
        ):
            update.update(rights_status="conflict", license_name=None, license_url=None)
            warning = "image_license_conflict"
        elif (
            short_name is None
            or not isinstance(actual_template, str)
            or _metadata(metadata, "Restrictions")
            or _metadata(metadata, "DeletionReason")
        ):
            update.update(rights_status="declared_unverified", license_name=name, license_url=url)
            warning = "image_license_unverified"
        else:
            update.update(rights_status="verified_license", license_name=name, license_url=url)

    attribution = image.model_copy(update=update)
    warnings = [
        value
        for value in result.warnings
        if value != "image_rights_unknown" or attribution.rights_status == "unknown"
    ]
    response = result.model_copy(update={"image": attribution, "warnings": warnings})
    return _with_warning(response, warning) if warning else response


class ImageAttributionResolver:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        user_agent: str = "MultiSearchBot/0.1 (self-hosted Commons attribution lookup)",
    ):
        self._client = client
        self._user_agent = user_agent
        self._lock = asyncio.Lock()

    @staticmethod
    def supports(result: ImageSearchResult) -> bool:
        image = result.image
        return (
            _source_title(image.source_page_url) is not None
            and _asset_path(image.original_url) is not None
        )

    async def resolve(self, result: ImageSearchResult, timeout_s: float) -> ImageSearchResult:
        image = result.image
        title = _source_title(image.source_page_url)
        asset = _asset_path(image.original_url)
        if title is None or asset is None or timeout_s <= 0:
            return result
        try:
            # Wikimedia 建议串行请求；等待锁也计入请求预算。
            async with asyncio.timeout(timeout_s), self._lock:
                payload = await self._fetch(title, timeout_s)
        except (TimeoutError, httpx.HTTPError, RecursionError, ValueError):
            return _with_warning(result, "image_source_verification_failed")
        if payload is None:
            return _with_warning(result, "image_source_verification_failed")
        matched = _matched_page(payload, title, asset)
        return (
            _apply_attribution(result, *matched)
            if matched
            else _with_warning(result, "image_source_not_matched")
        )

    async def _fetch(self, title: str, timeout_s: float) -> object:
        async with self._client.stream(
            "GET",
            _API,
            params={
                "action": "query",
                "prop": "info|imageinfo",
                "iiprop": "url|canonicaltitle|extmetadata|sha1",
                "iiextmetadatafilter": _METADATA,
                "iilimit": "1",
                "titles": title,
                "format": "json",
                "formatversion": "2",
            },
            headers={
                "User-Agent": self._user_agent,
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
            timeout=timeout_s,
            follow_redirects=False,
        ) as response:
            if (
                response.status_code != 200
                or response.headers.get("content-encoding", "identity").lower() != "identity"
                or response.headers.get("content-type", "").split(";", 1)[0].lower()
                != "application/json"
            ):
                return None
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                    return None
                body.extend(chunk)
            return json.loads(body)
