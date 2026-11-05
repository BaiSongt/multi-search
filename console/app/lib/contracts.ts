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
  team: "团队与权限",
  settings: "账户设置",
};

export const adminNav = {
  overview: "服务总览",
  providers: "Provider 管理",
  requests: "请求追踪",
  jobs: "任务与队列",
  routing: "路由与策略",
  images: "图片来源质量",
  tenants: "租户管理",
  quotas: "额度与限流",
  audit: "审计与安全",
  cache: "缓存与存储",
  settings: "系统设置",
};

export function canView(role: Role, workspace: Workspace, section: string) {
  if (workspace === "console") {
    if (role === "auditor") return section === "settings";
    if (section === "team") return role === "tenant_admin" || role === "platform_admin";
    return section !== "keys" || role !== "operations_admin";
  }
  if (role === "platform_admin") return true;
  if (role === "operations_admin") return ["overview", "providers", "requests", "jobs", "routing", "images", "cache", "audit", "settings"].includes(section);
  return (role === "auditor" || role === "tenant_admin") && section === "audit";
}

export function workspaceHome(role: Role) {
  if (role === "auditor") return "/admin/audit";
  if (role === "platform_admin" || role === "operations_admin") return "/admin/overview";
  return "/console/overview";
}

export function workspaceTarget(value: string | undefined): string | undefined {
  if (value && /^\/admin\/tenants\/[a-f0-9-]{32,36}$/i.test(value)) return value;
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
  trace_id?: string | null;
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

export type RequestTrace = RequestRow & {
  provider_runs: {
    provider_id: string;
    status: string;
    elapsed_ms: number;
    count: number;
    reason_codes: string[];
  }[];
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

type ProviderLocaleFilter = "unsupported" | "upstream_best_effort" | "upstream_requires_language" | "upstream_requires_region";
type ProviderDateFilter = "metadata_postfilter" | "upstream_and_metadata_postfilter";
export type ProviderFilters = {
  language: ProviderLocaleFilter;
  region: ProviderLocaleFilter;
  date_range: ProviderDateFilter;
  date_field: "published_at" | "submitted_at" | "created_at";
  exact_identifier_date_range: ProviderDateFilter | null;
  image_options: {
    upstream: string[];
    candidate_postfilter: string[];
    platform_postfilter: string[];
    unsupported: string[];
  } | null;
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
  pair_health: { source_type: string; modality: string; status: string; last_elapsed_ms: number | null; filters?: ProviderFilters | null }[];
  capabilities?: {
    source_types: string[];
    modalities: string[];
    supports_pagination: boolean | null;
    requires_api_key: boolean | null;
    max_concurrent_requests: number;
    max_upstream_concurrent_requests: number | null;
  };
  usage: { runs: number; errors: number; rate_limited: number; p50_ms: number | null; p95_ms: number | null };
};

export type Tenant = { id: string; slug: string; enabled: boolean; quota: Record<string, number>; search_requests: number };

type QuotaUsageCounts = {
  requests: number;
  search_requests: number;
  image_requests: number;
  image_results_returned: number;
  fetch_requests: number;
  pages_fetched: number;
  active_executions: number;
};

export type TenantQuotaUsage = {
  day: string;
  observed_at: string;
  quota: Record<string, number>;
  usage: QuotaUsageCounts;
  keys: Page<{ key_mask: string; usage: QuotaUsageCounts; quota: Record<string, number> }>;
};
export type Audit = { id: string; tenant_id: string; actor_type: string; actor_id: string | null; action: string; target_id: string | null; details: unknown; occurred_at: string };
export type Page<T> = { items: T[]; next_cursor: string | null };

export type Member = { user_id: string; email: string; role: Role; enabled: boolean; account_enabled: boolean; has_password: boolean };
export type Invitation = { id: string; email: string; role: Role; status: string; created_at: string; expires_at: string; tenant_id: string };
export type IssuedInvitation = { invitation: Invitation; token: string };

export type JobStatus = "queued" | "running" | "cancelling" | "completed" | "partial" | "failed" | "cancelled";
export type ResearchBudget = {
  max_latency_ms: number;
  max_search_rounds: number;
  max_pages: number;
  max_results: number;
  max_image_results: number;
  max_image_metadata_fetches: number;
  max_llm_tokens: number;
};

export type JobAccepted = { job_id: string; status: JobStatus; status_url: string; budget: ResearchBudget; trace_id?: string | null };

export type Job = {
  job_id: string;
  request_id?: string | null;
  trace_id?: string | null;
  tenant_id: string;
  kind: "fetch" | "research";
  status: JobStatus;
  created_at: string;
  updated_at: string;
  deadline_at: string;
  attempts: number;
  max_attempts: number;
  progress: { stage: string; completed: number; total: number; pages_fetched?: number };
  result: unknown;
  error: { code: string; message: string } | null;
};

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
  evidence_status?: "unverified" | "content_extracted" | "cross_checked" | "ocr_pending";
};

export type SearchResult = (ResultBase & { kind: "text" }) | (ResultBase & { kind: "image"; image: Attribution });
export type ImageResult = Extract<SearchResult, { kind: "image" }>;
export type ImageReportRow = {
  id: string;
  request_id: string;
  tenant_id: string;
  source_page_url: string | null;
  original_url: string;
  thumbnail_url?: string | null;
  rights_status: RightsStatus;
  source_page_verified: boolean;
  source_page_status?: string | null;
  last_check_at?: string | null;
  retrieved_at: string;
};

export type ImageReport = Page<ImageReportRow> & { counts: Record<string, number> };
export type EvidenceRecord = {
  evidence_id: string;
  result_id: string | null;
  origin_url: string;
  canonical_url: string;
  document_identifier: { type: string; value: string } | null;
  retrieved_at: string;
  published_at: string | null;
  title: string | null;
  content_type: string;
  evidence_text: string;
  locator: { kind: string; section: string | null; text_offset_start: number; text_offset_end: number; page_number: number | null };
  content_sha256: string;
  extraction_method: string;
  extraction_version: string;
  verification_status: string;
  access_policy: string;
};

export type ResearchResult = {
  mode: "deep";
  summary: string;
  planned_rounds: number;
  rounds: {
    number: number;
    step: string;
    mode: "standard" | "evidence";
    status: "completed" | "partial" | "failed" | "interrupted";
    elapsed_ms: number;
    result_count: number;
    evidence_refs: string[];
    error_code: string | null;
  }[];
  results: SearchResult[];
  evidence_records: EvidenceRecord[];
  findings: { title: string | null; excerpt: string; evidence_refs: string[]; origin_url: string; verification_status: "content_extracted" }[];
  comparisons: { round_number: number; evidence_refs: string[]; source_hosts: string[]; assessment: "source_independence_pending" }[];
  model_findings: { claim: string; evidence_refs: string[]; verification_status: "model_synthesis_unverified" }[];
  budget: ResearchBudget;
  usage: {
    search_rounds: number;
    pages_attempted: number | null;
    pages_charged: number;
    pages_fetched: number;
    image_metadata_fetches: number | null;
    image_metadata_charged: number;
    llm_tokens: number | null;
    llm_tokens_reserved: number;
  };
  warnings: string[];
};

export type ResearchJobView = Omit<Job, "kind" | "result"> & { kind: "research"; result: ResearchResult | null };

export type Capabilities = {
  fetch?: string;
  research?: string;
  modes: Record<string, string>;
  source_types: Record<string, string>;
  modalities: Record<string, string>;
  network_scopes: Record<string, string>;
  combinations: { source_type: string; modality: string }[];
  limits: { modes: Record<string, { max_latency_ms: number; max_results: number }>; max_image_metadata_fetches: number };
};

export type SearchResponse = {
  request_id: string;
  mode_executed: string;
  partial: boolean;
  elapsed_ms: number;
  result_count: number;
  results: SearchResult[];
  warnings: string[];
  cache_hit: boolean;
  evidence_records?: EvidenceRecord[];
};
