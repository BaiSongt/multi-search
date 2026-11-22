import json
import re
from urllib.parse import urlsplit

import httpx

from .contracts import SearchRequest, Source
from .pipeline import _matches_domain, _plain_excerpt, _publication_date, _text, public_link
from .providers import Provider, ProviderFailure
from .vertical_providers import _Batch, _dict, _doi, _json, _list, _Record, _rows, _VerticalProvider


class _OfficialProvider(_VerticalProvider):
    source = Source.WEB
    official_search = True
    spacing_seconds = 1
    result_domain: str
    topic: re.Pattern
    region: str | None = None
    query_languages = frozenset({"en"})

    def supports_request(self, request: SearchRequest) -> bool:
        if (
            request.network_scope != "public"
            or request.time_range
            and self.date_range == "unsupported"
        ):
            return False
        if (
            request.language
            and request.language.casefold().split("-")[0] not in self.query_languages
        ):
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


class AAIBReportsProvider(_OfficialProvider):
    provider_id = "uk-aaib"
    endpoint = "https://www.gov.uk/api/search.json"
    result_domain = "www.gov.uk"
    region = "gb"
    date_range = "unsupported"
    _document_type = "aaib_report"
    _authority = re.compile(
        r"\b(?:aaib|air accidents? investigation branch)\b|英国航空事故调查局", re.I
    )
    topic = re.compile(
        _authority.pattern
        + r"|\b(?:runway excursions?|runway incursions?|"
        + r"(?:british|uk) (?:air|aircraft|aviation) accident investigations?)\b"
        + r"|跑道(?:偏出|冲出|侵入)",
        re.I,
    )

    def terms(self, request: SearchRequest) -> str:
        return " ".join(self._authority.sub(" ", super().terms(request)).split())

    def supports_request(self, request: SearchRequest) -> bool:
        return (
            super().supports_request(request)
            and bool(self.topic.search(request.query))
            and bool(self.terms(request))
        )

    def explicitly_requested(self, request: SearchRequest) -> bool:
        domains = request.domain_allowlist or re.findall(
            r"(?<!\S)site:([\w.-]+)", request.query.casefold()
        )
        return domains in (["gov.uk"], ["www.gov.uk"]) and bool(self._authority.search(request.query))

    def _params(self, request, limit):
        return {
            "q": self.terms(request),
            "count": limit,
            "filter_content_store_document_type": self._document_type,
            "fields": "title,link,description,content_store_document_type,first_published_at,date_of_occurrence",
        }

    def _parse(self, body, *, exact=False):
        results = _json(body).get("results")
        if not isinstance(results, list):
            raise ProviderFailure("failed")
        rows = []
        for item in results[:3]:
            row = _dict(item)
            path = row.get("link")
            title = _text(row.get("title"), 512)
            if (
                row.get("content_store_document_type") != self._document_type
                or not title
                or not isinstance(path, str)
                or not re.fullmatch(r"/aaib-reports/[a-z0-9-]{1,240}", path)
            ):
                continue
            occurrence = _publication_date(row.get("date_of_occurrence"))
            parts = [
                f"Date of occurrence: {occurrence.date()}." if occurrence else None,
                _plain_excerpt(row.get("description")),
            ]
            rows.append(
                _Record(
                    title=title,
                    url=f"https://{self.result_domain}{path}",
                    published_at=_publication_date(row.get("first_published_at")),
                    snippet=_text(" ".join(part for part in parts if part), 1200),
                    authors=["Air Accidents Investigation Branch"],
                    language="en",
                )
            )
        return _Batch(rows, incomplete=len(rows) != min(3, len(results)))


