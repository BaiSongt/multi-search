import { cookies } from "next/headers";
import { SESSION_COOKIE, type Session } from "./contracts";

export function apiUrl(path: string): URL {
  const base = process.env.MULTISEARCH_INTERNAL_API_URL ?? "http://127.0.0.1:8765";
  return new URL(path, base);
}

export function publicApiUrl(path: string): string {
  const base = process.env.MULTISEARCH_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8765";
  return new URL(path, base).href;
}

export type SessionState = { session: Session; error?: never } | { session?: never; error: "anonymous" | "unavailable" };

export async function readSession(): Promise<SessionState> {
  const session = (await cookies()).get(SESSION_COOKIE);
  if (!session) return { error: "anonymous" };
  try {
    const response = await fetch(apiUrl("/api/v1/me"), {
      cache: "no-store",
      redirect: "error",
      headers: { cookie: `${SESSION_COOKIE}=${encodeURIComponent(session.value)}` },
      signal: AbortSignal.timeout(3000),
    });
    if (response.status === 401 || response.status === 403) return { error: "anonymous" };
    if (!response.ok) return { error: "unavailable" };
    return { session: await response.json() as Session };
  } catch {
    return { error: "unavailable" };
  }
}
