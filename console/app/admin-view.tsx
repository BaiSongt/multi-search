"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";
import { ProviderCapabilities } from "./provider-capabilities";
import { callApi } from "./lib/api";
import { quotaLabels, type Audit, type AuditRiskReport, type DependencyHealth, type Overview, type Page, type Provider, type RequestRow, type ServiceStatus, type Tenant, type TenantQuotaUsage } from "./lib/contracts";
import { CopyId, dateTime, Dialog, Empty, Failure, formatNumber, Loading, Pagination, Table, usePagination, useResource } from "./ui";
import { Metrics, QuotaSummary, RequestTable, resourceMetrics, Trend } from "./usage-view";

const dependencyLabels: Record<keyof DependencyHealth["checks"], string> = {
  api: "API",
  postgresql: "PostgreSQL",
  redis: "Redis",
  fetch_worker: "抓取 Worker",
  research_worker: "研究 Worker",
  dynamic_fetch_worker: "动态抓取 Worker",
  searxng: "SearXNG 服务",
  crawl4ai: "Crawl4AI（API）",
};
const serviceStatuses: Record<ServiceStatus, { label: string; tone: string }> = {
  healthy: { label: "正常", tone: "positive" },
  unavailable: { label: "不可用", tone: "danger" },
  unknown: { label: "未确认", tone: "warning" },
  not_configured: { label: "未配置", tone: "" },
};

function DependencyChecks({ health }: { health: DependencyHealth }) {
  return (
    <>
      <p className="muted">采样时间 <time dateTime={health.checked_at}>{dateTime(health.checked_at)}</time></p>
      <dl>
        {(Object.keys(dependencyLabels) as (keyof typeof dependencyLabels)[]).map((key) => {
          const status = serviceStatuses[health.checks[key] ?? "unknown"] ?? serviceStatuses.unknown;
          return <div className="definition" key={key}>
            <dt>{dependencyLabels[key]}</dt>
            <dd><span className={`badge ${status.tone}`}>{status.label}</span></dd>
          </div>;
        })}
      </dl>
    </>
  );
}

function OverviewMetrics({ overview }: { overview: Overview }) {
  const queue = overview.job_queue;
  return (
    <>
      <Metrics values={[
        ["平均 QPS", overview.requests_per_second, "次/s"],
        ["成功率", overview.success_rate == null ? null : overview.success_rate * 100, "%"],
        ["P50 延迟", overview.p50_ms, "ms"],
        ["P95 延迟", overview.p95_ms, "ms"],
        ["图片来源完整率", overview.image_source_coverage == null ? null : overview.image_source_coverage * 100, "%"],
        ["活跃任务", overview.active_jobs, "个"],
        ...resourceMetrics(overview.totals),
      ]} />
      <Trend series={overview.series} />
      <section className="panel">
        <h2>队列压力</h2>
        {queue ? <>
          <Metrics values={[
            ["排队中", queue.queued, "个"],
            ["运行中", queue.running, "个"],
            ["正在取消", queue.cancelling, "个"],
            ["排队重试", queue.queued_for_retry, "个"],
          ]} />
          <p className="muted">{queue.oldest_queued_at
            ? <>最早等待开始于 <time dateTime={queue.oldest_queued_at}>{dateTime(queue.oldest_queued_at)}</time></>
            : "当前没有排队任务。"}</p>
        </> : <Empty>队列指标暂不可用。</Empty>}
        <Link href="/admin/jobs">查看任务与队列</Link>
      </section>
    </>
  );
}

export function AdminOverview() {
  const resource = useResource<Overview>("admin/overview");
  const failures = useResource<Page<RequestRow>>("admin/requests?status=failed&limit=5");
  const health = useResource<DependencyHealth>("health/dependencies");
  return (
    <>
      <div className="actions">
        <span className="muted">QPS 取最近 1 分钟，其他用量和延迟取最近 30 天。没有样本时显示「—」。</span>
        <button type="button" onClick={() => { resource.reload(); failures.reload(); health.reload(); }}>刷新指标</button>
      </div>
      <section className="panel stack" aria-labelledby="dependency-health">
        <h2 id="dependency-health">服务状态</h2>
        {health.pending && <Loading />}
        {health.error !== undefined && <Failure reason={health.error} retry={health.reload} />}
        {health.data && <DependencyChecks health={health.data} />}
      </section>
      {resource.pending && <Loading />}
      {resource.error !== undefined && <Failure reason={resource.error} retry={resource.reload} />}
      {resource.data && <OverviewMetrics overview={resource.data} />}
      <section className="panel">
        <h2>最近异常</h2>
        {failures.pending && <Loading />}
        {failures.error !== undefined && <Failure reason={failures.error} retry={failures.reload} />}
        {failures.data && <RequestTable rows={failures.data.items} caption="最近失败的请求" showProviderRuns />}
      </section>
    </>
  );
}

