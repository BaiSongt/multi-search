"use client";

import { useState, type FormEvent } from "react";
import { callApi } from "./lib/api";
import { quotaLabels, type Audit, type Overview, type Page, type Provider, type RequestRow, type Tenant } from "./lib/contracts";
import { CopyId, dateTime, Dialog, Empty, Failure, formatNumber, Loading, Pagination, Table, usePagination, useResource } from "./ui";
import { Metrics, QuotaSummary, RequestTable, resourceMetrics, Trend } from "./usage-view";

export function AdminOverview() {
  const resource = useResource<Overview>("admin/overview");
  const failures = useResource<Page<RequestRow>>("admin/requests?status=failed&limit=5");
  if (resource.pending) return <Loading />;
  if (!resource.data) return <Failure reason={resource.error} retry={resource.reload} />;
  const overview = resource.data;
  return (
    <>
      <div className="actions">
        <span className="muted">按授权范围汇总。没有样本时显示「—」。</span>
        <button type="button" onClick={() => { resource.reload(); failures.reload(); }}>刷新指标</button>
      </div>
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
        <h2>最近异常</h2>
        {failures.pending && <Loading />}
        {failures.error !== undefined && <Failure reason={failures.error} retry={failures.reload} />}
        {failures.data && <RequestTable rows={failures.data.items} caption="最近失败的请求" />}
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
      {rows && rows.length > 0 && (
        <Table caption="Provider 状态和运行指标" headers={["Provider", "状态", "来源与模态", "最近耗时", "P50 / P95", "运行 / 错误 / 限流", "优先级 / RPM", ...(canEdit ? ["操作"] : [])]}>
          {rows.map((provider) => (
            <tr key={provider.provider_id}>
              <td><CopyId value={provider.provider_id} /></td>
              <td><span className={`badge ${provider.status === "healthy" && provider.enabled ? "positive" : "warning"}`}>{provider.enabled ? provider.status : "停用"}</span></td>
              <td>{provider.pair_health.map((pair) => (
                <div key={`${pair.source_type}/${pair.modality}`}>{pair.source_type} / {pair.modality} · {pair.status}</div>
              ))}</td>
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

export function RequestsView({ showTenant }: { showTenant: boolean }) {
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
          <label>模态<select name="modality"><option value="">全部</option><option value="text">文字</option><option value="image">图片</option></select></label>
          <label>来源<select name="source_type"><option value="">全部</option><option value="web">Web</option><option value="news">新闻</option></select></label>
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
          <RequestTable rows={resource.data.items} showTenant={showTenant} />
          <Pagination next={resource.data.next_cursor} pager={pager} />
        </>
      )}
    </>
  );
}

export function TenantsView({ csrf, quotas = false }: { csrf: string; quotas?: boolean }) {
  const [filter, setFilter] = useState("");
  const [editing, setEditing] = useState<Tenant>();
  const [creating, setCreating] = useState(false);
  const pager = usePagination();
  const query = new URLSearchParams({ limit: "20" });
  if (pager.cursor) query.set("cursor", pager.cursor);
  const resource = useResource<Page<Tenant>>(`admin/tenants?${query}`);
  const rows = resource.data?.items.filter((tenant) => `${tenant.slug} ${tenant.id}`.toLowerCase().includes(filter.trim().toLowerCase()));
  return (
    <>
      <div className="actions">
        {!quotas && <button type="button" className="primary" onClick={() => setCreating(true)}>新建租户</button>}
        <input value={filter} aria-label="筛选本页租户" placeholder="筛选本页租户名或 ID" onChange={(event) => setFilter(event.target.value)} />
        <button type="button" onClick={resource.reload} disabled={resource.pending}>刷新</button>
      </div>
      {resource.pending && <Loading />}
      {resource.error !== undefined && <Failure reason={resource.error} retry={resource.reload} />}
      {rows?.length === 0 && <Empty>没有符合条件的租户。</Empty>}
      {rows && rows.length > 0 && (
        <Table caption={quotas ? "租户用量与额度" : "租户列表"} headers={["租户", "状态", "请求数", ...(quotas ? ["额度上限"] : []), "操作"]}>
          {rows.map((tenant) => (
            <tr key={tenant.id}>
              <td><strong>{tenant.slug}</strong><br /><CopyId value={tenant.id} /></td>
              <td><span className={`badge ${tenant.enabled ? "positive" : "warning"}`}>{tenant.enabled ? "启用" : "停用"}</span></td>
              <td>{formatNumber(tenant.search_requests)}</td>
              {quotas && <td><QuotaSummary quota={tenant.quota} /></td>}
              <td><button type="button" onClick={() => setEditing(tenant)}>{quotas ? "编辑额度" : "编辑状态"}</button></td>
            </tr>
          ))}
        </Table>
      )}
      {resource.data && <Pagination next={resource.data.next_cursor} pager={pager} />}
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

export function AuditView() {
  const pager = usePagination();
  const [filter, setFilter] = useState("");
  const query = new URLSearchParams({ limit: "20" });
  if (pager.cursor) query.set("cursor", pager.cursor);
  const resource = useResource<Page<Audit>>(`admin/audit?${query}`);
  const rows = resource.data?.items.filter((event) =>
    `${event.action} ${event.tenant_id} ${event.actor_id ?? ""} ${event.target_id ?? ""}`.toLowerCase().includes(filter.trim().toLowerCase())
  );
  return (
    <>
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
