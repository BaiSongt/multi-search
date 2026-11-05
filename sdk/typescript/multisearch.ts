/** 使用原生 Fetch 的 MultiSearch REST 客户端。 */

export type Mode = "auto" | "local" | "quick" | "standard" | "evidence" | "deep";
export type SourceType = "web" | "news" | "academic" | "code" | "patent" | "local";
export type Modality = "text" | "image" | "video";
export type NetworkScope = "public" | "private" | "offline";
export type Availability = "available" | "unavailable";
export type JobStatus =
  | "queued" | "running" | "cancelling" | "completed" | "partial" | "failed" | "cancelled";
export type JsonObject = Record<string, unknown>;

export interface RequestMetadata {
  request_id?: string;
  idempotency_key?: string;
}

export interface ImageOptions {
  min_width?: number;
  min_height?: number;
  orientation?: "landscape" | "portrait" | "square";
  image_type?: "photo" | "illustration" | "transparent" | "icon";
  license_policy?: "any_with_status" | "declared_only" | "verified_only";
  source_page_required?: boolean;
}

export interface SearchRequest extends RequestMetadata {
  query: string;
  mode?: Mode;
  source_types?: SourceType[];
  modalities?: Modality[];
  language?: string;
  region?: string;
  time_range?: { from: string; to: string };
  max_results?: number;
  max_latency_ms?: number;
  max_pages?: number;
  max_search_rounds?: number;
  max_llm_tokens?: number;
  max_image_results?: number;
  max_image_metadata_fetches?: number;
  require_evidence?: boolean;
  image_options?: ImageOptions;
  network_scope?: NetworkScope;
  domain_allowlist?: string[];
  domain_denylist?: string[];
  cache_policy?: "use" | "refresh" | "bypass";
}

export type ResearchRequest = Omit<SearchRequest, "mode"> & { mode?: "auto" | "deep" };

interface FetchOptions extends RequestMetadata {
  max_latency_ms?: number;
  max_pages?: number;
  network_scope?: "public";
  domain_allowlist?: string[];
  domain_denylist?: string[];
}

export type FetchRequest = FetchOptions & ({ url: string; urls?: never } | { urls: string[]; url?: never });

export interface Capabilities {
  modes: Record<Mode, Availability>;
  source_types: Record<SourceType, Availability>;
  modalities: Record<Modality, Availability>;
  network_scopes: Record<NetworkScope, Availability>;
  combinations: { source_type: SourceType; modality: Modality }[];
  fetch: Availability;
  research: Availability;
  ocr: Availability;
  limits: JsonObject;
}

export interface ImageAttribution {
  original_url: string;
  thumbnail_url: string | null;
  source_page_url: string | null;
  source_site_name: string | null;
  source_site_domain: string | null;
  search_engines: string[];
  width: number | null;
  height: number | null;
  mime_type: string | null;
  creator: string | null;
  copyright_holder: string | null;
  license_name: string | null;
  license_url: string | null;
  attribution_text: string | null;
  rights_status: "unknown" | "declared_unverified" | "verified_license" | "conflict";
  rights_checked_at: string | null;
  rights_evidence_url: string | null;
  source_page_verified: boolean;
  source_page_status: "unverified" | "checked" | "invalid";
  source_checked_at: string | null;
  asset_hash: string | null;
  asset_hash_algorithm: "sha1" | null;
  asset_hash_source: "wikimedia_commons" | null;
}

interface SearchItemBase {
  result_id: string;
  title: string;
  url: string | null;
  canonical_url: string | null;
  source_type: SourceType;
  provider_ids: string[];
  authors: string[];
  doi: string | null;
  published_at: string | null;
  retrieved_at: string;
  content_type: string | null;
  language: string | null;
  snippet: string | null;
  relevance_score: number;
  evidence_status: "unverified" | "content_extracted" | "cross_checked" | "ocr_pending";
  evidence_refs: string[];
  warnings: string[];
}

export interface TextSearchItem extends SearchItemBase {
  kind: "text";
  journal: string | null;
  arxiv_id: string | null;
  version: string | null;
  license_name: string | null;
  license_url: string | null;
  repository_branch: string | null;
}

