export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

export async function callApi<T>(
  path: string,
  options: { method?: "GET" | "POST" | "PATCH"; body?: unknown; csrf?: string; key?: string; signal?: AbortSignal } = {},
): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.csrf) headers["X-CSRF-Token"] = options.csrf;
  if (options.key) headers.Authorization = `Bearer ${options.key}`;
  const response = await fetch(`/gateway/${path}`, {
    method: options.method ?? "GET",
    headers,
    cache: "no-store",
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    signal: options.signal,
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({})) as { code?: string; message?: string };
    throw new ApiError(response.status, error.code ?? "HTTP_ERROR", error.message ?? "请求失败");
  }
  return (response.status === 204 ? undefined : await response.json()) as T;
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return "连接异常，请重试。";
}
