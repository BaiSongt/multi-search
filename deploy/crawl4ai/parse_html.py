"""在一次性进程中调用 Crawl4AI 的纯 HTML 过滤器。"""

import json
import sys
from contextlib import redirect_stdout
from unicodedata import category, normalize

_MAX_BODY = 1024 * 1024
_EXCERPT_TAGS = {"p", "li", "blockquote", "pre", "td", "dd", "dt", "figcaption"}


def _clean(value: str) -> str:
    normalized = normalize("NFC", value)
    return " ".join(
        "".join(c for c in normalized if c.isspace() or not category(c).startswith("C")).split()
    )


def parse(raw: bytes) -> list[str]:
    from crawl4ai.content_filter_strategy import PruningContentFilterLXML
    from lxml import html

    raw_html = raw.decode("utf-8", errors="strict")
    chunks = PruningContentFilterLXML().filter_content(raw_html)
    parser = html.HTMLParser(no_network=True, recover=True, huge_tree=False)
    results: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        root = html.fragment_fromstring(chunk, create_parent=True, parser=parser)
        candidates = [element for element in root.iter() if element.tag in _EXCERPT_TAGS] or [root]
        for element in candidates:
            value = _clean(element.text_content())[:512]
            if value and value not in seen:
                results.append(value)
                seen.add(value)
            if len(results) == 24:
                return results
    return results


if __name__ == "__main__":
    body = sys.stdin.buffer.read(_MAX_BODY + 1)
    if not body or len(body) > _MAX_BODY:
        raise SystemExit(2)
    try:
        with redirect_stdout(sys.stderr):
            blocks = parse(body)
    except Exception:
        raise SystemExit(2) from None
    output = json.dumps({"version": "0.9.5", "blocks": blocks}, ensure_ascii=False)
    sys.stdout.buffer.write(output.encode("utf-8"))
