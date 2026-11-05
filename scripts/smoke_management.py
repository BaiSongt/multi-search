"""以本地 Compose 和真实 PostgreSQL 验证阶段 I 管理面与计量。"""

import argparse
import json
import secrets
import subprocess
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener
from uuid import uuid4

from smoke import ROOT, NoRedirect, SmokeFailure, loopback_url, require


class Client:
    def __init__(self, origin: str, base: str):
        self.origin = origin
        self.base = base
        self.cookies = CookieJar()
        self.opener = build_opener(
            ProxyHandler({}), NoRedirect(), HTTPCookieProcessor(self.cookies)
        )
        self.csrf = None

    def call(
        self,
        path: str,
        status: int = 200,
        *,
        method: str = "GET",
        payload: dict | None = None,
        key: str | None = None,
        csrf: bool = True,
        code: str | None = None,
    ) -> dict:
        headers = {"Origin": self.origin, "Accept": "application/json"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode()
        if csrf and self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        if key:
            headers["Authorization"] = f"Bearer {key}"
        request = Request(f"{self.base}/{path}", method=method, headers=headers, data=data)
        try:
            result = self.opener.open(request, timeout=20)
        except HTTPError as error:
            result = error
        with result:
            require(
                result.status == status, f"{method} {path} 预期 HTTP {status}，实际 {result.status}"
            )
            raw = result.read(2_000_000)
            body = json.loads(raw) if raw else {}
            if code:
                require(body.get("code") == code, f"{method} {path} 错误码不匹配")
            if path in {"search", "playground"}:
                require(
                    body.get("request_id") == result.headers.get("X-Request-ID"), "请求标识不一致"
                )
            return body

    def login(self, tenant: str, email: str, password: str, role: str) -> dict:
        view = self.call(
            "auth/login",
            method="POST",
            payload={
                "tenant": tenant,
                "email": email,
                "password": password,
            },
        )
        require(view["role"] == role, "登录角色错误")
        self.csrf = view["csrf_token"]
        cookie = next(iter(self.cookies), None)
        require(cookie is not None and cookie.has_nonstandard_attr("HttpOnly"), "会话缺少 HttpOnly")
        require(self.call("me")["tenant"] == tenant, "会话未绑定正确租户")
        return view


def create_user(tenant: str, role: str) -> tuple[str, str]:
    email = f"{tenant}@example.invalid"
    password = secrets.token_urlsafe(24)
    proc = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "multisearch-api",
            "python",
            "-m",
            "multisearch.keys",
            "user-add",
            "--tenant",
            tenant,
            "--email",
            email,
            "--role",
            role,
            "--password-stdin",
        ],
        input=password,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    require(proc.returncode == 0, "无法创建本地验收用户")
    return email, password