type PolicyField = {
  name: string;
  label: string;
} & ({ value: boolean } | { value: number | undefined; min: number; max?: number });

type Change = { field: PolicyField; value: number | boolean };

function displayValue(value: number | boolean | undefined) {
  if (typeof value === "boolean") return value ? "启用" : "停用";
  return value === undefined ? "继承" : formatNumber(value);
}

function PolicyDialog({ title, fields, save, close }: {
  title: string;
  fields: PolicyField[];
  save: (changes: Record<string, boolean | number>) => Promise<void>;
  close: () => void;
}) {
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [preview, setPreview] = useState<Change[]>();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>();
  const [unchanged, setUnchanged] = useState(false);

  function prepare(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const values = new FormData(event.currentTarget);
    const changes = fields.flatMap((field): Change[] => {
      const input = values.get(field.name);
      if (typeof input !== "string" || input === "") return [];
      const value = typeof field.value === "boolean" ? input === "true" : Number(input);
      return value === field.value ? [] : [{ field, value }];
    });
    setUnchanged(!changes.length);
    if (changes.length) setPreview(changes);
  }

  async function confirm() {
    if (!preview) return;
    setError(undefined);
    setPending(true);
    try {
      await save(Object.fromEntries(preview.map(({ field, value }) => [field.name, value])));
      close();
    } catch (reason) {
      setError(reason);
      setPending(false);
    }
  }

  return (
    <Dialog title={title} close={close} pending={pending}>
      {preview ? (
        <div className="stack" aria-busy={pending}>
          <h3>确认变更</h3>
          <dl>{preview.map(({ field, value }) => <div key={field.name} className="definition"><dt>{field.label}</dt><dd>{displayValue(field.value)} → {displayValue(value)}</dd></div>)}</dl>
          <p className="muted">确认后将写入审计记录。</p>
          {error !== undefined && <Failure reason={error} />}
          <div className="actions">
            <button type="button" disabled={pending} onClick={() => { setPreview(undefined); setError(undefined); }}>返回编辑</button>
            <button type="button" className="primary" disabled={pending} onClick={() => void confirm()}>{pending ? "提交中…" : "确认提交"}</button>
          </div>
        </div>
      ) : (
        <form method="post" className="stack" onSubmit={prepare}>
          <div className="form-grid">
            {fields.map((field) => {
              const value = draft[field.name] ?? (field.value === undefined ? "" : String(field.value));
              const update = (text: string) => {
                setDraft((current) => ({ ...current, [field.name]: text }));
                setUnchanged(false);
              };
              return (
                <label key={field.name}>
                  {field.label}
                  {"min" in field ? (
                    <input type="number" name={field.name} min={field.min} max={field.max} value={value}
                      placeholder={field.value === undefined ? "继承" : String(field.value)} onChange={(event) => update(event.target.value)} />
                  ) : (
                    <select name={field.name} value={value} onChange={(event) => update(event.target.value)}>
                      <option value="true">启用</option><option value="false">停用</option>
                    </select>
                  )}
                </label>
              );
            })}
          </div>
          {unchanged && <p className="notice warning" role="status">没有需要提交的变更。</p>}
          <button type="submit" className="primary">预览变更</button>
        </form>
      )}
    </Dialog>
  );
}

