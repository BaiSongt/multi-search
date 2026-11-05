"use client";

import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { ApiError, callApi, errorMessage } from "./lib/api";

const numbers = new Intl.NumberFormat("zh-HK", { maximumFractionDigits: 2 });

export function formatNumber(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : numbers.format(value);
}

export function dateTime(value: string | null): string {
  return value ? new Date(value).toLocaleString("zh-HK", { hour12: false }) : "—";
}

export function safeLink(value: string | null | undefined, { thumbnail = false } = {}): string | null {
  if (!value || /[\u0000-\u0020\u007f\\]/.test(value)) return null;
  try {
    const url = new URL(value);
    const host = url.hostname.toLowerCase().replace(/\.$/, "");
    if (
      !["https:", "http:"].includes(url.protocol) || url.username || url.password ||
      host.includes(":") || !host.includes(".") || /^\d+(?:\.\d+){3}$/.test(host) ||
      /\.(local|localhost|internal|lan|invalid|test|onion)$/.test(host)
    ) return null;
    if (thumbnail && url.protocol !== "https:") return null;
    return url.href;
  } catch {
    return null;
  }
}

export function ExternalLink({ href, children }: { href?: string | null; children: ReactNode }) {
  const link = safeLink(href);
  return link
    ? <a href={link} target="_blank" rel="noopener noreferrer nofollow">{children}</a>
    : <span className="muted">{typeof children === "string" && !href ? children : "链接不可用"}</span>;
}

export function ThemeToggle() {
  const [theme, setTheme] = useState<"light" | "dark" | "system">("system");
  useEffect(() => {
    const value = document.documentElement.dataset.theme;
    if (value === "light" || value === "dark") setTheme(value);
  }, []);

  function toggle() {
    const currentDark = theme === "dark" || (theme === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
    const next = currentDark ? "light" : "dark";
    setTheme(next);
    document.documentElement.dataset.theme = next;
    document.cookie = `multisearch_theme=${next}; Path=/; Max-Age=31536000; SameSite=Lax${location.protocol === "https:" ? "; Secure" : ""}`;
  }
  return <button type="button" className="quiet" onClick={toggle} aria-label="切换浅色或深色主题">◐ <span className="desktop-only">主题</span></button>;
}

export function Empty({ children = "暂无数据。" }: { children?: ReactNode }) {
  return <div className="empty" role="status">{children}</div>;
}

export function Loading() {
  return <div className="loading" role="status"><span className="skeleton" aria-hidden />正在读取数据…</div>;
}

export function Failure({ reason, retry }: { reason: unknown; retry?: () => void }) {
  return (
    <div className="notice danger" role="alert">
      <p>{errorMessage(reason)}</p>
      {reason instanceof ApiError && reason.status === 401
        ? <a href="/auth">重新登录</a>
        : retry && <button type="button" onClick={retry}>重试</button>}
    </div>
  );
}

export function useResource<T>(path: string) {
  const [revision, setRevision] = useState(0);
  const [result, setResult] = useState<{ path: string; revision: number; data?: T; error?: unknown }>();
  useEffect(() => {
    const abort = new AbortController();
    void callApi<T>(path, { signal: abort.signal }).then(
      (data) => { if (!abort.signal.aborted) setResult({ path, revision, data }); },
      (error: unknown) => { if (!abort.signal.aborted) setResult({ path, revision, error }); },
    );
    return () => abort.abort();
  }, [path, revision]);

  const current = result?.path === path && result.revision === revision ? result : undefined;
  return {
    data: current?.data,
    error: current?.error,
    pending: current === undefined,
    reload: () => setRevision((previous) => previous + 1),
  };
}

export function usePagination() {
  const [trail, setTrail] = useState<string[]>([]);
  return {
    cursor: trail.at(-1),
    hasPrevious: trail.length > 0,
    next: (cursor: string) => setTrail((current) => [...current, cursor]),
    previous: () => setTrail((current) => current.slice(0, -1)),
    reset: () => setTrail([]),
  };
}

export function Pagination({ next, pager }: { next: string | null; pager: ReturnType<typeof usePagination> }) {
  if (!next && !pager.hasPrevious) return null;
  return (
    <nav className="actions pagination" aria-label="分页">
      <button type="button" disabled={!pager.hasPrevious} onClick={pager.previous}>上一页</button>
      <button type="button" disabled={!next} onClick={() => next && pager.next(next)}>下一页</button>
    </nav>
  );
}

export function Table({ headers, children, caption }: { headers: string[]; children: ReactNode; caption: string }) {
  const [compact, setCompact] = useState(true);
  return (
    <div className="table-wrap">
      <div className="table-tools">
        <span className="muted">{caption}</span>
        <button type="button" className="quiet" aria-pressed={compact} onClick={() => setCompact(!compact)}>
          {compact ? "紧凑行距" : "宽松行距"}
        </button>
      </div>
      <div className="table-scroll" tabIndex={0} role="region" aria-label={caption}>
        <table className={compact ? "compact" : undefined}>
          <thead><tr>{headers.map((header) => <th key={header} scope="col">{header}</th>)}</tr></thead>
          <tbody>{children}</tbody>
        </table>
      </div>
    </div>
  );
}

export function Copy({ value, label = "复制" }: { value: string; label?: string }) {
  const [feedback, setFeedback] = useState("");
  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setFeedback("已复制");
    } catch {
      setFeedback("复制失败，可手动选取");
    }
  }
  return (
    <span className="copy">
      <button type="button" className="quiet" onClick={() => void copy()}>{label}</button>
      <span role="status" className="muted">{feedback}</span>
    </span>
  );
}

export function CopyId({ value }: { value: string | null }) {
  return value
    ? <span className="copy-id"><code title={value}>{value}</code><Copy value={value} label="复制 ID" /></span>
    : <span className="muted">—</span>;
}

export function Dialog({ title, children, close, pending = false }: { title: string; children: ReactNode; close: () => void; pending?: boolean }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const label = useId();
  useEffect(() => {
    const node = dialog.current;
    if (node && !node.open) node.showModal();
  }, []);
  return (
    <dialog ref={dialog} aria-labelledby={label} onClose={close} onCancel={(event) => { if (pending) event.preventDefault(); }}>
      <div className="dialog-header">
        <h2 id={label}>{title}</h2>
        <button type="button" className="quiet" aria-label="关闭对话框" disabled={pending} onClick={() => dialog.current?.close()}>关闭</button>
      </div>
      <div className="dialog-body">{children}</div>
    </dialog>
  );
}
