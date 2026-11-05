"use client";

import { useState, type FormEvent } from "react";
import { quotaLabels, type RequestRow, type Totals, type Usage, type UsageDay } from "./lib/contracts";
import { downloadCsv } from "./lib/csv";
import { CopyId, dateTime, Dialog, Empty, Failure, formatNumber, Loading, Table, useResource } from "./ui";

const statusTitles: Record<string, string> = {
  running: "处理中",
  completed: "完成",
  partial: "部分完成",
  failed: "失败",
  cancelled: "取消",
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

export function RequestTable({ rows, showTenant = false, caption = "调用记录" }: { rows: RequestRow[]; showTenant?: boolean; caption?: string }) {
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
          <dl>
            <dt>请求 ID</dt><dd><CopyId value={selected.request_id} /></dd>
            {selected.trace_id && <><dt>追踪 ID</dt><dd><CopyId value={selected.trace_id} /></dd></>}
            <dt>记录 ID</dt><dd><CopyId value={selected.id} /></dd>
            <dt>租户 ID</dt><dd><CopyId value={selected.tenant_id} /></dd>
            <dt>密钥 ID</dt><dd><CopyId value={selected.key_id} /></dd>
            <dt>发生时间</dt><dd>{dateTime(selected.created_at)}</dd>
            <dt>状态</dt><dd>{requestStatus(selected)}</dd>
            <dt>错误码</dt><dd>{selected.code ?? "—"}</dd>
            <dt>耗时</dt><dd>{selected.elapsed_ms === null ? "—" : `${selected.elapsed_ms} ms`}</dd>
            <dt>缓存命中</dt><dd>{selected.cache_hit ? "是" : "否"}</dd>
            <dt>文字结果</dt><dd>{formatNumber(selected.text_results_returned)}</dd>
            <dt>图片结果</dt><dd>{formatNumber(selected.image_results_returned)}</dd>
          </dl>
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