export function ProvidersView({ canEdit, csrf }: { canEdit: boolean; csrf: string }) {
  const resource = useResource<{ providers: Provider[] }>("admin/providers");
  const [filter, setFilter] = useState("");
  const [state, setState] = useState("");
  const [editing, setEditing] = useState<Provider>();
  const rows = resource.data?.providers.filter((provider) =>
    provider.provider_id.toLowerCase().includes(filter.trim().toLowerCase()) &&
    (!state || state === (provider.enabled ? "enabled" : "disabled"))
  );
  return (
    <>
      <div className="actions">
        <input value={filter} aria-label="筛选 Provider" placeholder="Provider ID" onChange={(event) => setFilter(event.target.value)} />
        <select value={state} aria-label="筛选 Provider 启停状态" onChange={(event) => setState(event.target.value)}>
          <option value="">全部状态</option><option value="enabled">启用</option><option value="disabled">停用</option>
        </select>
        <button type="button" onClick={resource.reload} disabled={resource.pending}>刷新状态</button>
      </div>
      {resource.pending && <Loading />}
      {resource.error !== undefined && <Failure reason={resource.error} retry={resource.reload} />}
      {rows?.length === 0 && <Empty>没有符合条件的 Provider。</Empty>}
      {Boolean(rows?.length) && (
        <p className="muted">已存运行指标统计{canEdit ? "所有租户" : "本租户"}。当前健康状态和 RPM 上限以单进程为单位。</p>
      )}
      {rows && rows.length > 0 && (
        <Table caption="Provider 状态和运行指标" headers={["Provider", "状态", "来源与模态", "最近耗时", "P50 / P95", "运行 / 错误 / 限流", "优先级 / RPM", ...(canEdit ? ["操作"] : [])]}>
          {rows.map((provider) => (
            <tr key={provider.provider_id}>
              <td><CopyId value={provider.provider_id} /></td>
              <td><span className={`badge ${provider.status === "healthy" && provider.enabled ? "positive" : "warning"}`}>{provider.enabled ? provider.status : "停用"}</span></td>
              <td>
                {provider.pair_health.map((pair) => (
                  <div key={`${pair.source_type}/${pair.modality}`}>{pair.source_type} / {pair.modality} · {pair.status}</div>
                ))}
                <ProviderCapabilities provider={provider} />
              </td>
              <td>{formatNumber(provider.last_elapsed_ms)}{provider.last_elapsed_ms == null ? "" : " ms"}</td>
              <td>{formatNumber(provider.usage.p50_ms)} / {formatNumber(provider.usage.p95_ms)} ms</td>
              <td>{provider.usage.runs} / {provider.usage.errors} / {provider.usage.rate_limited}</td>
              <td>{provider.priority} / {provider.requests_per_minute}</td>
              {canEdit && <td><button type="button" onClick={() => setEditing(provider)}>配置</button></td>}
            </tr>
          ))}
        </Table>
      )}
      {editing && (
        <PolicyDialog
          title={`配置 ${editing.provider_id}`}
          fields={[
            { name: "enabled", label: "启停", value: editing.enabled },
            { name: "priority", label: "优先级", value: editing.priority, min: -100, max: 100 },
            { name: "requests_per_minute", label: "每分钟上限", value: editing.requests_per_minute, min: 1, max: 120 },
          ]}
          close={() => setEditing(undefined)}
          save={async (changes) => {
            await callApi(`admin/providers/${encodeURIComponent(editing.provider_id)}`, { method: "PATCH", body: changes, csrf });
            resource.reload();
          }}
        />
      )}
    </>
  );
}