export interface ImageSearchItem extends SearchItemBase {
  kind: "image";
  image: ImageAttribution;
}

export type SearchItem = TextSearchItem | ImageSearchItem;

export interface SearchResponse {
  request_id: string;
  status: "completed" | "partial";
  mode_requested: Mode;
  mode_executed: Mode;
  partial: boolean;
  elapsed_ms: number;
  result_count: number;
  results: SearchItem[];
  evidence_records: JsonObject[];
  provider_runs: JsonObject[];
  budget: Record<string, number>;
  warnings: string[];
  routing_policy_version: number;
  next_cursor: string | null;
  cache_hit: boolean;
}

export interface JobAccepted {
  job_id: string;
  trace_id?: string | null;
  status: JobStatus;
  status_url: string;
  budget: Record<string, number>;
}

export interface JobView {
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
  progress: {
    stage: string;
    completed: number;
    total: number;
    pages_fetched: number;
    llm_tokens: number;
    research_budget: Record<string, number> | null;
  };
  result: JsonObject | null;
  error: { code: string; message: string } | null;
}

export interface ApiResult<T> {
  body: T;
  status: number;
  requestId: string | null;
  traceId: string | null;
}

export interface CallOptions {
  requestId?: string;
  idempotencyKey?: string;
  signal?: AbortSignal;
}

interface ResponseIds {
  status?: number;
  requestId?: string | null;
  traceId?: string | null;
}

interface ErrorDetails extends ResponseIds {
  retryable?: boolean;
  retryAfterMs?: number | null;
}

export class MultiSearchError extends Error {
  readonly code: string;
  readonly status: number | null;
  readonly requestId: string | null;
  readonly traceId: string | null;
  readonly retryable: boolean;
  readonly retryAfterMs: number | null;

  constructor(code: string, details: ErrorDetails = {}) {
    super(`MultiSearch ${code}${details.status === undefined ? "" : ` (HTTP ${details.status})`}`);
    this.name = "MultiSearchError";
    this.code = code;
    this.status = details.status ?? null;
    this.requestId = details.requestId ?? null;
    this.traceId = details.traceId ?? null;
    this.retryable = details.retryable ?? false;
    this.retryAfterMs = details.retryAfterMs ?? null;
  }
}

export interface ClientOptions {
  apiKey?: string;
  timeoutMs?: number;
  maxResponseBytes?: number;
  fetch?: typeof globalThis.fetch;
}

const CORRELATION = /^[A-Za-z0-9._-]{1,128}$/;
const TRACE = /^[a-f0-9]{32}$/i;
const ERROR_CODE = /^[A-Z][A-Z0-9_]{0,63}$/;
const BEARER = /^[A-Za-z0-9._~+/=-]{1,512}$/;
const UUID = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i;
const MAX_REQUEST_BYTES = 32_768;
const DEFAULT_RESPONSE_BYTES = 4 * 1024 * 1024;