class _CkanProvider(_OfficialProvider):
    date_field = "created_at"
    catalogue_date_key: str | None = "metadata_created"
    dataset_filter = ""

    def _params(self, request, limit):
        params = {"rows": limit, "q": self.terms(request)}
        filters = [self.dataset_filter] if self.dataset_filter else []
        if request.time_range and self.catalogue_date_key:
            start, end = request.time_range.start, request.time_range.end
            filters.append(f"{self.catalogue_date_key}:[{start}T00:00:00Z TO {end}T23:59:59.999Z]")
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
            if row.get("archived") is True or row.get("unpublished") is True:
                continue
            if not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", identity):
                continue
            catalogue_date = row.get(self.catalogue_date_key) if self.catalogue_date_key else None
            license_url = public_link(row.get("license_url"))
            rows.append(
                _Record(
                    title=_text(row.get("title"), 512),
                    url=self._record_url(identity, row),
                    published_at=_publication_date(catalogue_date),
                    snippet=_plain_excerpt(row.get("notes")),
                    license_name=_text(row.get("license_title"), 160) if license_url else None,
                    license_url=license_url,
                )
            )
            if len(rows) == 3:
                break
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


class AustraliaOpenDataProvider(_CkanProvider):
    provider_id = "australia-open-data"
    endpoint = "https://data.gov.au/data/api/3/action/package_search"
    result_domain = "data.gov.au"
    region = "au"
    date_range = "unsupported"
    catalogue_date_key = None
    _authority = re.compile(r"\bdata\.gov\.au\b|澳大利亚政府数据目录", re.I)
    topic = re.compile(
        _authority.pattern
        + r"|\b(?:australia|australian) .{0,45}(?:data|datasets?|statistics)\b"
        + r"|澳大利亚.{0,18}(?:数据|统计)",
        re.I,
    )

    def terms(self, request: SearchRequest) -> str:
        return " ".join(self._authority.sub(" ", super().terms(request)).split())

    def supports_request(self, request: SearchRequest) -> bool:
        return super().supports_request(request) and bool(self.terms(request))

    def _record_url(self, identity: str, row: dict) -> str | None:
        slug = row.get("name")
        if (
            row.get("type") == "dataset"
            and isinstance(slug, str)
            and re.fullmatch(r"[a-z0-9_-]{1,160}", slug)
        ):
            return f"https://{self.result_domain}/data/dataset/{slug}"
        return None


class FranceOpenDataProvider(_OfficialProvider):
    provider_id = "france-open-data"
    endpoint = "https://www.data.gouv.fr/api/1/datasets/"
    result_domain = "www.data.gouv.fr"
    date_range = "unsupported"
    region = "fr"
    query_languages = frozenset({"en", "fr"})
    _authority = re.compile(r"\bdata\.gouv\.fr\b|法国开放数据目录", re.I)
    topic = re.compile(
        _authority.pattern
        + r"|\b(?:france|french) .{0,35}(?:data|statistics)\b"
        + r"|\b(?:données|statistiques) .{0,35}(?:france|françaises?)\b"
        + r"|法国.{0,12}(?:数据|统计)",
        re.I,
    )

    def terms(self, request: SearchRequest) -> str:
        return " ".join(self._authority.sub(" ", super().terms(request)).split())

    def supports_request(self, request: SearchRequest) -> bool:
        return super().supports_request(request) and bool(self.terms(request))

    def _params(self, request, limit):
        return {"q": self.terms(request), "page_size": limit}

    def _parse(self, body, *, exact=False):
        data = _json(body).get("data")
        if not isinstance(data, list):
            raise ProviderFailure("failed")
        rows = []
        for item in _rows(data)[:3]:
            row = _dict(item)
            if (
                row.get("private") is not False
                or row.get("access_type") != "open"
                or row.get("archived")
                or row.get("deleted")
            ):
                continue
            publisher = _dict(row.get("organization"))
            if not any(
                _dict(badge).get("kind") in ("public-service", "local-authority")
                for badge in _list(publisher.get("badges"))
            ):
                continue
            page = public_link(row.get("page"))
            if not page or not re.fullmatch(
                r"https://www\.data\.gouv\.fr/(?:fr/)?datasets/[a-z0-9_-]+/?", page
            ):
                continue
            rows.append(
                _Record(
                    title=_text(row.get("title"), 512),
                    url=page,
                    snippet=_plain_excerpt(row.get("description")),
                    authors=[name] if (name := _text(publisher.get("name"), 180)) else [],
                )
            )
        return _Batch(rows)


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


