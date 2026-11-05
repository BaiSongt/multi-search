"use client";

import { useEffect, useState, type FormEvent } from "react";
import { callApi } from "./lib/api";
import { quotaLabels, type RequestRow, type RequestTrace, type Totals, type Usage, type UsageDay } from "./lib/contracts";
import { downloadCsv } from "./lib/csv";
import { CopyId, dateTime, Dialog, Empty, Failure, formatNumber, Loading, Table, useResource } from "./ui";

const statusTitles: Record<string, string> = {
  running: "处理中",
  completed: "完成",
  partial: "部分完成",
  failed: "失败",
  cancelled: "取消",
};

const modeTitles: Record<string, string> = {
  auto: "自动",
  local: "本地",
  quick: "快速",
  standard: "标准",
  evidence: "证据检索",
  deep: "深度研究",
};

const providerStatuses: Record<string, string> = {
  success: "成功",
  empty: "无结果",
  partial: "部分完成",
  failed: "失败",
  timeout: "超时",
  rate_limited: "限流",
  circuit_open: "熔断",
};

const providerReasons: Record<string, string> = {
  upstream_captcha: "上游验证码",
  upstream_rate_limited: "上游限流",
  upstream_timeout: "上游超时",
  upstream_unavailable: "上游不可用",
  local_provider_queue_timeout: "本地来源并发等待超时",
  local_provider_rate_limited: "本地来源配额已满",
};

export function Metrics({ values }: { values: [string, number | null | undefined, string?][] }) {
  return (
    <div className="metrics">
      {values.map(([label, value, unit]) => (
        <section className="metric" key={label}><h2 className="muted">{label}</h2><strong>{formatNumber(value)}{value != null && unit ? ` ${unit}` : ""}</strong></section>
      ))}
    </div>
  );
}

export function resourceMetrics(totals: Totals): [string, number, string][] {
  return [
    ["请求数", totals.search_requests, "次"],
    ["错误数", totals.errors, "次"],
    ["文字结果", totals.text_results_returned, "条"],
    ["图片结果", totals.image_results_returned, "项"],
    ["抓取页数", totals.pages_fetched, "页"],
    ["研究任务", totals.research_jobs_started, "个"],
    ["模型用量", totals.llm_tokens, "tokens"],
  ];
}

