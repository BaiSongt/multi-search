"""在已启动的 Compose 环境中验证首轮交付。"""

import argparse
import ipaddress
import json
import re
import subprocess
import sys
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
KEY_PATTERN = re.compile(r"msk_([0-9a-f]{32})\.[A-Za-z0-9_-]{43}")
COMPOSE_EXEC = ["docker", "compose", "exec", "-T", "multisearch-api"]
SEARCH_PATH = "/api/v1/search"


class SmokeFailure(Exception):
    """向操作员报告脱敏的冒烟验证失败。"""


class NoRedirect(HTTPRedirectHandler):
    """禁止带测试凭据的 HTTP 请求重定向到其他目标。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def loopback_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme in {"http", "https"}
            and ipaddress.ip_address(parsed.hostname or "").is_loopback
            and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
            and parsed.path in {"", "/"}
            and (parsed.port is None or parsed.port > 0)
        ):
            return value.rstrip("/")
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("请输入仅含回环 IP 与端口的 HTTP(S) 地址")


def require(condition: bool, detail: str) -> None:
    if not condition:
        raise SmokeFailure(detail)


def fetch(
    base: str,
    path: str,
    *,
    key: str | None = None,
    payload: dict | None = None,
) -> tuple[int, Message, bytes]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if key is not None:
        headers["Authorization"] = f"Bearer {key}"
    request = Request(f"{base}{path}", data=data, headers=headers)
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        try:
            response = opener.open(request, timeout=20)
        except HTTPError as error:
            response = error
        with response:
            return response.code, response.headers, response.read(2_000_000)
    except (URLError, OSError) as error:
        raise SmokeFailure(f"{path} 无法连接或已超时") from error


def json_object(body: bytes, name: str) -> dict:
    try:
        value = json.loads(body)
    except (UnicodeError, ValueError) as error:
        raise SmokeFailure(f"{name} 未返回合法 JSON") from error
    require(isinstance(value, dict), f"{name} JSON 顶层必须是对象")
    return value


def expected_response(
    base: str,
    path: str,
    status: int,
    *,
    key: str | None = None,
    payload: dict | None = None,
    code: str | None = None,
) -> dict:
    actual, headers, body = fetch(base, path, key=key, payload=payload)
    require(actual == status, f"{path} 应返回 HTTP {status}，实际为 HTTP {actual}")
    document = json_object(body, path)
    if code is not None:
        require(document.get("code") == code, f"{path} 未返回错误码 {code}")
        require(
            bool(document.get("request_id"))
            and document["request_id"] == headers.get("X-Request-ID"),
            f"{path} 缺少一致的请求标识",
        )
    return document


def compose_command(arguments: list[str]) -> str:
    try:
        completed = subprocess.run(
            [*COMPOSE_EXEC, *arguments],
            cwd=ROOT,
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SmokeFailure("Compose 容器命令不可用或已超时") from error
    if completed.returncode != 0:
        raise SmokeFailure(f"Compose 容器命令失败，退出码 {completed.returncode}")
    return completed.stdout.strip()


def issue_key(tenant: str, *scopes: str) -> tuple[str, str]:
    arguments = [
        "python",
        "-m",
        "multisearch.keys",
        "issue",
        "--tenant",
        tenant,
        "--email",
        "smoke@example.invalid",
        "--ttl-hours",
        "1",
    ]
    for scope in scopes:
        arguments.extend(["--scope", scope])
    key = compose_command(arguments)
    match = KEY_PATTERN.fullmatch(key)
    if match is None:
        raise SmokeFailure("CLI 返回了无法识别的测试密钥格式")
    return key, match.group(1)


SEARXNG_SEARCH_PROBE = """
import json
import os
from urllib.parse import urlencode
from urllib.request import urlopen

