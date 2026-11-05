import type { NextRequest } from "next/server";
import { apiUrl } from "../../lib/api-server";
import { SESSION_COOKIE } from "../../lib/contracts";

export const dynamic = "force-dynamic";

const GET_ROUTES = new Set([
  "me", "keys", "usage", "capabilities", "providers", "admin/overview", "admin/providers",
  "admin/requests", "admin/tenants", "admin/audit", "admin/jobs", "admin/images", "admin/routing",
  "admin/cache", "admin/settings",
]);
const POST_ROUTES = new Set([
  "auth/login", "auth/refresh", "auth/logout", "auth/invitations/accept", "keys", "playground", "search", "admin/tenants",
  "admin/routing/test", "admin/routing/publish", "admin/routing/rollback", "admin/cache/invalidate",
]);
const KEY_ACTION = /^keys\/[a-f0-9-]{32,36}\/(rotate|revoke)$/i;
const JOB_VIEW = /^admin\/jobs\/[a-f0-9-]{32,36}$/i;
const JOB_CANCEL = /^admin\/jobs\/[a-f0-9-]{32,36}\/cancel$/i;
const CONSOLE_JOB = "console/jobs/[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}";
const CONSOLE_JOB_VIEW = new RegExp(`^${CONSOLE_JOB}$`, "i");
const CONSOLE_JOB_CANCEL = new RegExp(`^${CONSOLE_JOB}/cancel$`, "i");
const ADMIN_UPDATE = /^admin\/(?:providers\/[a-z0-9_-]{1,64}|tenants\/[a-f0-9-]{32,36}(?:\/quota)?)$/i;
const TEAM_READ = /^(?:team|admin\/tenants\/[a-f0-9-]{32,36})\/(?:members|invitations)$/i;
const TEAM_WRITE = /^(?:team|admin\/tenants\/[a-f0-9-]{32,36})\/invitations(?:\/[a-f0-9-]{32,36}\/revoke)?$/i;
const MEMBER_UPDATE = /^(?:team|admin\/tenants\/[a-f0-9-]{32,36})\/members\/[a-f0-9-]{32,36}$/i;
const BODY_LIMIT = 64 * 1024;

function accepted(method: string, path: string) {
  if (method === "GET") return GET_ROUTES.has(path) || JOB_VIEW.test(path) || CONSOLE_JOB_VIEW.test(path) || TEAM_READ.test(path);
  if (method === "POST") return POST_ROUTES.has(path) || KEY_ACTION.test(path) || JOB_CANCEL.test(path) || TEAM_WRITE.test(path)
    || CONSOLE_JOB_CANCEL.test(path);
  return method === "PATCH" && (ADMIN_UPDATE.test(path) || MEMBER_UPDATE.test(path));
}

function failure(status: number, code: string, message: string) {
  return Response.json({ code, message }, { status, headers: { "Cache-Control": "no-store" } });
}

async function boundedBody(request: NextRequest): Promise<ArrayBuffer | null> {
  if (!request.body) return new ArrayBuffer(0);
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      length += value.byteLength;
      if (length > BODY_LIMIT) {
        await reader.cancel();
        return null;
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const body = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return body.buffer;
}

async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  // 只转发固定管理和数据面路径，每个路径段必须匹配安全字符。
  if (!path.every((segment) => /^[a-z0-9_-]+$/i.test(segment)) || !accepted(request.method, path.join("/"))) {
    return failure(404, "HTTP_ERROR", "接口不存在");
  }
  const headers = new Headers();
  for (const name of ["content-type", "accept", "origin", "x-csrf-token", "authorization"]) {
    const value = request.headers.get(name);
    if (value !== null) headers.set(name, value);
  }
  const session = request.cookies.get(SESSION_COOKIE);
  if (session) headers.set("cookie", `${SESSION_COOKIE}=${encodeURIComponent(session.value)}`);
  try {
    const body = request.method === "GET" ? undefined : await boundedBody(request);
    if (body === null) return failure(413, "INVALID_REQUEST", "请求体超过 64 KiB");
    const target = apiUrl(`/api/v1/${path.join("/")}`);
    target.search = request.nextUrl.search;
    const upstream = await fetch(target, {
      method: request.method,
      headers,
      body,
      redirect: "error",
      cache: "no-store",
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(path.join("/") === "playground" || path.join("/") === "search" ? 65_000 : 25_000)]),
    });
    const output = new Headers({ "Cache-Control": "no-store" });
    for (const name of ["content-type", "x-request-id", "x-trace-id", "retry-after", "www-authenticate"]) {
      const value = upstream.headers.get(name);
      if (value !== null) output.set(name, value);
    }
    for (const cookie of upstream.headers.getSetCookie()) output.append("set-cookie", cookie);
    return new Response(upstream.body, { status: upstream.status, headers: output });
  } catch {
    return failure(502, "GATEWAY_UNAVAILABLE", "暂时无法连接 MultiSearch API");
  }
}

export { proxy as GET, proxy as POST, proxy as PATCH };
