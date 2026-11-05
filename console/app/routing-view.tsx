"use client";

import { useState, type FormEvent } from "react";
import { callApi } from "./lib/api";
import { CopyId, dateTime, Dialog, Empty, Failure, Loading, Pagination, Table, usePagination, useResource } from "./ui";

type Budget = { max_latency_ms: number; max_results: number };
const CEILINGS = {
  local: { max_latency_ms: 2000, max_results: 30 },
  quick: { max_latency_ms: 4000, max_results: 10 },
  standard: { max_latency_ms: 12000, max_results: 30 },
} satisfies Record<string, Budget>;
type BudgetMode = keyof typeof CEILINGS;
const CACHE = { web: "网页", news: "新闻", image: "图片", academic: "学术", code: "代码", negative: "空结果" };
type CacheKind = keyof typeof CACHE;
type Policy = {
  budgets: Record<BudgetMode, Budget>;
  auto_quick_max_latency_ms: number;
  quick_fallback: boolean;
  cache_ttl_seconds: Record<CacheKind, number>;
};
type Version = { version: number; policy: Policy; created_at: string | null; actor_id: string | null; reason: string; rollback_of: number | null };
type Routing = { active: Version; items: Version[]; next_cursor: number | null };
type RouteTest = Budget & { mode: string; changes: unknown };

function readPolicy(form: FormData): Policy {
  const budgets = Object.fromEntries((Object.keys(CEILINGS) as BudgetMode[]).map((mode) => [mode, {
    max_latency_ms: Number(form.get(`${mode}_max_latency_ms`)),
    max_results: Number(form.get(`${mode}_max_results`)),
  }])) as Policy["budgets"];
  const cache_ttl_seconds = Object.fromEntries(Object.keys(CACHE).map((kind) => [kind, Number(form.get(`ttl_${kind}`))])) as Policy["cache_ttl_seconds"];
  return {
    budgets, cache_ttl_seconds,
    quick_fallback: form.get("quick_fallback") !== null,
    auto_quick_max_latency_ms: Number(form.get("auto_quick_max_latency_ms")),
  };
}

type Change = { label: string; before: number | boolean; after: number | boolean };

function policyDiff(before: Policy, after: Policy): Change[] {
  const changes: Change[] = [];
  function add(label: string, from: Change["before"], to: Change["after"]) {
    if (from !== to) changes.push({ label, before: from, after: to });
  }
  for (const mode of Object.keys(CEILINGS) as BudgetMode[]) {
    add(`${mode} 超时（ms）`, before.budgets[mode].max_latency_ms, after.budgets[mode].max_latency_ms);
    add(`${mode} 结果上限`, before.budgets[mode].max_results, after.budgets[mode].max_results);
  }
  add("自动切换快速模式阈值（ms）", before.auto_quick_max_latency_ms, after.auto_quick_max_latency_ms);
  add("快速模式回退", before.quick_fallback, after.quick_fallback);
  for (const kind of Object.keys(CACHE) as CacheKind[]) {
    add(`${CACHE[kind]}缓存（秒）`, before.cache_ttl_seconds[kind], after.cache_ttl_seconds[kind]);
  }
  return changes;
}

function PolicyDiff({ before, after }: { before: Policy; after: Policy }) {
  const changes = policyDiff(before, after);
  if (!changes.length) return <p className="muted">策略字段没有变化。</p>;
  return <dl>{changes.map(({ label, before, after }) => (
    <div key={label} className="definition"><dt>{label}</dt><dd>{String(before)} → {String(after)}</dd></div>
  ))}</dl>;
}