export function RequestsView({ showTenant, csrf }: { showTenant: boolean; csrf: string }) {
  const pager = usePagination();
  const [filters, setFilters] = useState<Record<string, string>>({});
  const query = new URLSearchParams({ limit: "20", ...filters });
  if (pager.cursor) query.set("cursor", pager.cursor);
  const resource = useResource<Page<RequestRow>>(`admin/requests?${query}`);

  function applyFilters(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const fields = new FormData(event.currentTarget);
    const values = Object.fromEntries(Array.from(fields).flatMap(([name, input]) => {
      const value = String(input).trim();
      if (!value) return [];
      if (name === "trace_id") return [[name, value.toLowerCase()]];
      return [[name, name === "since" || name === "until" ? new Date(value).toISOString() : value]];
    }));
    pager.reset();
    setFilters(values);
  }
  return (
    <>
      <form method="post" className="panel" onSubmit={applyFilters}>
        <div className="form-grid">
          <label>请求状态<select name="status"><option value="">全部</option><option value="running">处理中</option><option value="completed">完成</option><option value="partial">部分完成</option><option value="failed">失败</option><option value="cancelled">取消</option></select></label>
          <label>搜索深度<select name="mode"><option value="">全部</option><option value="auto">自动</option><option value="quick">快速</option><option value="standard">标准</option><option value="local">本地</option><option value="evidence">证据</option><option value="deep">深度</option></select></label>
          <label>模态<select name="modality"><option value="">全部</option><option value="text">文字</option><option value="image">图片</option><option value="video">视频</option></select></label>
          <label>来源<select name="source_type"><option value="">全部</option><option value="web">Web</option><option value="news">新闻</option><option value="academic">学术</option><option value="code">代码</option><option value="patent">专利</option><option value="local">本地</option></select></label>
          <label>追踪 ID<input name="trace_id" maxLength={32} pattern="[a-fA-F0-9]{32}" title="请输入 32 位十六进制追踪 ID" /></label>
          {showTenant && <label>租户 ID<input name="tenant_id" maxLength={36} /></label>}
          <label>开始时间<input name="since" type="datetime-local" /></label>
          <label>结束时间<input name="until" type="datetime-local" /></label>
        </div>
        <div className="actions">
          <button type="submit" className="primary">应用筛选</button>
          <button type="reset" onClick={() => { pager.reset(); setFilters({}); }}>清除筛选</button>
          <button type="button" onClick={resource.reload} disabled={resource.pending}>刷新</button>
        </div>
      </form>
      {resource.pending && <Loading />}
      {resource.error !== undefined && <Failure reason={resource.error} retry={resource.reload} />}
      {resource.data && (
        <>
          <RequestTable rows={resource.data.items} showTenant={showTenant} showProviderRuns
            queryCsrf={showTenant ? csrf : undefined} />
          <Pagination next={resource.data.next_cursor} pager={pager} />
        </>
      )}
    </>
  );
}

export function TenantsView({ csrf, quotas = false }: { csrf: string; quotas?: boolean }) {
  const [filter, setFilter] = useState("");
  const [editing, setEditing] = useState<Tenant>();
  const [usageTenant, setUsageTenant] = useState<Tenant>();
  const [creating, setCreating] = useState(false);
  const pager = usePagination();
  const query = new URLSearchParams({ limit: "20" });
  if (pager.cursor) query.set("cursor", pager.cursor);
  if (filter.trim()) query.set("q", filter.trim());
  const resource = useResource<Page<Tenant>>(`admin/tenants?${query}`);
  const rows = resource.data?.items;
  return (
    <>
      <div className="actions">
        {!quotas && <button type="button" className="primary" onClick={() => setCreating(true)}>新建租户</button>}
        <input value={filter} aria-label="筛选租户" placeholder="筛选租户名或 ID" maxLength={63} onChange={(event) => { pager.reset(); setFilter(event.target.value); }} />
        <button type="button" onClick={resource.reload} disabled={resource.pending}>刷新</button>
      </div>
      {resource.pending && <Loading />}
      {resource.error !== undefined && <Failure reason={resource.error} retry={resource.reload} />}
      {rows?.length === 0 && <Empty>没有符合条件的租户。</Empty>}
      {rows && rows.length > 0 && (
        <Table caption={quotas ? "租户用量与额度" : "租户列表"} headers={["租户", "状态", "近 30 天请求数", ...(quotas ? ["额度上限"] : []), "操作"]}>
          {rows.map((tenant) => (
            <tr key={tenant.id}>
              <td><strong>{tenant.slug}</strong><br /><CopyId value={tenant.id} /></td>
              <td><span className={`badge ${tenant.enabled ? "positive" : "warning"}`}>{tenant.enabled ? "启用" : "停用"}</span></td>
              <td>{formatNumber(tenant.search_requests)}</td>
              {quotas && <td><QuotaSummary quota={tenant.quota} /></td>}
              <td>
                <div className="actions">
                  {!quotas && <Link className="button" href={`/admin/tenants/${encodeURIComponent(tenant.id)}`}>成员与邀请</Link>}
                  {quotas && <button type="button" onClick={() => setUsageTenant(tenant)}>查看用量</button>}
                  <button type="button" onClick={() => setEditing(tenant)}>{quotas ? "编辑额度" : "编辑状态"}</button>
                </div>
              </td>
            </tr>
          ))}
        </Table>
      )}
      {resource.data && <Pagination next={resource.data.next_cursor} pager={pager} />}
      {usageTenant && <TenantQuotaUsageDialog tenant={usageTenant} close={() => setUsageTenant(undefined)} />}
      {creating && <CreateTenant csrf={csrf} close={() => setCreating(false)} created={(tenant) => {
        setFilter(tenant.slug);
        pager.reset();
        resource.reload();
      }} />}
      {editing && (
        <PolicyDialog
          title={quotas ? `配置 ${editing.slug} 的额度` : `调整 ${editing.slug} 的状态`}
          fields={quotas
            ? Object.entries(quotaLabels).map(([name, label]) => ({ name, label, value: editing.quota[name], min: 1 }))
            : [{ name: "enabled", label: "租户状态", value: editing.enabled }]}
          close={() => setEditing(undefined)}
          save={async (changes) => {
            const base = `admin/tenants/${encodeURIComponent(editing.id)}`;
            await callApi(quotas ? `${base}/quota` : base, { method: "PATCH", csrf, body: quotas ? { quota: changes } : changes });
            resource.reload();
          }}
        />
      )}
    </>
  );
}

