import re
from urllib.parse import urlsplit

import httpx

from .contracts import SearchRequest, Source
from .pipeline import _matches_domain, _publication_date, _text, public_link
from .providers import Provider, ProviderFailure
from .vertical_providers import _Batch, _dict, _json, _Record, _rows, _VerticalProvider


class _OfficialProvider(_VerticalProvider):
    source = Source.WEB
    official_search = True
    spacing_seconds = 1
    result_domain: str
    topic: re.Pattern
    region: str | None = None

    def supports_request(self, request: SearchRequest) -> bool:
        if request.network_scope != "public" or request.time_range and not self.supports_dates:
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


class UKOpenDataProvider(_OfficialProvider):
    provider_id = "uk-open-data"
    endpoint = "https://www.data.gov.uk/api/3/action/package_search"
    result_domain = "www.data.gov.uk"
    region = "gb"
    date_field = "created_at"
    topic = re.compile(
        r"\b(?:data\.gov\.uk|(?:uk|united kingdom|british) .{0,35}(?:data|statistics))\b|英国.{0,12}(?:数据|统计)",
        re.I,
    )

    def _params(self, request, limit):
        params = {"rows": limit, "q": self.terms(request)}
        if request.time_range:
            start, end = request.time_range.start, request.time_range.end
            params["fq"] = f"metadata_created:[{start}T00:00:00Z TO {end}T23:59:59.999Z]"
        return params

    def _parse(self, body, *, exact=False):
        data = _json(body)
        if data.get("success") is not True:
            raise ProviderFailure("failed")
        rows = []
        for item in _rows(_dict(data.get("result")).get("results")):
            row = _dict(item)
            identity, slug = str(row.get("id", "")), str(row.get("name", ""))
            if row.get("private") is not False or row.get("state") != "active":
                continue
            if not re.fullmatch(r"[a-f0-9-]{36}", identity) or not re.fullmatch(
                r"[a-z0-9_-]{1,160}", slug
            ):
                continue
            rows.append(
                _Record(
                    title=_text(row.get("title"), 512),
                    url=f"https://{self.result_domain}/dataset/{identity}/{slug}",
                    published_at=_publication_date(row.get("metadata_created")),
                    snippet=_text(row.get("notes"), 1200),
                )
            )
        return _Batch(rows)


class EarthdataProvider(_OfficialProvider):
    provider_id = "nasa-earthdata"
    endpoint = "https://cmr.earthdata.nasa.gov/search/collections.json"
    result_domain = "search.earthdata.nasa.gov"
    supports_dates = False
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


def create_official_providers(
    client: httpx.AsyncClient, enabled_sources: frozenset[str]
) -> list[Provider]:
    return [
        provider(client)
        for provider in (FederalRegisterProvider, UKOpenDataProvider, EarthdataProvider)
        if provider.provider_id in enabled_sources
    ]