export function RoutingView({ csrf, canEdit }: { csrf: string; canEdit: boolean }) {
  const pager = usePagination();
  const [rollback, setRollback] = useState<Version>();
  const [selected, setSelected] = useState<Version>();
  const query = new URLSearchParams({ limit: "20" });
  if (pager.cursor) query.set("cursor", pager.cursor);
  const resource = useResource<Routing>(`admin/routing?${query}`);
  if (!resource.data && resource.pending) return <Loading />;
  if (!resource.data) return <Failure reason={resource.error} retry={resource.reload} />;
  const { active, items } = resource.data;
  function changed() {
    pager.reset();
    resource.reload();
  }
  return (
    <>
      <div className="actions">
        <span className="badge positive">当前版本 {active.version}</span>
        <span className="muted">{active.reason}</span>
        <button type="button" disabled={resource.pending} onClick={resource.reload}>刷新版本</button>
      </div>
      <PolicyEditor key={active.version} active={active} csrf={csrf} canEdit={canEdit} changed={changed} />
      {items.length ? (
        <Table caption="已发布路由策略" headers={["版本", "说明", "发布时间", "操作者 ID", "操作"]}>
          {items.map((item) => (
            <tr key={item.version}>
              <td>{item.version}{item.rollback_of !== null && <p className="muted">回滚自 {item.rollback_of}</p>}</td>
              <td>{item.reason}</td><td>{dateTime(item.created_at)}</td><td><CopyId value={item.actor_id} /></td>
              <td className="actions">
                <button type="button" onClick={() => setSelected(item)}>查看策略</button>
                {canEdit && item.version !== active.version && <button type="button" onClick={() => setRollback(item)}>回滚到此版本</button>}
              </td>
            </tr>
          ))}
        </Table>
      ) : <Empty>暂无已发布的自定义策略。</Empty>}
      <Pagination next={resource.data.next_cursor === null ? null : String(resource.data.next_cursor)} pager={pager} />
      {selected && (
        <Dialog title={`策略版本 ${selected.version}`} close={() => setSelected(undefined)}>
          <pre>{JSON.stringify(selected.policy, null, 2)}</pre>
        </Dialog>
      )}
      {rollback && <Rollback csrf={csrf} target={rollback} active={active} close={() => setRollback(undefined)} changed={changed} />}
    </>
  );
}