class EuropePMCProvider(_OfficialProvider):
    provider_id = "europe-pmc"
    supplement_timeout_s = 6.0
    endpoint = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    result_domain = "europepmc.org"
    date_range = "metadata_postfilter"
    topic = re.compile(
        r"\b(?:europe pmc|public health|occupational health|aviation noise|cabin air quality|"
        r"epidemiolog\w*|biomedical|clinical (?:studies|trials?))\b|公共卫生|职业健康|航空噪声|流行病学|生物医学",
        re.I,
    )

    def _params(self, request, limit):
        terms = re.sub(r"[^\w .-]", " ", self.terms(request)).strip()
        return {
            "query": f"OPEN_ACCESS:Y AND ({terms})" if terms else "OPEN_ACCESS:Y",
            "resultType": "core",
            "format": "json",
            "pageSize": limit,
        }

    def _parse(self, body, *, exact=False):
        rows = []
        for item in _rows(_dict(_json(body).get("resultList")).get("result")):
            row = _dict(item)
            identity = row.get("pmcid")
            if (
                row.get("isOpenAccess") != "Y"
                or not isinstance(identity, str)
                or not re.fullmatch(r"PMC\d{1,12}", identity)
            ):
                continue
            rows.append(
                _Record(
                    title=_text(row.get("title"), 512),
                    url=f"https://{self.result_domain}/article/PMC/{identity}",
                    published_at=_publication_date(row.get("firstPublicationDate")),
                    snippet=_plain_excerpt(row.get("abstractText")),
                )
            )
        return _Batch(rows)


class TransportationStatisticsProvider(_OfficialProvider):
    provider_id = "us-bts-open-data"
    endpoint = "https://api.us.socrata.com/api/catalog/v1"
    result_domain = "data.bts.gov"
    region = "us"
    date_range = "unsupported"
    topic = re.compile(
        r"\b(?:data\.bts\.gov|bureau of transportation statistics|"
        r"(?:commercial aviation|flight|airline) (?:departures|delays|cancellations|statistics)|"
        r"(?:aviation|airline) (?:passenger traffic|cargo traffic))\b|美国运输统计局|航班延误统计",
        re.I,
    )

    def _params(self, request, limit):
        return {
            "search_context": self.result_domain,
            "q": self.terms(request),
            "only": "datasets",
            "provenance": "official",
            "limit": limit,
        }

    def _parse(self, body, *, exact=False):
        rows = []
        for item in _rows(_json(body).get("results"))[:3]:
            row = _dict(item)
            resource = _dict(row.get("resource"))
            identity = resource.get("id")
            if (
                resource.get("type") != "dataset"
                or resource.get("provenance") != "official"
                or _dict(row.get("metadata")).get("domain") != self.result_domain
                or not isinstance(identity, str)
                or not re.fullmatch(r"[a-z0-9]{4}-[a-z0-9]{4}", identity)
                or not (url := public_link(row.get("link")))
            ):
                continue
            target = urlsplit(url)
            if (
                target.scheme != "https"
                or target.netloc != self.result_domain
                or not re.fullmatch(rf"/(?:[a-zA-Z0-9_-]+/){{1,2}}{identity}", target.path)
            ):
                continue
            rows.append(
                _Record(
                    title=_text(resource.get("name"), 512),
                    url=f"https://{self.result_domain}{target.path}",
                    snippet=_text(resource.get("description"), 1200),
                )
            )
        return _Batch(rows)