export function Trend({ series }: { series: UsageDay[] }) {
  const [metric, setMetric] = useState<"search_requests" | "errors" | "text_results_returned" | "image_results_returned">("search_requests");
  const highest = Math.max(1, ...series.map((point) => point[metric]));
  return (
    <section className="panel">
      <div className="table-tools">
        <h2>每日趋势 · UTC</h2>
        <select aria-label="趋势指标" value={metric} onChange={(event) => setMetric(event.target.value as typeof metric)}>
          <option value="search_requests">请求次数</option><option value="errors">失败次数</option>
          <option value="text_results_returned">文字结果</option><option value="image_results_returned">图片结果</option>
        </select>
      </div>
      {series.length === 0 ? <Empty>暂无可绘制的用量记录。</Empty> : (
        <div className="chart" aria-label="日用量趋势">
          {series.map((point) => (
            <div className="chart-row" key={point.day}>
              <time dateTime={point.day}>{point.day.slice(5)}</time>
              <meter min={0} max={highest} value={point[metric]} aria-label={`${point.day} · ${point[metric]}`} />
              <span>{formatNumber(point[metric])}</span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

export function QuotaSummary({ quota }: { quota: Record<string, number> }) {
  const entries = Object.entries(quota);
  return entries.length ? (
    <dl>{entries.map(([name, value]) => <div key={name} className="definition"><dt>{quotaLabels[name] ?? name}</dt><dd>{formatNumber(value)}</dd></div>)}</dl>
  ) : <p className="muted">使用部署默认额度。</p>;
}

function requestStatus(row: RequestRow) {
  return <span className={`badge ${row.status === "failed" ? "danger" : row.status === "partial" ? "warning" : row.status === "completed" ? "positive" : ""}`}>{statusTitles[row.status] ?? row.status}</span>;
}

function RequestFields({ row }: { row: RequestRow }) {
  return (
    <dl>
      <dt>请求 ID</dt><dd><CopyId value={row.request_id} /></dd>
      {row.trace_id && <><dt>追踪 ID</dt><dd><CopyId value={row.trace_id} /></dd></>}
      <dt>记录 ID</dt><dd><CopyId value={row.id} /></dd>
      <dt>租户 ID</dt><dd><CopyId value={row.tenant_id} /></dd>
      <dt>密钥 ID</dt><dd><CopyId value={row.key_id} /></dd>
      <dt>发生时间</dt><dd>{dateTime(row.created_at)}</dd>
      <dt>状态</dt><dd>{requestStatus(row)}</dd>
      <dt>错误码</dt><dd>{row.code ?? "—"}</dd>
      <dt>耗时</dt><dd>{row.elapsed_ms === null ? "—" : `${row.elapsed_ms} ms`}</dd>
      <dt>缓存命中</dt><dd>{row.cache_hit ? "是" : "否"}</dd>
      <dt>文字结果</dt><dd>{formatNumber(row.text_results_returned)}</dd>
      <dt>图片结果</dt><dd>{formatNumber(row.image_results_returned)}</dd>
    </dl>
  );
}

function QueryDisclosure({ row, csrf }: { row: RequestRow; csrf: string }) {
  const [confirming, setConfirming] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>();
  const [retained, setRetained] = useState<{ query: string; expires_at: string }>();

  useEffect(() => {
    if (!retained) return;
    const delay = Math.min(60_000, Math.max(0, Date.parse(retained.expires_at) - Date.now()));
    const timer = window.setTimeout(() => setRetained(undefined), delay);
    return () => window.clearTimeout(timer);
  }, [retained]);

  async function reveal() {
    setPending(true);
    setError(undefined);
    try {
      const value = await callApi<{ query: string; expires_at: string }>(
        `admin/requests/${encodeURIComponent(row.id)}/query`,
        { method: "POST", csrf, body: { tenant_id: row.tenant_id, confirm: true } },
      );
      setRetained(value);
      setConfirming(false);
    } catch (failure) {
      setError(failure);
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="stack" aria-label="查询原文">
      {retained ? <>
        <pre className="notice warning">{retained.query}</pre>
        <button type="button" onClick={() => setRetained(undefined)}>隐藏查询原文</button>
      </> : confirming ? <>
        <p className="notice warning">查询原文可能包含敏感信息。查看操作将写入审计，并在此页面短暂显示。</p>
        <div className="actions">
          <button type="button" disabled={pending} onClick={() => void reveal()}>{pending ? "正在查看…" : "确认查看原文"}</button>
          <button type="button" disabled={pending} onClick={() => { setConfirming(false); setError(undefined); }}>返回</button>
        </div>
        {error !== undefined && <Failure reason={error} />}
      </> : <button type="button" onClick={() => setConfirming(true)}>查看查询原文</button>}
    </section>
  );
}

function AdminRequestTrace({ row, queryCsrf }: { row: RequestRow; queryCsrf?: string }) {
  const resource = useResource<RequestTrace>(`admin/requests/${encodeURIComponent(row.id)}`);
  const runs = resource.data?.provider_runs;
  return (
    <>
      <RequestFields row={resource.data ?? row} />
      {queryCsrf && <QueryDisclosure row={row} csrf={queryCsrf} />}
      <h3>来源调用</h3>
      {resource.pending && <Loading />}
      {resource.error !== undefined && <Failure reason={resource.error} retry={resource.reload} />}
      {runs && (runs.length ? (
        <Table caption="来源调用记录" headers={["Provider", "状态", "耗时", "尝试次数", "结果数", "原因"]}>
          {runs.map((run, ordinal) => (
            <tr key={ordinal}>
              <td>{run.provider_id}</td>
              <td>{providerStatuses[run.status] ?? run.status}</td>
              <td>{formatNumber(run.elapsed_ms)} ms</td>
              <td>{formatNumber(run.attempts)}</td>
              <td>{formatNumber(run.count)}</td>
              <td>{run.reason_codes.length ? (
                <ul>{run.reason_codes.map((code) => <li key={code}>{providerReasons[code] ?? "未知原因"} <code>{code}</code></li>)}</ul>
              ) : "—"}</td>
            </tr>
          ))}
        </Table>
      ) : <Empty>暂无已记录的来源调用。</Empty>)}
    </>
  );
}

export function RequestTable({ rows, showTenant = false, caption = "调用记录", showProviderRuns = false, queryCsrf }: {
  rows: RequestRow[];
  showTenant?: boolean;
  caption?: string;
  showProviderRuns?: boolean;
  queryCsrf?: string;
}) {
  const [selected, setSelected] = useState<RequestRow>();
  if (!rows.length) return <Empty>暂无符合条件的调用记录。</Empty>;
  return (
    <>
      <Table caption={caption} headers={["时间", "请求 ID", ...(showTenant ? ["租户"] : []), "状态", "深度", "来源与模态", "耗时", "详情"]}>
        {rows.map((row) => (
          <tr key={row.id}>
            <td>{dateTime(row.created_at)}</td>
            <td><CopyId value={row.request_id} /></td>
            {showTenant && <td><CopyId value={row.tenant_id} /></td>}
            <td>{requestStatus(row)}{row.code && <div className="muted"><code>{row.code}</code></div>}</td>
            <td>{row.mode}</td>
            <td>{row.source_types.join("、")} / {row.modalities.join("、")}</td>
            <td>{formatNumber(row.elapsed_ms)}{row.elapsed_ms === null ? "" : " ms"}</td>
            <td><button type="button" onClick={() => setSelected(row)}>详情</button></td>
          </tr>
        ))}
      </Table>
      {selected && (
        <Dialog title="请求详情" close={() => setSelected(undefined)}>
          {showProviderRuns ? <AdminRequestTrace row={selected} queryCsrf={queryCsrf} /> : <RequestFields row={selected} />}
        </Dialog>
      )}
    </>
  );
}

export function UsageView({ overview = false }: { overview?: boolean }) {
  const resource = useResource<Usage>("usage");
  const [filter, setFilter] = useState("");
  const [status, setStatus] = useState("");
  function setRequestFilter(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    setFilter(String(data.get("request_id") ?? "").trim());
    setStatus(String(data.get("status") ?? ""));
  }
  if (resource.pending) return <Loading />;
  if (!resource.data) return <Failure reason={resource.error} retry={resource.reload} />;
  const usage = resource.data;
  const recent = usage.recent.filter((row) => (!filter || row.request_id.includes(filter)) && (!status || row.status === status));
  return (
    <>
      <div className="actions">
        <span className="badge">最近 {usage.window_days} 天</span>
        <span className="muted">时间按浏览器时区显示，图表按 UTC 汇总。</span>
        <button type="button" onClick={resource.reload}>刷新</button>
        {!overview && <button type="button" disabled={!usage.series.length} onClick={() => downloadCsv(
          "multisearch-usage-utc.csv",
          ["日期（UTC）", "搜索次数", "错误次数", "文字结果数", "图片结果数"],
          usage.series.map((point) => [point.day, point.search_requests, point.errors, point.text_results_returned, point.image_results_returned]),
        )}>导出日用量 CSV</button>}
      </div>
      <Metrics values={resourceMetrics(usage.totals)} />
      <div className="overview-grid">
        <Trend series={overview ? usage.series.slice(-7) : usage.series} />
        <section className="panel"><h2>有效额度上限</h2><QuotaSummary quota={usage.quota} /></section>
      </div>
      {!overview && <div className="overview-grid">
        <section className="panel" aria-labelledby="usage-modes">
          <h2 id="usage-modes">模式分布</h2>
          {!usage.mode_counts ? <Empty>模式统计暂不可用。</Empty> : usage.mode_counts.length === 0 ? <Empty>暂无模式记录。</Empty> : (
            <dl>{usage.mode_counts.map(({ mode, count }) => (
              <div key={mode} className="definition"><dt>{modeTitles[mode] ?? mode}</dt><dd>{formatNumber(count)} 次</dd></div>
            ))}</dl>
          )}
        </section>
        <section className="panel" aria-labelledby="usage-failures">
          <h2 id="usage-failures">失败原因</h2>
          {!usage.failure_counts ? <Empty>失败统计暂不可用。</Empty> : usage.failure_counts.length === 0 ? <Empty>暂无失败记录。</Empty> : (
            <dl>{usage.failure_counts.map(({ code, count }) => (
              <div key={code} className="definition"><dt><code>{code}</code></dt><dd>{formatNumber(count)} 次</dd></div>
            ))}</dl>
          )}
        </section>
      </div>}
      <form method="post" className="actions" onSubmit={setRequestFilter}>
        <input name="request_id" aria-label="按请求 ID 筛选最近调用" placeholder="请求 ID" />
        <select name="status" aria-label="按状态筛选最近调用">
          <option value="">全部状态</option>
          {Object.entries(statusTitles).map(([code, label]) => <option key={code} value={code}>{label}</option>)}
        </select>
        <button type="submit">筛选</button>
        {!overview && <button type="button" disabled={!recent.length} onClick={() => downloadCsv(
          "multisearch-requests.csv",
          ["请求 ID", "时间（UTC）", "状态", "错误码", "模式", "模态", "耗时（毫秒）", "文字结果数", "图片结果数", "缓存命中"],
          recent.map((row) => [
            row.request_id, row.created_at, row.status, row.code, row.mode, row.modalities.join("|"),
            row.elapsed_ms, row.text_results_returned, row.image_results_returned, row.cache_hit,
          ]),
        )}>导出筛选调用 CSV</button>}
      </form>
      <RequestTable rows={recent} caption="最近调用" />
    </>
  );
}
