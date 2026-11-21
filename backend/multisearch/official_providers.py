import re
from urllib.parse import urlsplit

import httpx

from .contracts import SearchRequest, Source
from .pipeline import _matches_domain, _publication_date, _text, public_link
from .providers import Provider, ProviderFailure
from .vertical_providers import _Batch, _dict, _json, _list, _Record, _rows, _VerticalProvider


class _OfficialProvider(_VerticalProvider):
    source = Source.WEB
    official_search = True
    spacing_seconds = 1
    result_domain: str
    topic: re.Pattern
    region: str | None = None

    def supports_request(self, request: SearchRequest) -> bool:
        if (
            request.network_scope != "public"
            or request.time_range
            and self.date_range == "unsupported"
        ):
            return False
        if request.language and request.language.casefold().split("-")[0] != "en":
            return False
        if self.region and request.region and request.region.casefold() != self.region:
            return False
        url = f"https://{self.result_domain}/"
        if _matches_domain(url, request.domain_denylist):
            return False
        if request.domain_allowlist:
            return _matches_domain(url, request.domain_allowlist)
        sites = re.findall(r"(?<!\S)site:([\w.-]+)", request.query, re.IGNORECASE)
        if sites:
            return _matches_domain(url, [site.casefold() for site in sites])
        return bool(self.topic.search(request.query))

    def terms(self, request: SearchRequest) -> str:
        query = re.sub(r"(?<!\S)site:[\w.-]+", "", request.query, flags=re.IGNORECASE)
        return " ".join(query.split())[:256]

    def _target(self, request, limit, identifier):
        return super()._target(request, min(3, limit), identifier)


class FederalRegisterProvider(_OfficialProvider):
    provider_id = "federal-register"
    endpoint = "https://www.federalregister.gov/api/v1/documents.json"
    result_domain = "www.federalregister.gov"
    region = "us"
    topic = re.compile(
        r"\b(?:federal register|faa|rulemaking|airworthiness directives?|us regulations?)\b|美国联邦公报|适航指令",
        re.I,
    )

    def _params(self, request, limit):
        params = {"per_page": limit, "conditions[term]": self.terms(request)}
        if request.time_range:
            params["conditions[publication_date][gte]"] = str(request.time_range.start)
            params["conditions[publication_date][lte]"] = str(request.time_range.end)
        return params

    def _parse(self, body, *, exact=False):
        return _Batch(
            [
                _Record(
                    title=_text(row.get("title"), 512),
                    url=url
                    if (url := public_link(row.get("html_url")))
                    and urlsplit(url).hostname == self.result_domain
                    else None,
                    published_at=_publication_date(row.get("publication_date")),
                    snippet=_text(row.get("abstract"), 1200) or _text(row.get("excerpts"), 1200),
                )
                for item in _rows(_json(body).get("results"))
                if (row := _dict(item))
            ]
        )


class _CkanProvider(_OfficialProvider):
    date_field = "created_at"
    dataset_filter = ""

    def _params(self, request, limit):
        params = {"rows": limit, "q": self.terms(request)}
        filters = [self.dataset_filter] if self.dataset_filter else []
        if request.time_range:
            start, end = request.time_range.start, request.time_range.end
            filters.append(f"metadata_created:[{start}T00:00:00Z TO {end}T23:59:59.999Z]")
        if filters:
            params["fq"] = " AND ".join(filters)
        return params

    def _record_url(self, identity: str, row: dict) -> str | None:
        raise NotImplementedError

    def _parse(self, body, *, exact=False):
        data = _json(body)
        if data.get("success") is not True:
            raise ProviderFailure("failed")
        rows = []
        for item in _rows(_dict(data.get("result")).get("results")):
            row = _dict(item)
            identity = str(row.get("id", ""))
            if row.get("private") is not False or row.get("state") != "active":
                continue
            if not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", identity):
                continue
            rows.append(
                _Record(
                    title=_text(row.get("title"), 512),
                    url=self._record_url(identity, row),
                    published_at=_publication_date(row.get("metadata_created")),
                    snippet=_text(row.get("notes"), 1200),
                )
            )
        return _Batch(rows)