class EuropeanDataPortalProvider(_OfficialProvider):
    provider_id = "eu-open-data"
    endpoint = "https://data.europa.eu/api/hub/search/search"
    result_domain = "data.europa.eu"
    region = "eu"
    supplement_timeout_s = 4.0
    publishers = {
        "estat": ("Eurostat", "publications.europa.eu/resource/authority/corporate-body/ESTAT"),
        "eea-sdi": ("European Environment Agency", "www.eea.europa.eu"),
        "cinea": (
            "European Climate, Infrastructure and Environment Executive Agency",
            "publications.europa.eu/resource/authority/corporate-body/CINEA",
        ),
    }
    named_catalogues = {
        "estat": re.compile(r"\beurostat\b|欧盟统计局", re.I),
        "eea-sdi": re.compile(r"\beuropean environment agency\b|欧洲环境署", re.I),
        "cinea": re.compile(r"\bcinea\b|欧盟气候、基础设施与环境执行局", re.I),
    }
    topic = re.compile(
        r"|".join(hint.pattern for hint in named_catalogues.values())
        + r"|\bdata\.europa\.eu\b|\b(?:eu|european union) .{0,28}(?:statistics|open data|datasets?)\b"
        + r"|欧盟.{0,12}(?:开放数据|统计数据)",
        re.I,
    )

    def _params(self, request, limit):
        query = self.terms(request)
        selected = [name for name, hint in self.named_catalogues.items() if hint.search(query)]
        for name in selected:
            query = self.named_catalogues[name].sub(" ", query)
        params = {
            "q": " ".join(query.split()),
            "filters": "dataset",
            "facets": json.dumps({"catalog": selected or list(self.publishers)}),
            "aggregation": "false",
            "limit": limit,
        }
        if request.time_range:
            params.update(
                dateType="issued",
                minDate=f"{request.time_range.start}T00:00:00Z",
                maxDate=f"{request.time_range.end}T23:59:59.999Z",
            )
        return params

    @staticmethod
    def _distribution_license(row):
        licenses = set()
        for item in _list(row.get("distributions")):
            license = _dict(_dict(item).get("license"))
            url = public_link(license.get("resource"))
            if not url:
                return None, None
            licenses.add((_text(license.get("label"), 120), url))
        return licenses.pop() if len(licenses) == 1 else (None, None)

    def _parse(self, body, *, exact=False):
        rows = []
        for item in _rows(_dict(_json(body).get("result")).get("results"))[:3]:
            row = _dict(item)
            identity = row.get("id")
            catalogue = _dict(row.get("catalog")).get("id")
            if (
                row.get("index") != "dataset"
                or not isinstance(catalogue, str)
                or catalogue not in self.publishers
                or not isinstance(identity, str)
                or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,159}", identity)
                or not (resource := public_link(row.get("resource")))
            ):
                continue
            target = urlsplit(resource)
            rights = _dict(row.get("access_rights")).get("id")
            if target.netloc != self.result_domain or target.path != f"/88u/dataset/{identity}":
                continue
            if rights in ("NON_PUBLIC", "RESTRICTED", "SENSITIVE"):
                continue
            name, authority = self.publishers[catalogue]
            publisher = _dict(row.get("publisher"))
            organization = urlsplit(
                public_link(publisher.get("resource") or publisher.get("homepage")) or ""
            )
            if organization.netloc + organization.path.rstrip("/") != authority:
                continue
            description = _plain_excerpt(_dict(row.get("description")).get("en"))
            license_name, license_url = self._distribution_license(row)
            rows.append(
                _Record(
                    title=_text(_dict(row.get("title")).get("en"), 512),
                    url=f"https://{self.result_domain}/data/datasets/{identity}?locale=en",
                    authors=[name],
                    published_at=_publication_date(row.get("issued")),
                    snippet=_text(" · ".join(filter(None, [name, description])), 1200),
                    license_name=license_name,
                    license_url=license_url,
                    language="en",
                )
            )
        return _Batch(rows)