function TenantQuotaUsageDialog({ tenant, close }: { tenant: Tenant; close: () => void }) {
  const pager = usePagination();
  const query = new URLSearchParams({ limit: "20" });
  if (pager.cursor) query.set("cursor", pager.cursor);
  const resource = useResource<TenantQuotaUsage>(`admin/tenants/${encodeURIComponent(tenant.id)}/quota-usage?${query}`);
  const report = resource.data;
  return (
    <Dialog title={`${tenant.slug} 的额度用量`} close={close}>
      {resource.pending && <Loading />}
      {resource.error !== undefined && <Failure reason={resource.error} retry={resource.reload} />}
      {report && (
        <div className="stack quota-usage-report">
          <div className="actions">
            <span className="muted">统计日期 {report.day.slice(0, 10)} UTC · 更新于 {dateTime(report.observed_at)}（{Intl.DateTimeFormat().resolvedOptions().timeZone}）</span>
            <button type="button" onClick={resource.reload} disabled={resource.pending}>刷新用量</button>
          </div>
          <h3>租户合计</h3>
          <p className="muted">包括 API 密钥与 Console 会话产生的用量。</p>
          <Metrics values={[
            ["总请求", report.usage.requests, "次"],
            ["搜索请求", report.usage.search_requests, "次"],
            ["图片请求", report.usage.image_requests, "次"],
            ["图片结果", report.usage.image_results_returned, "项"],
            ["抓取请求", report.usage.fetch_requests, "次"],
            ["抓取页数", report.usage.pages_fetched, "页"],
            ["当前并发", report.usage.active_executions, "个"],
          ]} />
          <QuotaSummary quota={report.quota} />
          {report.keys.items.length === 0 ? <Empty>此页没有密钥。</Empty> : (
            <Table caption="按密钥统计" headers={["密钥", "每日请求 / 额度", "搜索请求", "图片请求 / 结果", "抓取请求 / 页数", "当前并发 / 上限", "有效额度"]}>
              {report.keys.items.map(({ key_mask, usage, quota }, index) => (
                <tr key={`${key_mask}-${index}`}>
                  <td><code>{key_mask}</code></td>
                  <td>{formatNumber(usage.requests)} / {formatNumber(quota.requests_per_day)}</td>
                  <td>{formatNumber(usage.search_requests)}</td>
                  <td>{formatNumber(usage.image_requests)} / {formatNumber(usage.image_results_returned)}</td>
                  <td>{formatNumber(usage.fetch_requests)} / {formatNumber(usage.pages_fetched)}</td>
                  <td>{formatNumber(usage.active_executions)} / {formatNumber(quota.concurrency)}</td>
                  <td><details><summary>全部额度</summary><QuotaSummary quota={quota} /></details></td>
                </tr>
              ))}
            </Table>
          )}
          <Pagination next={report.keys.next_cursor} pager={pager} />
        </div>
      )}
    </Dialog>
  );
}