def run(origin: str, api_url: str | None = None) -> None:
    suffix = uuid4().hex[:10]
    base = f"{api_url}/api/v1" if api_url else f"{origin}/gateway"
    clients = [Client(origin, base) for _ in range(4)]
    admin, developer, outsider, machine = clients
    admin_tenant, dev_tenant, other_tenant = (
        f"smoke-{name}-{suffix}" for name in ("admin", "dev", "other")
    )
    admin.login(admin_tenant, *create_user(admin_tenant, "platform_admin"), "platform_admin")
    new_tenant = admin.call("admin/tenants", 201, method="POST", payload={"slug": dev_tenant})
    admin.call(
        "admin/tenants",
        409,
        method="POST",
        payload={"slug": dev_tenant},
        code="TENANT_STATE_CONFLICT",
    )
    member = developer.login(dev_tenant, *create_user(dev_tenant, "developer"), "developer")
    other = outsider.login(other_tenant, *create_user(other_tenant, "developer"), "developer")
    tenant = member["tenant_id"]
    require(tenant == new_tenant["id"], "开发者未加入管理员创建的租户")
    admin.call(
        f"admin/tenants/{tenant}/quota",
        method="PATCH",
        payload={
            "quota": {"requests_per_day": 5, "local_per_day": 5},
        },
    )
    developer.call("admin/overview", 403, code="FORBIDDEN")
    body = {
        "name": "阶段 I 验收",
        "environment": "test",
        "scopes": ["search:text"],
        "limits": {"requests_per_day": 1},
    }
    developer.call("keys", 403, method="POST", payload=body, csrf=False, code="FORBIDDEN")
    created = developer.call("keys", 201, method="POST", payload=body)
    raw = created["secret"]
    key_id = created["key"]["id"]
    masked = developer.call("keys")
    require(
        raw not in json.dumps(masked) and masked["keys"][0]["id"] == key_id, "密钥列表泄露或错位"
    )
    require(outsider.call("keys")["keys"] == [], "读取到了其他租户的 Key")
    outsider.call(f"keys/{key_id}/revoke", 404, method="POST")
    machine.call("keys", 401, key=raw)

    search = {"query": "management-smoke", "mode": "local", "idempotency_key": f"smoke-{suffix}"}
    first = machine.call("search", method="POST", payload=search, key=raw)
    replay = machine.call(
        "search", method="POST", payload={**search, "request_id": "client-retry"}, key=raw
    )
    require(first == replay, "幂等重试未复用原响应")
    machine.call(
        "search",
        409,
        method="POST",
        payload={**search, "query": "different"},
        key=raw,
        code="IDEMPOTENCY_CONFLICT",
    )
    machine.call(
        "search",
        429,
        method="POST",
        payload={**search, "idempotency_key": "another"},
        key=raw,
        code="QUOTA_EXCEEDED",
    )
    require(developer.call("usage")["totals"]["search_requests"] == 1, "用量重复扣除")
    require(outsider.call("usage")["totals"]["search_requests"] == 0, "跨租户用量泄漏")
    developer.call(
        "playground", method="POST", payload={"query": "management-smoke", "mode": "local"}
    )
    require(developer.call("usage")["totals"]["search_requests"] == 2, "Playground 用量缺失")

    rotated = developer.call(f"keys/{key_id}/rotate", method="POST", payload={"grace_seconds": 0})
    machine.call("providers", 401, key=raw, code="AUTH_REQUIRED")
    machine.call("providers", key=rotated["secret"])
    developer.call(f"keys/{rotated['key']['id']}/revoke", method="POST")
    machine.call("providers", 401, key=rotated["secret"], code="AUTH_REQUIRED")
    requests = admin.call(f"admin/requests?tenant_id={tenant}&mode=local&limit=1")
    cursor = requests["next_cursor"]
    require(bool(cursor), "请求列表缺少分页游标")
    next_page = admin.call(f"admin/requests?tenant_id={tenant}&mode=local&limit=1&cursor={cursor}")
    require(requests["items"][0]["id"] != next_page["items"][0]["id"], "分页返回了重复请求")
    require(admin.call("admin/overview")["totals"]["search_requests"] >= 2, "管理总览缺少实测用量")
    audits = admin.call(f"admin/audit?tenant_id={tenant}")["items"]
    require(any(row["action"] == "quota.update" for row in audits), "管理员额度修改缺少审计")
    require(raw not in json.dumps(audits), "审计输出泄露密钥")
    for client in (developer, outsider):
        client.call("auth/logout", 204, method="POST")
        client.call("me", 401)
    for identity in (tenant, other["tenant_id"]):
        admin.call(f"admin/tenants/{identity}", method="PATCH", payload={"enabled": False})
    admin.call("auth/logout", 204, method="POST")
    cleanup = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "multisearch-api",
            "python",
            "-m",
            "multisearch.keys",
            "user-disable",
            "--tenant",
            admin_tenant,
            "--email",
            f"{admin_tenant}@example.invalid",
        ],
        cwd=ROOT,
        capture_output=True,
        timeout=20,
        check=False,
    )
    require(cleanup.returncode == 0, "无法停用本次验收的管理员账号")
    print("通过会话、CSRF、角色隔离、密钥轮换与吊销、幂等额度计量、真实用量、分页与审计")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--console-url", type=loopback_url, default="http://127.0.0.1:3000")
    parser.add_argument("--api-url", type=loopback_url, help="直接验证 API，跳过 Console 代理")
    args = parser.parse_args()
    try:
        run(args.console_url, args.api_url)
    except SmokeFailure as error:
        raise SystemExit(str(error)) from None