class WorldBankReportsProvider(_OfficialProvider):
    provider_id = "world-bank-reports"
    endpoint = "https://search.worldbank.org/api/v3/wds"
    result_domain = "documents.worldbank.org"
    supplement_timeout_s = 4.0
    topic = re.compile(r"\bworld bank\b|世界银行|世行报告", re.I)

    def _params(self, request, limit):
        query = " ".join(self.topic.sub(" ", self.terms(request)).split())
        params = {
            "format": "json",
            "qterm": query,
            "rows": limit,
            "fl": "display_title,docdt,abstract,docty",
            "lang_exact": "English",
        }
        if request.time_range:
            params.update(
                strdate=str(request.time_range.start), enddate=str(request.time_range.end)
            )
        return params

    def _parse(self, body, *, exact=False):
        records = _dict(_json(body).get("documents"))
        rows = []
        for item in records.values():
            if len(rows) == 3:
                break
            row = _dict(item)
            url = urlsplit(public_link(row.get("url")) or "")
            if url.netloc != self.result_domain or not re.fullmatch(
                r"/curated/en/[0-9]{8,24}(?:/[^/]+)?", url.path
            ):
                continue
            abstract = _plain_excerpt(_dict(row.get("abstracts")).get("cdata!"))
            rows.append(
                _Record(
                    title=_text(row.get("display_title"), 512),
                    url=f"https://{self.result_domain}{url.path}",
                    published_at=_publication_date(row.get("docdt")),
                    snippet=_text(
                        " · ".join(filter(None, [_text(row.get("docty"), 120), abstract])), 1200
                    ),
                    language="en",
                )
            )
        return _Batch(rows)


class ClinicalTrialsRegistryProvider(_OfficialProvider):
    provider_id = "clinicaltrials-registry"
    endpoint = "https://clinicaltrials.gov/api/v2/studies"
    result_domain = "clinicaltrials.gov"
    supplement_timeout_s = 4.0
    topic = re.compile(r"\bclinicaltrials(?:\.gov)?\b|\bNCT[0-9]{8}\b|美国临床试验登记", re.I)
    _registry_name = re.compile(r"\bclinicaltrials(?:\.gov)?\b|美国临床试验登记", re.I)

    def _params(self, request, limit):
        query = " ".join(self._registry_name.sub(" ", self.terms(request)).split())
        params = {
            "format": "json",
            "query.term": query,
            "pageSize": limit,
            "fields": "NCTId,BriefTitle,BriefSummary,StudyFirstPostDate,OverallStatus",
        }
        if request.time_range:
            dates = request.time_range
            params["filter.advanced"] = f"AREA[StudyFirstPostDate]RANGE[{dates.start},{dates.end}]"
        return params

    def _parse(self, body, *, exact=False):
        rows = []
        for item in _rows(_json(body).get("studies"))[:3]:
            protocol = _dict(_dict(item).get("protocolSection"))
            record = _dict(protocol.get("identificationModule"))
            study_id = record.get("nctId")
            if not isinstance(study_id, str) or not re.fullmatch(r"NCT[0-9]{8}", study_id):
                continue
            status = _dict(protocol.get("statusModule"))
            description = _plain_excerpt(
                _dict(protocol.get("descriptionModule")).get("briefSummary")
            )
            rows.append(
                _Record(
                    title=_text(record.get("briefTitle"), 512),
                    url=f"https://{self.result_domain}/study/{study_id}",
                    published_at=_publication_date(
                        _dict(status.get("studyFirstPostDateStruct")).get("date")
                    ),
                    snippet=_text(
                        " · ".join(
                            filter(
                                None,
                                [study_id, _text(status.get("overallStatus"), 64), description],
                            )
                        ),
                        1200,
                    ),
                    language="en",
                )
            )
        return _Batch(rows)