function PolicyEditor({ active, csrf, canEdit, changed }: { active: Version; csrf: string; canEdit: boolean; changed: () => void }) {
  const [preview, setPreview] = useState<Policy>();
  const [plan, setPlan] = useState<RouteTest>();
  const [error, setError] = useState<unknown>();
  const [pending, setPending] = useState(false);
  const [reason, setReason] = useState("");
  const policy = active.policy;

  async function validate(fields: FormData, publish: boolean) {
    setPending(true);
    setError(undefined);
    setPlan(undefined);
    try {
      const draft = readPolicy(fields);
      const value = fields.get("test_latency");
      const limit = fields.get("test_results");
      const result = await callApi<RouteTest>("admin/routing/test", {
        method: "POST", csrf,
        body: { policy: draft, request: {
          mode: fields.get("test_mode"),
          ...(value ? { max_latency_ms: Number(value) } : {}),
          ...(limit ? { max_results: Number(limit) } : {}),
        } },
      });
      setPlan(result);
      if (publish) setPreview(draft);
    } catch (failure) {
      setError(failure);
    } finally {
      setPending(false);
    }
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const button = (event.nativeEvent as SubmitEvent).submitter;
    void validate(new FormData(event.currentTarget), canEdit && button?.getAttribute("value") === "publish");
  }

  async function publish(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!preview) return;
    setPending(true);
    setError(undefined);
    try {
      await callApi("admin/routing/publish", {
        method: "POST", csrf, body: { policy: preview, expected_version: active.version, reason },
      });
      setPreview(undefined);
      setReason("");
      changed();
    } catch (failure) {
      setError(failure);
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="panel">
      <h2>{canEdit ? "编辑策略草稿" : "试算当前策略"}</h2>
      <form method="post" className="stack" onSubmit={submit} aria-busy={pending}>
        <fieldset>
          <legend>搜索预算</legend>
          <div className="form-grid">
            {(Object.keys(CEILINGS) as BudgetMode[]).flatMap((mode) => [
              <label key={`${mode}-time`}>{mode} 时限（ms）<input type="number" name={`${mode}_max_latency_ms`} defaultValue={policy.budgets[mode].max_latency_ms}
                min={100} max={CEILINGS[mode].max_latency_ms} readOnly={!canEdit} required /></label>,
              <label key={`${mode}-results`}>{mode} 结果上限<input type="number" name={`${mode}_max_results`} defaultValue={policy.budgets[mode].max_results}
                min={1} max={CEILINGS[mode].max_results} readOnly={!canEdit} required /></label>,
            ])}
            <label>自动切换快速阈值（ms）<input name="auto_quick_max_latency_ms" type="number" min={0} max={4000}
              defaultValue={policy.auto_quick_max_latency_ms} readOnly={!canEdit} required /></label>
          </div>
          <label className="check"><input type="checkbox" name="quick_fallback" defaultChecked={policy.quick_fallback} disabled={!canEdit} />快速模式允许回退</label>
          {!canEdit && policy.quick_fallback && <input type="hidden" name="quick_fallback" value="true" />}
        </fieldset>
        <fieldset>
          <legend>缓存有效期（秒）</legend>
          <div className="form-grid">
            {(Object.keys(CACHE) as CacheKind[]).map((kind) => (
              <label key={kind}>{CACHE[kind]}<input type="number" name={`ttl_${kind}`} min={0} max={kind === "negative" ? 120 : 86400}
                defaultValue={policy.cache_ttl_seconds[kind]} readOnly={!canEdit} required /></label>
            ))}
          </div>
        </fieldset>
        <fieldset>
          <legend>试算请求</legend>
          <div className="form-grid">
            <label>模式<select name="test_mode"><option>auto</option><option>local</option><option>quick</option><option>standard</option></select></label>
            <label>请求时限（ms）<input type="number" name="test_latency" min={100} max={900000} /></label>
            <label>请求结果上限<input type="number" name="test_results" min={1} max={30} /></label>
          </div>
        </fieldset>
        {error !== undefined && !preview && <Failure reason={error} />}
        <div className="actions">
          <button type="submit" value="test" disabled={pending}>试算路由</button>
          {canEdit && <button type="submit" value="publish" className="primary" disabled={pending}>预览发布</button>}
        </div>
      </form>
      {plan && (
        <div className="notice positive" role="status">
          <p>执行模式 {plan.mode} · 时限 {plan.max_latency_ms} ms · 最多 {plan.max_results} 条</p>
          {plan.changes != null && <details><summary>查看路由试算数据</summary><pre>{JSON.stringify(plan.changes, null, 2)}</pre></details>}
        </div>
      )}
      {preview && (
        <Dialog title="确认发布策略" pending={pending} close={() => { setPreview(undefined); setError(undefined); }}>
          <form method="post" className="stack" onSubmit={publish}>
            <PolicyDiff before={policy} after={preview} />
            <p className="muted">当前版本 {active.version}。其他管理员修改策略后需刷新并重新确认。</p>
            <label>发布说明<textarea value={reason} onChange={(event) => setReason(event.target.value)} maxLength={500} required /></label>
            {error !== undefined && <Failure reason={error} />}
            <button type="submit" className="primary" disabled={pending || !reason.trim() || !policyDiff(policy, preview).length}>确认发布</button>
          </form>
        </Dialog>
      )}
    </section>
  );
}

function Rollback({ csrf, target, active, close, changed }: { csrf: string; target: Version; active: Version; close: () => void; changed: () => void }) {
  const [error, setError] = useState<unknown>();
  const [pending, setPending] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setError(undefined);
    try {
      const reason = new FormData(event.currentTarget).get("reason");
      await callApi("admin/routing/rollback", {
        method: "POST", csrf, body: { version: target.version, expected_version: active.version, reason },
      });
      close();
      changed();
    } catch (failure) {
      setError(failure);
      setPending(false);
    }
  }
  return (
    <Dialog title={`回滚到版本 ${target.version}`} close={close} pending={pending}>
      <form method="post" className="stack" onSubmit={submit}>
        <PolicyDiff before={active.policy} after={target.policy} />
        <p className="muted">回滚会发布一个新版本并保留现有历史。</p>
        <label>回滚说明<textarea name="reason" required maxLength={500} /></label>
        {error !== undefined && <Failure reason={error} />}
        <button type="submit" className="danger" disabled={pending}>确认回滚</button>
      </form>
    </Dialog>
  );
}
