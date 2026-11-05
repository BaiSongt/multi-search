import asyncio
import json
import re
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic, time
from typing import Literal
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree

import httpx

from .contracts import Modality, SearchRequest, Source, TextSearchResult
from .pipeline import _id, _matches_domain, _publication_date, _text, canonical_url, public_link
from .providers import (
    Provider,
    ProviderCapabilities,
    ProviderFailure,
    ProviderFilters,
    ProviderResult,
    _retry_after,
)

_MAX_UPSTREAM_BYTES = 2 * 1024 * 1024
_MAX_RESULTS = 30
_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV = "{http://arxiv.org/schemas/atom}"
_ARXIV_ID = re.compile(
    r"(\d{4}\.\d{4,5}|[A-Za-z][A-Za-z0-9.-]{0,40}/\d{7})(v[1-9]\d{0,5})?",
    flags=re.ASCII,
)
_DOI = re.compile(r"10\.[0-9]{4,9}/[^\s\x00-\x1f\x7f]+", flags=re.IGNORECASE)
_ARXIV_HOSTS = {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}
AcademicIdentifierKind = Literal["doi", "arxiv"]


@dataclass(frozen=True)
class AcademicIdentifier:
    kind: AcademicIdentifierKind
    value: str
    version: str | None = None


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list:
    return value if isinstance(value, list) else []


def _json(body: bytes) -> dict:
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        raise ProviderFailure("failed") from None
    if not isinstance(data, dict):
        raise ProviderFailure("failed")
    return data


def _rows(value: object) -> list:
    if not isinstance(value, list):
        raise ProviderFailure("failed")
    return value[:_MAX_RESULTS]