class UKOpenDataProvider(_CkanProvider):
    provider_id = "uk-open-data"
    endpoint = "https://ckan.publishing.service.gov.uk/api/action/package_search"
    result_domain = "www.data.gov.uk"
    region = "gb"
    topic = re.compile(
        r"\b(?:data\.gov\.uk|(?:uk|united kingdom|british) .{0,35}(?:data|statistics))\b|英国.{0,12}(?:数据|统计)",
        re.I,
    )

    def _record_url(self, identity: str, row: dict) -> str | None:
        slug = str(row.get("name", ""))
        if re.fullmatch(r"[a-z0-9_-]{1,160}", slug):
            return f"https://{self.result_domain}/dataset/{identity}/{slug}"
        return None


class CanadaOpenDataProvider(_CkanProvider):
    provider_id = "canada-open-data"
    endpoint = "https://open.canada.ca/data/en/api/3/action/package_search"
    result_domain = "open.canada.ca"
    region = "ca"
    dataset_filter = "portal_type:dataset"
    topic = re.compile(
        r"\b(?:open\.canada\.ca|(?:canada|canadian) .{0,35}(?:data|statistics))\b|加拿大.{0,12}(?:数据|统计)",
        re.I,
    )

    def _record_url(self, identity: str, row: dict) -> str:
        return f"https://{self.result_domain}/data/en/dataset/{identity}"


class EarthdataProvider(_OfficialProvider):
    provider_id = "nasa-earthdata"
    endpoint = "https://cmr.earthdata.nasa.gov/search/collections.json"
    result_domain = "search.earthdata.nasa.gov"
    date_range = "unsupported"
    topic = re.compile(
        r"\b(?:earthdata|earth observation|remote sensing|satellite data|atmospheric datasets?)\b|地球观测|遥感数据|卫星数据",
        re.I,
    )

    def _params(self, request, limit):
        return {"page_size": limit, "keyword": self.terms(request)}

    def _parse(self, body, *, exact=False):
        rows = []
        for item in _rows(_dict(_json(body).get("feed")).get("entry")):
            row = _dict(item)
            identity = str(row.get("id", ""))
            if not re.fullmatch(r"C[0-9]+-[A-Za-z0-9_-]{1,64}", identity):
                continue
            rows.append(
                _Record(
                    title=_text(row.get("title"), 512),
                    url=f"https://{self.result_domain}/search/granules?p={identity}",
                    snippet=_text(row.get("summary"), 1200),
                )
            )
        return _Batch(rows)


class USGSProvider(_OfficialProvider):
    provider_id = "usgs-publications"
    endpoint = "https://pubs.usgs.gov/pubs-services/publication/"
    result_domain = "pubs.usgs.gov"
    cache_version = "2"
    date_range = "metadata_postfilter"
    topic = re.compile(
        r"\b(?:usgs|geologic(?:al)? survey|groundwater|landslide|seismic hazards?)\b|美国地质调查局|地下水|滑坡",
        re.I,
    )

    def _params(self, request, limit):
        return {"q": self.terms(request), "page_size": limit}

    def _report_url(self, identity, row):
        for item in _list(row.get("links"))[:30]:
            link = _dict(item)
            if _dict(link.get("type")).get("id") != 39:
                continue
            url = public_link(link.get("url"))
            if url:
                target = urlsplit(url)
                if target.scheme == "https" and target.hostname == self.result_domain:
                    return url
        return f"https://{self.result_domain}/publication/{identity}"

    def _parse(self, body, *, exact=False):
        rows = []
        for item in _rows(_json(body).get("records")):
            row = _dict(item)
            identity = str(row.get("indexId", ""))
            if not re.fullmatch(r"[a-zA-Z0-9._-]{1,96}", identity) or identity in {".", ".."}:
                continue
            rows.append(
                _Record(
                    title=_text(row.get("title"), 512),
                    url=self._report_url(identity, row),
                    published_at=_publication_date(row.get("publicationDate")),
                    snippet=_text(row.get("docAbstract"), 1200),
                )
            )
        return _Batch(rows)


def create_official_providers(
    client: httpx.AsyncClient, enabled_sources: frozenset[str]
) -> list[Provider]:
    return [
        provider(client)
        for provider in (
            FederalRegisterProvider,
            UKOpenDataProvider,
            CanadaOpenDataProvider,
            EarthdataProvider,
            USGSProvider,
        )
        if provider.provider_id in enabled_sources
    ]
