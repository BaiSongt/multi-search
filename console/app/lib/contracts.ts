export type Role = "developer" | "tenant_admin" | "operations_admin" | "platform_admin" | "auditor";
export type Workspace = "console" | "admin";

export type Session = {
  tenant_id: string;
  tenant: string;
  user_id: string;
  email: string;
  role: Role;
  expires_at: string;
  csrf_token: string;
};

export const SESSION_COOKIE = "multisearch_session";
export const roleLabels: Record<Role, string> = {
  developer: "开发者",
  tenant_admin: "租户管理员",
  operations_admin: "运维管理员",
  platform_admin: "平台管理员",
  auditor: "审计员",
};

export const consoleNav = {
  overview: "我的概览",
  keys: "API 密钥",
  usage: "调用量与额度",
  playground: "Playground",
  images: "图片检索",
  docs: "API 文档",
  settings: "账户设置",
};

export const adminNav = {
  overview: "服务总览",
  providers: "Provider 管理",
  requests: "请求追踪",
  tenants: "租户管理",
  quotas: "额度与限流",
  audit: "审计与安全",
};

export function canView(role: Role, workspace: Workspace, section: string) {
  if (workspace === "console") {
    if (role === "auditor") return section === "settings";
    return section !== "keys" || role !== "operations_admin";
  }
  if (role === "platform_admin") return true;
  if (role === "operations_admin") return ["overview", "providers", "requests", "audit"].includes(section);
  return (role === "auditor" || role === "tenant_admin") && section === "audit";
}

export function workspaceHome(role: Role) {
  if (role === "auditor") return "/admin/audit";
  if (role === "platform_admin" || role === "operations_admin") return "/admin/overview";
  return "/console/overview";
}

export function workspaceTarget(value: string | undefined): string | undefined {
  const match = /^\/(console|admin)\/([a-z]+)$/.exec(value ?? "");
  if (!match) return undefined;
  const nav = match[1] === "console" ? consoleNav : adminNav;
  return Object.hasOwn(nav, match[2]) ? value : undefined;
}

export type KeyRow = {
  id: string;
  name: string;
  environment: "dev" | "test" | "prod";
  prefix: string;
  suffix: string;
  scopes: string[];
  created_at: string;
  expires_at: string;
  revoked_at: string | null;
  last_used_at: string | null;
  status: "active" | "expired" | "revoked" | "rotating";
  limits: Record<string, number>;
};

export type KeyPage = { keys: KeyRow[]; next_cursor: string | null };
export type IssuedKey = { key: KeyRow; secret: string };

export const quotaLabels: Record<string, string> = {
  requests_per_minute: "每分钟请求",
  requests_per_day: "每日请求",
  requests_per_month: "每月请求",
  concurrency: "并发数",
  text_per_day: "每日文字请求",
  image_per_day: "每日图片请求",
  local_per_day: "每日缓存请求",
  quick_per_day: "每日快速检索",
  standard_per_day: "每日标准检索",
  evidence_per_day: "每日证据检索",
  deep_per_day: "每日深度检索",
};

export type RequestRow = {
  id: string;
  request_id: string;
  tenant_id: string;
  key_id: string | null;
  mode: string;
  modalities: string[];
  source_types: string[];
  status: string;
  code: string | null;
  elapsed_ms: number | null;
  cache_hit: boolean;
  text_results_returned: number;
  image_results_returned: number;
  created_at: string;
};

export type Totals = {
  search_requests: number;
  text_results_returned: number;
  image_results_returned: number;
  pages_fetched: number;
  research_jobs_started: number;
  llm_tokens: number;
  errors: number;
};

export type UsageDay = Pick<Totals, "search_requests" | "errors" | "text_results_returned" | "image_results_returned"> & { day: string };
export type Usage = { totals: Totals; quota: Record<string, number>; series: UsageDay[]; recent: RequestRow[]; window_days: number };
export type Overview = {
  totals: Totals;
  series: UsageDay[];
  requests_per_second: number | null;
  success_rate: number | null;
  p50_ms: number | null;
  p95_ms: number | null;
  image_source_coverage: number | null;
  active_jobs: number | null;
};

export type Provider = {
  provider_id: string;
  enabled: boolean;
  priority: number;
  requests_per_minute: number;
  status: string;
  last_status: string | null;
  last_elapsed_ms: number | null;
  network_scope: string;
  pair_health: { source_type: string; modality: string; status: string; last_elapsed_ms: number | null }[];
  usage: { runs: number; errors: number; rate_limited: number; p50_ms: number | null; p95_ms: number | null };
};

export type Tenant = { id: string; slug: string; enabled: boolean; quota: Record<string, number>; search_requests: number };
export type Audit = { id: string; tenant_id: string; actor_type: string; actor_id: string | null; action: string; target_id: string | null; details: unknown; occurred_at: string };
export type Page<T> = { items: T[]; next_cursor: string | null };

export type RightsStatus = "unknown" | "declared_unverified" | "verified_license" | "conflict";
export type Attribution = {
  original_url: string;
  thumbnail_url: string | null;
  source_page_url: string | null;
  source_site_name: string | null;
  source_site_domain: string | null;
  search_engines: string[];
  width: number | null;
  height: number | null;
  creator: string | null;
  copyright_holder: string | null;
  license_name: string | null;
  license_url: string | null;
  attribution_text: string | null;
  rights_status: RightsStatus;
  rights_checked_at: string | null;
  rights_evidence_url: string | null;
  source_page_verified: boolean;
};

type ResultBase = {
  result_id: string;
  title: string;
  url: string | null;
  source_type: string;
  provider_ids: string[];
  retrieved_at: string;
  snippet: string | null;
  warnings: string[];
};

export type SearchResult = (ResultBase & { kind: "text" }) | (ResultBase & { kind: "image"; image: Attribution });
export type ImageResult = Extract<SearchResult, { kind: "image" }>;
export type SearchResponse = {
  request_id: string;
  mode_executed: string;
  partial: boolean;
  elapsed_ms: number;
  result_count: number;
  results: SearchResult[];
  warnings: string[];
  cache_hit: boolean;
};