def _doi(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 1024:
        return None
    doi = _text(value, 1024)
    if doi and (url := public_link(doi)):
        parts = urlsplit(url)
        if parts.hostname not in {"doi.org", "dx.doi.org"}:
            return None
        doi = unquote(parts.path[1:])
    return doi.casefold() if doi and _DOI.fullmatch(doi) else None


def _doi_url(doi: str | None) -> str | None:
    return f"https://doi.org/{quote(doi, safe='/')}" if doi else None


def academic_identifier(query: str) -> AcademicIdentifier | None:
    candidate = query.strip()
    prefix, separator, suffix = candidate.partition(":")
    if separator and prefix.casefold() in {"doi", "arxiv"}:
        candidate = suffix.strip()
    else:
        prefix = ""
    if prefix.casefold() != "arxiv" and (doi := _doi(candidate)):
        # DOI 中的点段和反斜线不能改变固定的上游 API 路径。
        path = unquote(doi)
        if not {".", ".."}.intersection(path.split("/")) and "\\" not in path:
            return AcademicIdentifier("doi", doi)
        return None
    if prefix.casefold() == "doi":
        return None
    if url := public_link(candidate):
        parts = urlsplit(url)
        path = unquote(parts.path)
        if parts.hostname not in _ARXIV_HOSTS or parts.port is not None:
            return None
        if path.startswith("/abs/"):
            candidate = path[5:]
        elif path.startswith("/pdf/"):
            candidate = path[5:].removesuffix(".pdf")
        else:
            return None
    if match := _ARXIV_ID.fullmatch(candidate.casefold()):
        return AcademicIdentifier("arxiv", *match.groups())
    return None


def _first_text(values: object, limit: int) -> str | None:
    return next((name for item in _list(values) if (name := _text(item, limit))), None)


def _query_phrase(query: str) -> str:
    return " ".join(query.replace('"', " ").replace("\\", " ").split())


def _crossref_date(record: dict) -> datetime | None:
    for date_field in ("published", "published-online", "published-print", "issued"):
        dates = _list(_dict(record.get(date_field)).get("date-parts"))
        if not dates or not isinstance(dates[0], list) or len(dates[0]) != 3:
            continue
        if all(type(value) is int for value in dates[0]):
            try:
                return datetime(*dates[0], tzinfo=UTC)
            except (ValueError, OverflowError):
                continue
    return None


@dataclass
class _Record:
    title: str | None
    url: str | None
    published_at: datetime | None = None
    authors: list[str] = field(default_factory=list)
    doi: str | None = None
    journal: str | None = None
    arxiv_id: str | None = None
    version: str | None = None
    language: str | None = None
    license_name: str | None = None
    license_url: str | None = None
    repository_branch: str | None = None
    snippet: str | None = None


@dataclass
class _Batch:
    rows: list[_Record]
    incomplete: bool = False


class _VerticalProvider:
    source = Source.ACADEMIC
    accept = "application/json"
    spacing_seconds = 0.0
    cache_version = "1"
    identifier_kind: AcademicIdentifierKind | None = None
    date_field: Literal["published_at", "submitted_at", "created_at"] = "published_at"
    requires_api_key = False
    provider_id: str
    endpoint: str

    def __init__(self, client: httpx.AsyncClient, token: str = ""):
        self.client = client
        self.pairs = frozenset({(self.source, Modality.TEXT)})
        self.capabilities = ProviderCapabilities(
            {
                Modality.TEXT: ProviderFilters(
                    date_range="upstream_and_metadata_postfilter",
                    date_field=self.date_field,
                    exact_identifier_date_range=(
                        "metadata_postfilter" if self.identifier_kind else None
                    ),
                )
            },
            requires_api_key=self.requires_api_key,
            upstream_concurrency=1 if self.spacing_seconds else None,
        )
        self.headers = {
            "Accept": self.accept,
            "Accept-Encoding": "identity",
            "User-Agent": "MultiSearch/metadata",
        }
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        self._connection = asyncio.Lock()
        self._next_request = 0.0

    async def _download(
        self, endpoint: str, params: dict[str, str | int], timeout_s: float, *, missing_ok: bool
    ) -> bytes | None:
        async with self.client.stream(
            "GET",
            endpoint,
            params=params,
            headers=self.headers,
            follow_redirects=False,
            timeout=timeout_s,
        ) as response:
            if response.status_code == 404 and missing_ok:
                return None
            retry_after = _retry_after(response.headers.get("retry-after"))
            github_limit = self.provider_id == "github" and (
                retry_after is not None or response.headers.get("x-ratelimit-remaining") == "0"
            )
            if response.status_code == 429 or (response.status_code == 403 and github_limit):
                if self.provider_id == "github" and retry_after is None:
                    reset = response.headers.get("x-ratelimit-reset", "")
                    if len(reset) <= 10 and reset.isascii() and reset.isdecimal():
                        retry_after = max(1.0, int(reset) - time())
                    else:
                        retry_after = 60.0
                raise ProviderFailure("rate_limited", retry_after)
            if response.status_code in {408, 504}:
                raise ProviderFailure("timeout")
            check_secondary_limit = self.provider_id == "github" and response.status_code == 403
            if (response.status_code != 200 and not check_secondary_limit) or response.headers.get(
                "content-encoding", "identity"
            ).lower() != "identity":
                raise ProviderFailure("failed")
            maximum = 4096 if check_secondary_limit else _MAX_UPSTREAM_BYTES
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > maximum:
                    raise ProviderFailure("failed")
                body.extend(chunk)
            if check_secondary_limit:
                if b"secondary rate limit" in body.lower():
                    raise ProviderFailure("rate_limited", 60.0)
                raise ProviderFailure("failed")
            return bytes(body)

    async def _request(
        self, endpoint: str, params: dict[str, str | int], timeout_s: float, *, missing_ok: bool
    ) -> bytes | None:
        if timeout_s <= 0:
            raise ProviderFailure("timeout")
        try:
            async with asyncio.timeout(timeout_s):
                if self.spacing_seconds == 0:
                    return await self._download(endpoint, params, timeout_s, missing_ok=missing_ok)
                # 需要限速的来源同时使用单连接，排队时间计入调用预算。
                async with self._connection:
                    await asyncio.sleep(max(0.0, self._next_request - monotonic()))
                    self._next_request = monotonic() + self.spacing_seconds
                    return await self._download(endpoint, params, timeout_s, missing_ok=missing_ok)
        except (TimeoutError, httpx.TimeoutException):
            raise ProviderFailure("timeout") from None
        except (httpx.HTTPError, ValueError):
            raise ProviderFailure("failed") from None

    def _params(self, request: SearchRequest, limit: int) -> dict[str, str | int]:
        raise NotImplementedError

    def _target(
        self, request: SearchRequest, limit: int, identifier: AcademicIdentifier | None
    ) -> tuple[str, dict[str, str | int]]:
        return self.endpoint, self._params(request, limit)

    def _parse(self, body: bytes, *, exact: bool = False) -> _Batch:
        raise NotImplementedError

    async def search(
        self,
        request: SearchRequest,
        source: Source,
        modality: Modality,
        limit: int,
        timeout_s: float,
    ) -> ProviderResult:
        if (
            request.network_scope != self.capabilities.network_scope
            or (source, modality) not in self.pairs
        ):
            raise ProviderFailure("failed")
        limit = max(1, min(_MAX_RESULTS, limit))
        identifier = academic_identifier(request.query) if source == Source.ACADEMIC else None
        if identifier and self.identifier_kind != identifier.kind:
            return ProviderResult([])
        endpoint, params = self._target(request, limit, identifier)
        body = await self._request(endpoint, params, timeout_s, missing_ok=identifier is not None)
        batch = self._parse(body, exact=identifier is not None) if body is not None else _Batch([])
        warnings = []
        results = []
        invalid = missing_dates = 0
        retrieved_at = datetime.now(UTC)
        for row in batch.rows:
            if identifier and (
                (identifier.kind == "doi" and row.doi != identifier.value)
                or (
                    identifier.kind == "arxiv"
                    and (
                        (row.arxiv_id or "").casefold() != identifier.value
                        or (identifier.version is not None and row.version != identifier.version)
                    )
                )
            ):
                raise ProviderFailure("failed")
            if not row.url or not row.title:
                invalid += 1
                continue
            if _matches_domain(row.url, request.domain_denylist) or (
                request.domain_allowlist and not _matches_domain(row.url, request.domain_allowlist)
            ):
                continue
            if request.time_range:
                if row.published_at is None:
                    missing_dates += 1
                    continue
                if (
                    not request.time_range.start
                    <= row.published_at.date()
                    <= request.time_range.end
                ):
                    continue
            canonical = canonical_url(row.url)
            results.append(
                TextSearchResult(
                    result_id=_id(source, row.doi or canonical),
                    title=row.title,
                    url=row.url,
                    canonical_url=canonical,
                    source_type=source,
                    provider_ids=[self.provider_id],
                    retrieved_at=retrieved_at,
                    published_at=row.published_at,
                    authors=row.authors,
                    doi=row.doi,
                    journal=row.journal,
                    arxiv_id=row.arxiv_id,
                    version=row.version,
                    language=row.language,
                    license_name=row.license_name,
                    license_url=row.license_url,
                    repository_branch=row.repository_branch,
                    snippet=row.snippet,
                )
            )
        if invalid:
            warnings.append(f"{self.provider_id}:invalid_metadata:{invalid}")
        if missing_dates:
            warnings.append(f"{self.provider_id}:publication_date_missing:{missing_dates}")
        if request.language or request.region:
            warnings.append(f"{self.provider_id}:locale_unsupported")
        if batch.incomplete:
            warnings.append(f"{self.provider_id}:upstream_incomplete")
        if invalid and invalid == len(batch.rows):
            raise ProviderFailure("failed")
        return ProviderResult(results[:limit], warnings, partial=batch.incomplete or bool(invalid))


class OpenAlexProvider(_VerticalProvider):
    provider_id = "openalex"
    endpoint = "https://api.openalex.org/works"
    cache_version = "2"
    identifier_kind = "doi"
    fields = "id,doi,display_name,authorships,publication_date,language,primary_location"

    def _target(self, request, limit, identifier):
        if identifier:
            return f"{self.endpoint}/doi:{quote(identifier.value, safe='/')}", {
                "select": self.fields
            }
        return super()._target(request, limit, identifier)

    def _params(self, request: SearchRequest, limit: int) -> dict[str, str | int]:
        params: dict[str, str | int] = {
            "search": request.query,
            "per_page": limit,
            "select": self.fields,
        }
        if window := request.time_range:
            params["filter"] = (
                f"from_publication_date:{window.start},to_publication_date:{window.end}"
            )
        return params

    def _parse(self, body: bytes, *, exact: bool = False) -> _Batch:
        data = _json(body)
        documents = []
        for value in [data] if exact else _rows(data.get("results")):
            row = _dict(value)
            location = _dict(row.get("primary_location"))
            source = _dict(location.get("source"))
            doi = _doi(row.get("doi"))
            authors = []
            for entry in _list(row.get("authorships"))[:100]:
                author = _dict(_dict(entry).get("author"))
                if name := _text(author.get("display_name"), 256):
                    authors.append(name)
            documents.append(
                _Record(
                    title=_text(row.get("display_name"), 512),
                    url=public_link(location.get("landing_page_url"))
                    or _doi_url(doi)
                    or public_link(row.get("id")),
                    published_at=_publication_date(row.get("publication_date")),
                    authors=authors,
                    doi=doi,
                    journal=_text(source.get("display_name"), 512)
                    if source.get("type") == "journal"
                    else None,
                    version=_text(location.get("version"), 64),
                    language=_text(row.get("language"), 24),
                    license_name=_text(location.get("license"), 80),
                    license_url=public_link(location.get("license_id")),
                )
            )
        return _Batch(documents)


class CrossrefProvider(_VerticalProvider):
    provider_id = "crossref"
    endpoint = "https://api.crossref.org/works"
    spacing_seconds = 0.2
    cache_version = "2"
    identifier_kind = "doi"

    def __init__(self, client: httpx.AsyncClient, email: str = ""):
        super().__init__(client)
        if email and (len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)):
            raise ValueError("Crossref 联系邮箱格式无效")
        self.email = email

    def _target(self, request, limit, identifier):
        if identifier:
            params = {"mailto": self.email} if self.email else {}
            return f"{self.endpoint}/{quote(identifier.value, safe='/')}", params
        return super()._target(request, limit, identifier)

    def _params(self, request: SearchRequest, limit: int) -> dict[str, str | int]:
        params: dict[str, str | int] = {
            "query.bibliographic": request.query,
            "rows": limit,
            "select": "DOI,title,author,container-title,published,published-online,published-print,issued,URL,license",
        }
        if self.email:
            params["mailto"] = self.email
        if window := request.time_range:
            params["filter"] = f"from-pub-date:{window.start},until-pub-date:{window.end}"
        return params

    def _parse(self, body: bytes, *, exact: bool = False) -> _Batch:
        data = _json(body)
        if data.get("status") != "ok":
            raise ProviderFailure("failed")
        message = _dict(data.get("message"))
        documents = []
        for value in [message] if exact else _rows(message.get("items")):
            row = _dict(value)
            doi = _doi(row.get("DOI"))
            authors = []
            for entry in _list(row.get("author"))[:100]:
                author = _dict(entry)
                names = [_text(author.get(field), 128) for field in ("given", "family")]
                if name := " ".join(part for part in names if part) or _text(
                    author.get("name"), 256
                ):
                    authors.append(name)
            license_urls = {
                url
                for entry in _list(row.get("license"))
                if (url := public_link(_dict(entry).get("URL")))
            }
            documents.append(
                _Record(
                    title=_first_text(row.get("title"), 512),
                    url=public_link(row.get("URL")) or _doi_url(doi),
                    published_at=_crossref_date(row),
                    authors=authors,
                    doi=doi,
                    journal=_first_text(row.get("container-title"), 512),
                    license_url=next(iter(license_urls)) if len(license_urls) == 1 else None,
                )
            )
        return _Batch(documents)