class NoaaClimateDataProvider(_OfficialProvider):
    provider_id = "noaa-climate-data"
    endpoint = "https://www.ncei.noaa.gov/access/services/search/v1/datasets"
    result_domain = "www.ncei.noaa.gov"
    date_range = "unsupported"
    supplement_timeout_s = 4.0
    _authority = re.compile(
        r"\b(?:noaa|ncei)(?:\s+ncei)?\b|美国国家环境信息中心|美国国家海洋和大气管理局",
        re.I,
    )
    topic = re.compile(
        _authority.pattern
        + r"|\b(?:airport weather|aviation weather|wind gusts?|climatological data|METAR)\b",
        re.I,
    )

    def terms(self, request: SearchRequest) -> str:
        return " ".join(self._authority.sub(" ", super().terms(request)).split())

    def supports_request(self, request: SearchRequest) -> bool:
        return super().supports_request(request) and bool(self.terms(request))

    def _params(self, request, limit):
        return {"text": self.terms(request)}

    def _parse(self, body, *, exact=False):
        rows = []
        for item in _rows(_json(body).get("results")):
            row = _dict(item)
            identity = row.get("fileId")
            title = _text(row.get("name"), 512)
            if not title or not isinstance(identity, str) or not re.fullmatch(
                r"gov\.noaa\.(?:ncdc|ncei):C[0-9]{5,9}", identity
            ):
                continue
            rows.append(
                _Record(
                    title=title,
                    url=f"https://{self.result_domain}/access/metadata/landing-page/bin/iso?id={identity}",
                    snippet=_plain_excerpt(row.get("description")),
                    authors=["NOAA National Centers for Environmental Information"],
                    language="en",
                )
            )
            if len(rows) == 3:
                break
        return _Batch(rows)


class CordisProjectsProvider(_OfficialProvider):
    provider_id = "eu-cordis-projects"
    endpoint = "https://cordis.europa.eu/datalab/sparql"
    result_domain = "cordis.europa.eu"
    accept = "application/sparql-results+json"
    date_range = "unsupported"
    supplement_timeout_s = 4.0
    authority = re.compile(r"\bCORDIS\b", re.I)
    topic = re.compile(
        r"\b(?:CORDIS|Horizon Europe|Clean Aviation Joint Undertaking)\b|地平线欧洲", re.I
    )

    def terms(self, request: SearchRequest) -> str:
        return " ".join(self.authority.sub(" ", super().terms(request)).split()).strip(" \"'")

    def supports_request(self, request: SearchRequest) -> bool:
        return super().supports_request(request) and bool(self.terms(request))

    def explicitly_requested(self, request: SearchRequest) -> bool:
        domains = request.domain_allowlist or re.findall(
            r"(?<!\S)site:([\w.-]+)", request.query.casefold()
        )
        return domains == [self.result_domain] or bool(self.authority.search(request.query))

    def _params(self, request, limit):
        terms = self.terms(request)
        literal = json.dumps(terms.lower(), ensure_ascii=False)
        condition = (
            f"STR(?id) = {literal}"
            if re.fullmatch(r"[0-9]{1,16}", terms)
            else f"CONTAINS(LCASE(STR(?title)), {literal})"
        )
        query = f"""PREFIX eurio: <http://data.europa.eu/s66#>
SELECT DISTINCT ?id ?title ?abstract ?start ?end WHERE {{
  ?project a eurio:Project ; eurio:identifier ?id ; eurio:title ?title .
  FILTER({condition})
  OPTIONAL {{ ?project eurio:abstract ?abstract }}
  OPTIONAL {{ ?project eurio:startDate ?start }}
  OPTIONAL {{ ?project eurio:endDate ?end }}
}}
LIMIT {limit}"""
        return {"query": query, "format": self.accept}

    def _parse(self, body, *, exact=False):
        records = _rows(_dict(_json(body).get("results")).get("bindings"))
        rows = []
        for item in records[:3]:
            row = _dict(item)
            identity = _dict(row.get("id")).get("value")
            title = _text(_dict(row.get("title")).get("value"), 512)
            if not isinstance(identity, str) or not re.fullmatch(r"[0-9]{1,16}", identity):
                rows.append(_Record(title=title, url=None))
                continue
            start = _publication_date(_dict(row.get("start")).get("value"))
            end = _publication_date(_dict(row.get("end")).get("value"))
            details = [f"项目编号 {identity}"]
            if start:
                details.append(f"目录记录开始 {start.date().isoformat()}")
            if end:
                details.append(f"目录记录结束 {end.date().isoformat()}")
            if abstract := _plain_excerpt(_dict(row.get("abstract")).get("value"), 1000):
                details.append(abstract)
            rows.append(
                _Record(
                    title=title,
                    url=f"https://{self.result_domain}/project/id/{identity}",
                    snippet=" · ".join(details),
                )
            )
        return _Batch(rows)