function CreateTenant({ csrf, close, created }: { csrf: string; close: () => void; created: (tenant: Tenant) => void }) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>();
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const slug = new FormData(event.currentTarget).get("slug");
    setPending(true);
    setError(undefined);
    try {
      const tenant = await callApi<Tenant>("admin/tenants", { method: "POST", csrf, body: { slug } });
      created(tenant);
      close();
    } catch (reason) {
      setError(reason);
      setPending(false);
    }
  }
  return (
    <Dialog title="新建租户" close={close} pending={pending}>
      <form method="post" className="stack" onSubmit={submit} aria-busy={pending}>
        <label>租户标识<input name="slug" required pattern="[a-z0-9][a-z0-9\-]{0,62}" maxLength={63} /></label>
        <p className="muted">使用 1–63 个小写字母、数字或连字符，以字母或数字开头。</p>
        {error !== undefined && <Failure reason={error} />}
        <button type="submit" className="primary" disabled={pending}>{pending ? "创建中…" : "创建租户"}</button>
      </form>
    </Dialog>
  );
}

const AUDIT_RISK_LABELS: Record<AuditRiskReport["alerts"][number]["code"], string> = {
  privileged_access_change: "管理员权限相关操作",
  prod_key_without_ip_allowlist: "签发了未限制 IP 的生产密钥",
  tenant_disabled: "租户已停用",
};

export function AuditView() {
  const pager = usePagination();
  const [filter, setFilter] = useState("");
  const query = new URLSearchParams({ limit: "20" });
  if (pager.cursor) query.set("cursor", pager.cursor);
  const resource = useResource<Page<Audit>>(`admin/audit?${query}`);
  const risks = useResource<AuditRiskReport>("admin/audit/risks");
  const rows = resource.data?.items.filter((event) =>
    `${event.action} ${event.tenant_id} ${event.actor_id ?? ""} ${event.target_id ?? ""}`.toLowerCase().includes(filter.trim().toLowerCase())
  );
  return (
    <>
      <section className="panel stack" aria-labelledby="audit-risk-title">
        <div className="actions">
          <h2 id="audit-risk-title">风险告警</h2>
          <button type="button" onClick={risks.reload} disabled={risks.pending}>刷新风险告警</button>
        </div>
        {risks.pending && <Loading />}
        {risks.error !== undefined && <Failure reason={risks.error} retry={risks.reload} />}
        {risks.data && <>
          <p className="muted">最近 {risks.data.window_days} 天内值得复核的管理操作。{risks.data.truncated && `仅显示最新 ${risks.data.alerts.length} 条。`}</p>
          {risks.data.alerts.length === 0 ? <Empty>本周期没有匹配的风险告警。</Empty> :
            <Table caption="近期风险告警" headers={["时间", "风险提示", "租户", "事件 ID"]}>
              {risks.data.alerts.map((alert) => (
                <tr key={alert.event_id}>
                  <td>{dateTime(alert.occurred_at)}</td>
                  <td><span className="badge warning">{AUDIT_RISK_LABELS[alert.code] ?? alert.code}</span></td>
                  <td><CopyId value={alert.tenant_id} /></td>
                  <td><CopyId value={alert.event_id} /></td>
                </tr>
              ))}
            </Table>}
        </>}
      </section>
      <div className="actions">
        <input value={filter} aria-label="筛选本页审计事件" placeholder="筛选本页动作或 ID" onChange={(event) => setFilter(event.target.value)} />
        <button type="button" onClick={resource.reload} disabled={resource.pending}>刷新</button>
      </div>
      {resource.pending && <Loading />}
      {resource.error !== undefined && <Failure reason={resource.error} retry={resource.reload} />}
      {rows?.length === 0 && <Empty>暂无符合条件的审计事件。</Empty>}
      {rows && rows.length > 0 && (
        <Table caption="变更审计" headers={["时间", "事件 ID", "租户", "操作者", "动作", "对象", "差异"]}>
          {rows.map((event) => (
            <tr key={event.id}>
              <td>{dateTime(event.occurred_at)}</td>
              <td><CopyId value={String(event.id)} /></td>
              <td><CopyId value={event.tenant_id} /></td>
              <td>{event.actor_type}{event.actor_id && <div><CopyId value={event.actor_id} /></div>}</td>
              <td><code>{event.action}</code></td>
              <td><CopyId value={event.target_id} /></td>
              <td><details><summary>查看</summary><pre>{JSON.stringify(event.details, null, 2)}</pre></details></td>
            </tr>
          ))}
        </Table>
      )}
      {resource.data && <Pagination next={resource.data.next_cursor} pager={pager} />}
    </>
  );
}
