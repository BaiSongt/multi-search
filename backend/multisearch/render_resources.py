import json

from .safe_fetch import MAX_RESOURCE_BYTES as MAX_RESOURCE_BYTES
from .safe_fetch import FetchedResource as FetchedResource
from .safe_fetch import FetchError, render_target
from .safe_fetch import ResourceSpec as ResourceSpec

MAX_RESOURCE_BUNDLE_BYTES = 512 * 1024
MAX_RESOURCES_PER_PAGE = 8
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_PAGES = 128
_INVALID_MANIFEST = "MULTISEARCH_DYNAMIC_FETCH_RESOURCES 需要有效的同源 HTTPS 资源清单"


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    entries = dict(pairs)
    if len(entries) != len(pairs):
        raise ValueError(_INVALID_MANIFEST)
    return entries


def parse_resource_manifest(value: str) -> dict[str, tuple[ResourceSpec, ...]]:
    if not value.isascii() or len(value) > _MAX_MANIFEST_BYTES:
        raise ValueError(_INVALID_MANIFEST)
    if not value.strip():
        return {}
    try:
        raw = json.loads(value, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError):
        raise ValueError(_INVALID_MANIFEST) from None
    if not isinstance(raw, dict) or len(raw) > _MAX_PAGES:
        raise ValueError(_INVALID_MANIFEST)
    manifest: dict[str, tuple[ResourceSpec, ...]] = {}
    try:
        for url, entries in raw.items():
            page = render_target(url)
            if not isinstance(entries, list) or len(entries) > MAX_RESOURCES_PER_PAGE:
                raise ValueError(_INVALID_MANIFEST)
            used = {page.url}
            specs = []
            for entry in entries:
                if (
                    not isinstance(entry, dict)
                    or entry.keys() != {"url", "kind"}
                    or not isinstance(entry["url"], str)
                    or entry["kind"] not in ("script", "json")
                ):
                    raise ValueError(_INVALID_MANIFEST)
                resource = render_target(entry["url"])
                if resource.origin != page.origin or resource.url in used:
                    raise ValueError(_INVALID_MANIFEST)
                used.add(resource.url)
                specs.append(ResourceSpec(resource.url, entry["kind"]))
            manifest[page.url] = tuple(specs)
    except FetchError:
        raise ValueError(_INVALID_MANIFEST) from None
    return manifest