class OstiResearchProvider(_OfficialProvider):
    provider_id = "doe-osti-research"
    endpoint = "https://www.osti.gov/api/v1/records"
    result_domain = "www.osti.gov"
    supplement_timeout_s = 4.0
    _authority = re.compile(r"\b(?:DOE\s+)?OSTI(?:\.GOV)?\b|美国能源部(?:科技信息办公室)?", re.I)
    topic = re.compile(
        _authority.pattern
        + r"|\b(?:aviation fuels?|grid[- ]scale energy storage|long[- ]duration energy storage)\b"
        + r"|可持续航空燃料|长时储能",
        re.I,
    )

    def terms(self, request: SearchRequest) -> str:
        return " ".join(self._authority.sub(" ", super().terms(request)).split())

    def supports_request(self, request: SearchRequest) -> bool:
        return super().supports_request(request) and bool(self.terms(request))

    def _params(self, request, limit):
        params = {"q": self.terms(request), "rows": limit}
        if request.time_range:
            params.update(
                publication_date_start=request.time_range.start.strftime("%m/%d/%Y"),
                publication_date_end=request.time_range.end.strftime("%m/%d/%Y"),
            )
        if request.language:
            params["language"] = "English"
        return params

    def _parse(self, body, *, exact=False):
        try:
            records = _rows(json.loads(body))
        except (ValueError, RecursionError):
            raise ProviderFailure("failed") from None
        rows = []
        for item in records:
            row = _dict(item)
            identity = row.get("osti_id")
            title = _text(row.get("title"), 512)
            if not title or not isinstance(identity, str) or not re.fullmatch(r"[0-9]{1,16}", identity):
                continue
            rows.append(
                _Record(
                    title=title,
                    url=f"https://{self.result_domain}/biblio/{identity}",
                    published_at=_publication_date(row.get("publication_date")),
                    snippet=_plain_excerpt(row.get("description")),
                    authors=[
                        name for value in _list(row.get("authors"))[:20] if (name := _text(value, 180))
                    ],
                    doi=_doi(row.get("doi")),
                    language="en" if row.get("language") == "English" else None,
                )
            )
            if len(rows) == 3:
                break
        return _Batch(rows)


def create_official_providers(
    client: httpx.AsyncClient, enabled_sources: frozenset[str]
) -> list[Provider]:
    return [
        provider(client)
        for provider in (
            FederalRegisterProvider,
            AAIBReportsProvider,
            UKOpenDataProvider,
            CanadaOpenDataProvider,
            AustraliaOpenDataProvider,
            FranceOpenDataProvider,
            EarthdataProvider,
            USGSProvider,
            EuropePMCProvider,
            EuropeanDataPortalProvider,
            TransportationStatisticsProvider,
            WorldBankReportsProvider,
            ClinicalTrialsRegistryProvider,
            NoaaClimateDataProvider,
            CordisProjectsProvider,
            OstiResearchProvider,
        )
        if provider.provider_id in enabled_sources
    ]