class ArxivProvider(_VerticalProvider):
    provider_id = "arxiv"
    endpoint = "https://export.arxiv.org/api/query"
    accept = "application/atom+xml"
    spacing_seconds = 3.0
    cache_version = "2"
    identifier_kind = "arxiv"
    date_field = "submitted_at"

    def _target(self, request, limit, identifier):
        if identifier:
            return self.endpoint, {
                "id_list": identifier.value + (identifier.version or ""),
                "max_results": 1,
            }
        return super()._target(request, limit, identifier)

    def _params(self, request: SearchRequest, limit: int) -> dict[str, str | int]:
        phrase = _query_phrase(request.query)
        if not phrase:
            raise ProviderFailure("failed")
        search = f'all:"{phrase}"'
        if window := request.time_range:
            start = window.start.isoformat().replace("-", "")
            end = window.end.isoformat().replace("-", "")
            search += f" AND submittedDate:[{start}0000 TO {end}2359]"
        return {"search_query": search, "start": 0, "max_results": limit}

    def _parse(self, body: bytes, *, exact: bool = False) -> _Batch:
        try:
            xml = body.decode("utf-8-sig")
            if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", xml, flags=re.IGNORECASE):
                raise ProviderFailure("failed")
            feed = ElementTree.fromstring(xml)
        except (UnicodeError, ElementTree.ParseError, RecursionError, ValueError):
            raise ProviderFailure("failed") from None
        if feed.tag != f"{_ATOM}feed":
            raise ProviderFailure("failed")
        documents = []
        for entry in feed.findall(f"{_ATOM}entry")[:_MAX_RESULTS]:
            identifier = public_link(entry.findtext(f"{_ATOM}id"))
            parts = urlsplit(identifier or "")
            from_arxiv = parts.hostname in _ARXIV_HOSTS
            if from_arxiv and parts.path == "/api/errors":
                raise ProviderFailure("failed")
            match = (
                _ARXIV_ID.fullmatch(parts.path[5:])
                if from_arxiv and parts.path.startswith("/abs/")
                else None
            )
            arxiv_id, version = match.groups() if match else (None, None)
            authors = [
                name
                for author in entry.findall(f"{_ATOM}author")[:100]
                if (name := _text(author.findtext(f"{_ATOM}name"), 256))
            ]
            documents.append(
                _Record(
                    title=_text(entry.findtext(f"{_ATOM}title"), 512),
                    url=f"https://arxiv.org/abs/{arxiv_id}{version or ''}" if arxiv_id else None,
                    published_at=_publication_date(entry.findtext(f"{_ATOM}published")),
                    authors=authors,
                    doi=_doi(entry.findtext(f"{_ARXIV}doi")),
                    journal=_text(entry.findtext(f"{_ARXIV}journal_ref"), 512),
                    arxiv_id=arxiv_id,
                    version=version,
                    snippet=_text(entry.findtext(f"{_ATOM}summary"), 2048),
                )
            )
        return _Batch(documents)