base = os.environ.get("SEARXNG_BASE_URL", "http://searxng:8080").rstrip("/")
parameters = urlencode({"q": "Golden Gate Bridge", "categories": "images", "format": "json"})
with urlopen(f"{base}/search?{parameters}", timeout=12) as response:
    result = json.load(response)
results = result.get("results")
if not isinstance(results, list):
    raise ValueError("SearXNG 缺少结果列表")
print(json.dumps({
    "result_count": len(results),
    "image_count": sum(isinstance(row, dict) and bool(row.get("img_src")) for row in results),
    "unresponsive_engines": len(result.get("unresponsive_engines", [])),
}))
"""


def probe_searxng() -> None:
    image_probe = json_object(
        compose_command(["python", "-c", SEARXNG_SEARCH_PROBE]).encode(),
        "SearXNG images JSON",
    )
    require(
        image_probe.get("image_count") == image_probe.get("result_count"),
        "SearXNG images 分类返回了缺少原图字段的结果",
    )
    print(
        f"通过 SearXNG images JSON 接口 · {image_probe['image_count']} 条图片候选，"
        f"{image_probe['unresponsive_engines']} 个上游未响应"
    )


def is_http_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def live_search(api_url: str, key: str, modality: str) -> tuple[dict | None, dict]:
    query = "NASA" if modality == "text" else "Golden Gate Bridge"
    payload = {"query": query, "mode": "standard", "modalities": [modality], "max_results": 3}
    status, headers, body = fetch(api_url, SEARCH_PATH, key=key, payload=payload)
    document = json_object(body, f"{modality} 搜索")
    require(
        bool(document.get("request_id")) and document["request_id"] == headers.get("X-Request-ID"),
        f"{modality} 搜索缺少一致的请求标识",
    )
    if status in {502, 503, 504} and document.get("code") in {
        "PROVIDER_UNAVAILABLE",
        "TIME_BUDGET_EXCEEDED",
    }:
        print(f"未验收 {modality} 公网检索 · HTTP {status} · {document['code']}")
        return None, payload
    require(status == 200, f"{modality} 搜索 HTTP {status}")
    results = document.get("results")
    require(
        isinstance(results, list)
        and len(results) == document.get("result_count")
        and len(results) <= 3,
        f"{modality} 响应的结果数不符合请求约定",
    )
    for result in results:
        require(
            isinstance(result, dict)
            and result.get("kind") == modality
            and bool(result.get("retrieved_at"))
            and bool(result.get("provider_ids")),
            f"{modality} 结果缺少模态、检索时间或来源",
        )
        if modality == "image":
            image = result.get("image")
            require(
                isinstance(image, dict)
                and is_http_url(image.get("original_url"))
                and is_http_url(image.get("source_page_url"))
                and image.get("original_url") != image.get("source_page_url")
                and image.get("rights_status") == "unknown",
                "图片结果缺少独立的原图、来源页或未知授权状态",
            )
        else:
            require(is_http_url(result.get("url")), "文字结果缺少有效来源 URL")
    if not results:
        print(f"未验收 {modality} 公网检索 · 上游返回 0 条符合条件的结果")
        return None, payload
    outcome = "部分成功" if document.get("partial") is True else "通过"
    print(f"{outcome} {modality} 公网检索 · {len(results)} 条结果")
    return document, payload


def validate_local_miss(api_url: str, key: str, payload: dict) -> None:
    miss = expected_response(api_url, SEARCH_PATH, 200, key=key, payload=payload)
    require(
        miss.get("partial") is True
        and miss.get("cache_hit") is False
        and miss.get("results") == []
        and miss.get("provider_runs") == []
        and "LOCAL_CACHE_MISS" in miss.get("warnings", []),
        "本地模式未按契约报告缓存缺失",
    )


def validate_cache(api_url: str, key: str, initial: dict | None, payload: dict) -> None:
    if initial is None or initial.get("partial") is True:
        print("未验收缓存命中 · 需要一次完整、有结果的公网响应")
        return
    cached = expected_response(
        api_url, SEARCH_PATH, 200, key=key, payload={**payload, "mode": "local"}
    )
    require(
        cached.get("cache_hit") is True
        and cached.get("request_id") != initial.get("request_id")
        and cached.get("results") == initial.get("results"),
        "相同请求未命中缓存或缓存结果不一致",
    )
    print("通过同租户本地模式缓存命中与新请求标识")


def run(args: argparse.Namespace, issued_ids: list[str]) -> None:
    api = args.api_url
    live = expected_response(api, "/health/live", 200)
    ready = expected_response(api, "/health/ready", 200)
    require(live.get("status") == "alive" and ready.get("status") == "ready", "健康状态不符合约定")
    status, _, console = fetch(args.console_url, "/")
    require(status == 200 and b"MultiSearch" in console, "Console 首页不可用")
    expected_response(api, "/api/v1/capabilities", 200)
    expected_response(api, "/api/v1/providers", 401, code="AUTH_REQUIRED")
    expected_response(api, SEARCH_PATH, 401, payload={"query": "test"}, code="AUTH_REQUIRED")
    print("通过 API、Console、能力发现与匿名访问限制")

    key, key_id = issue_key("multisearch-smoke", "search:text", "search:image")
    issued_ids.append(key_id)
    text_key, text_key_id = issue_key("multisearch-smoke-isolated", "search:text")
    issued_ids.append(text_key_id)
    expected_response(api, "/api/v1/providers", 200, key=key)
    expected_response(
        api, SEARCH_PATH, 422, key=key, payload={"query": " "}, code="INVALID_REQUEST"
    )
    expected_response(
        api,
        SEARCH_PATH,
        403,
        key=text_key,
        payload={"query": "test", "modalities": ["image"]},
        code="FORBIDDEN",
    )
    expected_response(
        api,
        SEARCH_PATH,
        422,
        key=text_key,
        payload={"query": "internal-smoke", "network_scope": "offline"},
        code="SOURCE_UNAVAILABLE",
    )
    validate_local_miss(api, key, {"query": f"smoke-{uuid4().hex}", "mode": "local"})
    print("通过请求验证、图片作用域隔离、离线边界与本地缓存缺失")

    probe_searxng()
    text_result, text_payload = live_search(api, key, "text")
    image_result, image_payload = live_search(api, key, "image")
    if text_result is not None and text_result.get("partial") is False:
        validate_cache(api, key, text_result, text_payload)
        validate_local_miss(api, text_key, {**text_payload, "mode": "local"})
        print("通过跨租户缓存隔离")
    else:
        validate_cache(api, key, image_result, image_payload)
        print("未验收跨租户缓存隔离 · 需要一次完整、有结果的公网文字响应")
    compose_command(["python", "-m", "multisearch.keys", "revoke", "--id", text_key_id])
    issued_ids.remove(text_key_id)
    expected_response(api, "/api/v1/providers", 401, key=text_key, code="AUTH_REQUIRED")
    print("通过密钥吊销")
    if not args.allow_upstream_failure:
        require(text_result is not None and image_result is not None, "公网双模态结果未验收")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", type=loopback_url, default="http://127.0.0.1:8765")
    parser.add_argument("--console-url", type=loopback_url, default="http://127.0.0.1:3000")
    parser.add_argument(
        "--allow-upstream-failure",
        action="store_true",
        help="公网无结果时仍可验收容器与本地契约",
    )
    issued_ids: list[str] = []
    failed = False
    try:
        run(parser.parse_args(), issued_ids)
    except SmokeFailure as error:
        print(f"失败 {error}", file=sys.stderr)
        failed = True
    finally:
        for key_id in issued_ids:
            try:
                compose_command(["python", "-m", "multisearch.keys", "revoke", "--id", key_id])
            except SmokeFailure as error:
                print(f"失败 清理测试密钥 · {error}", file=sys.stderr)
                failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