function normalizedBase(value: string): string {
  try {
    if (/[\s\x00-\x1f\\?#]/u.test(value)) throw new Error();
    const url = new URL(value);
    const loopback = /^http:\/\/(?:127(?:\.\d{1,3}){3}|\[::1\])(?::\d+)?(?:\/|$)/i.test(value)
      && (url.hostname.startsWith("127.") || url.hostname === "[::1]");
    if (
      (url.protocol !== "https:" && !(url.protocol === "http:" && loopback)) ||
      !url.hostname || url.username || url.password || url.port === "0"
    ) throw new Error();
    const prefix = url.pathname.replace(/\/+$/, "");
    url.pathname = prefix.endsWith("/api/v1") ? prefix : `${prefix}/api/v1`;
    return url.toString();
  } catch {
    throw new TypeError("baseUrl requires HTTPS or a literal loopback HTTP address");
  }
}

function matching(value: unknown, pattern: RegExp): string | null {
  return typeof value === "string" && pattern.test(value) ? value : null;
}

function retryDelay(payload: JsonObject, header: string | null): number | null {
  const delay = payload.retry_after_ms;
  if (typeof delay === "number" && Number.isInteger(delay) && delay >= 0 && delay <= 2 ** 31 - 1) {
    return delay;
  }
  if (header === null || header.length > 64) return null;
  if (/^\d{1,7}$/.test(header)) return Number(header) * 1000;
  const date = Date.parse(header);
  return Number.isFinite(date) ? Math.max(0, date - Date.now()) : null;
}

function encodedBody(body: RequestMetadata, options: CallOptions): string {
  const payload = { ...body };
  const metadata = {
    request_id: options.requestId,
    idempotency_key: options.idempotencyKey,
  };
  for (const name of ["request_id", "idempotency_key"] as const) {
    const supplied = metadata[name];
    const current = payload[name];
    if (supplied !== undefined && current !== undefined && current !== supplied) {
      throw new TypeError(`Conflicting ${name} values`);
    }
    const value = supplied ?? current;
    if (value !== undefined && !matching(value, CORRELATION)) throw new TypeError(`Invalid ${name}`);
    if (supplied !== undefined) payload[name] = supplied;
  }
  const text = JSON.stringify(payload);
  if (new TextEncoder().encode(text).byteLength > MAX_REQUEST_BYTES) {
    throw new MultiSearchError("REQUEST_TOO_LARGE");
  }
  return text;
}

function jobPath(id: string): string {
  if (!UUID.test(id)) throw new TypeError("Invalid job ID");
  return `/jobs/${id.toLowerCase()}`;
}

async function readJson(response: Response, limit: number, ids: ResponseIds): Promise<JsonObject> {
  const contentLength = response.headers.get("content-length");
  if (contentLength !== null && /^\d+$/.test(contentLength) && Number(contentLength) > limit) {
    void response.body?.cancel().catch(() => {});
    throw new MultiSearchError("RESPONSE_TOO_LARGE", ids);
  }
  const reader = response.body?.getReader();
  if (!reader) throw new MultiSearchError("INVALID_RESPONSE", ids);
  const chunks: Uint8Array[] = [];
  let bytes = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      if (bytes > limit) {
        void reader.cancel().catch(() => {});
        throw new MultiSearchError("RESPONSE_TOO_LARGE", ids);
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const body = new Uint8Array(bytes);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    const payload: unknown = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(body));
    if (typeof payload !== "object" || payload === null || Array.isArray(payload)) throw new Error();
    return payload as JsonObject;
  } catch {
    throw new MultiSearchError("INVALID_RESPONSE", ids);
  }
}

export class MultiSearchClient {
  readonly #baseUrl: string;
  readonly #apiKey: string | undefined;
  readonly #timeoutMs: number;
  readonly #maxResponseBytes: number;
  readonly #fetch: typeof globalThis.fetch;

  constructor(baseUrl: string, options: ClientOptions = {}) {
    if (options.apiKey !== undefined && !matching(options.apiKey, BEARER)) {
      throw new TypeError("Invalid API Key");
    }
    const timeoutMs = options.timeoutMs ?? 30_000;
    if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 900_000) {
      throw new RangeError("timeoutMs must be in [1, 900000]");
    }
    const limit = options.maxResponseBytes ?? DEFAULT_RESPONSE_BYTES;
    if (!Number.isInteger(limit) || limit < 1 || limit > 16 * 1024 * 1024) {
      throw new RangeError("maxResponseBytes must be in [1, 16777216]");
    }
    this.#baseUrl = normalizedBase(baseUrl);
    this.#apiKey = options.apiKey;
    this.#timeoutMs = timeoutMs;
    this.#maxResponseBytes = limit;
    this.#fetch = options.fetch ?? globalThis.fetch.bind(globalThis);
  }

  async #request<T>(
    path: string, method: "GET" | "POST", key: string, expected: readonly number[],
    options: CallOptions = {}, body?: RequestMetadata, publicRoute = false,
  ): Promise<ApiResult<T>> {
    const headers = new Headers({ Accept: "application/json" });
    if (!publicRoute) {
      if (this.#apiKey === undefined) throw new MultiSearchError("API_KEY_REQUIRED");
      headers.set("Authorization", `Bearer ${this.#apiKey}`);
    }
    let data: string | undefined;
    if (body !== undefined) {
      headers.set("Content-Type", "application/json");
      data = encodedBody(body, options);
    }
    const timeout = AbortSignal.timeout(this.#timeoutMs);
    const signal = options.signal ? AbortSignal.any([timeout, options.signal]) : timeout;
    let ids: ResponseIds = {};
    try {
      const response = await this.#fetch(this.#baseUrl + path, {
        method, headers, body: data, signal, redirect: "manual", credentials: "omit", cache: "no-store",
      });
      ids = {
        status: response.status || undefined,
        requestId: matching(response.headers.get("x-request-id"), CORRELATION),
        traceId: matching(response.headers.get("x-trace-id"), TRACE),
      };
      if (
        response.type === "opaqueredirect" || response.redirected ||
        (response.status >= 300 && response.status < 400)
      ) {
        void response.body?.cancel().catch(() => {});
        throw new MultiSearchError("REDIRECT_BLOCKED", ids);
      }
      let payload: JsonObject;
      try {
        payload = await readJson(response, this.#maxResponseBytes, ids);
      } catch (error) {
        if (error instanceof MultiSearchError && error.code === "INVALID_RESPONSE" && !response.ok) {
          throw new MultiSearchError("HTTP_ERROR", {
            ...ids,
            retryable: response.status === 429 || response.status >= 500,
            retryAfterMs: retryDelay({}, response.headers.get("retry-after")),
          });
        }
        throw error;
      }
      ids.requestId ??= matching(payload.request_id, CORRELATION);
      if (!response.ok) {
        throw new MultiSearchError(matching(payload.code, ERROR_CODE) ?? "HTTP_ERROR", {
          ...ids,
          retryable: typeof payload.retryable === "boolean"
            ? payload.retryable : response.status === 429 || response.status >= 500,
          retryAfterMs: retryDelay(payload, response.headers.get("retry-after")),
        });
      }
      if (!expected.includes(response.status) || !Object.hasOwn(payload, response.status === 202 ? "job_id" : key)) {
        throw new MultiSearchError("INVALID_RESPONSE", ids);
      }
      return {
        body: payload as T,
        status: response.status,
        requestId: ids.requestId ?? null,
        traceId: ids.traceId ?? null,
      };
    } catch (error) {
      if (error instanceof MultiSearchError) throw error;
      if (options.signal?.aborted) throw new MultiSearchError("ABORTED", ids);
      if (timeout.aborted) throw new MultiSearchError("TIMEOUT", { ...ids, retryable: true });
      throw new MultiSearchError("TRANSPORT_ERROR", { ...ids, retryable: true });
    }
  }

  capabilities(options: Pick<CallOptions, "signal"> = {}): Promise<ApiResult<Capabilities>> {
    return this.#request("/capabilities", "GET", "modes", [200], options, undefined, true);
  }

  search(request: SearchRequest, options?: CallOptions): Promise<ApiResult<SearchResponse | JobAccepted>> {
    return this.#request("/search", "POST", "results", [200, 202], options, request);
  }

  fetch(request: FetchRequest, options?: CallOptions): Promise<ApiResult<JobAccepted>> {
    return this.#request("/fetch", "POST", "job_id", [202], options, request);
  }

  research(request: ResearchRequest, options?: CallOptions): Promise<ApiResult<JobAccepted>> {
    return this.#request("/research", "POST", "job_id", [202], options, request);
  }

  getJob(id: string, options: Pick<CallOptions, "signal"> = {}): Promise<ApiResult<JobView>> {
    return this.#request(jobPath(id), "GET", "job_id", [200], options);
  }

  cancelJob(id: string, options: Pick<CallOptions, "signal"> = {}): Promise<ApiResult<JobView>> {
    return this.#request(`${jobPath(id)}/cancel`, "POST", "job_id", [200], options);
  }
}