class GitHubProvider(_VerticalProvider):
    source = Source.CODE
    provider_id = "github"
    endpoint = "https://api.github.com/search/repositories"
    accept = "application/vnd.github+json"
    spacing_seconds = 2.0
    date_field = "created_at"
    requires_api_key = True

    def __init__(self, client: httpx.AsyncClient, token: str):
        if not token:
            raise ValueError("启用 GitHub 检索需要令牌")
        super().__init__(client, token)
        self.headers["X-GitHub-Api-Version"] = "2026-03-10"

    def _params(self, request: SearchRequest, limit: int) -> dict[str, str | int]:
        # 将用户输入限定为搜索短语，避免令牌被用来探测私有仓库的限定词。
        phrase = _query_phrase(request.query)
        if not phrase or len(phrase) > 256:
            raise ProviderFailure("failed")
        query = f'"{phrase}" is:public'
        if window := request.time_range:
            query += f" created:{window.start}..{window.end}"
        return {"q": query, "per_page": limit}

    def _parse(self, body: bytes, *, exact: bool = False) -> _Batch:
        data = _json(body)
        documents = []
        for value in _rows(data.get("items")):
            row = _dict(value)
            # 令牌可访问的私有或内部仓库永不进入公共结果。
            if row.get("private") is not False or row.get("visibility") not in (None, "public"):
                continue
            url = public_link(row.get("html_url"))
            if url and urlsplit(url).hostname != "github.com":
                url = None
            license_info = _dict(row.get("license"))
            spdx_id = _text(license_info.get("spdx_id"), 80)
            if spdx_id in {None, "NOASSERTION", "NONE"}:
                license_info = {}
                spdx_id = None
            documents.append(
                _Record(
                    title=_text(row.get("full_name"), 512),
                    url=url,
                    published_at=_publication_date(row.get("created_at")),
                    license_name=spdx_id,
                    license_url=public_link(license_info.get("html_url"))
                    or public_link(license_info.get("url")),
                    repository_branch=_text(row.get("default_branch"), 256),
                    snippet=_text(row.get("description"), 2048),
                )
            )
        return _Batch(documents, incomplete=data.get("incomplete_results") is True)


def create_vertical_providers(
    client: httpx.AsyncClient,
    *,
    enabled_sources: Collection[str],
    openalex_key: str = "",
    github_token: str = "",
    crossref_email: str = "",
) -> list[Provider]:
    providers: list[Provider] = []
    if "openalex" in enabled_sources:
        providers.append(OpenAlexProvider(client, openalex_key))
    if "crossref" in enabled_sources:
        providers.append(CrossrefProvider(client, crossref_email))
    if "arxiv" in enabled_sources:
        providers.append(ArxivProvider(client))
    if "github" in enabled_sources and github_token:
        providers.append(GitHubProvider(client, github_token))
    return providers
