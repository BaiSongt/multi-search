"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";
import { ApiError, callApi } from "./lib/api";
import { CopyId, dateTime, Dialog, Empty, Failure, formatNumber, Loading, Table, useResource } from "./ui";
import { Metrics } from "./usage-view";

const CACHE_TYPES = { web: "网页", news: "新闻", image: "图片", academic: "学术", code: "代码", local: "本地", all: "当前租户全部缓存" };
type CacheType = keyof typeof CACHE_TYPES;
type CacheInfo = {
  tenant_id: string;
  search_requests: number;
  cache_hits: number;
  hit_rate: number | null;
  window_days: number;
  epochs: Partial<Record<CacheType, number>>;
  storage: { keys: number | null; bytes: number | null; complete: boolean; available: boolean };
};
type Settings = {
  environment: { api_version: string; providers: string[]; public_search: boolean; enable_images: boolean };
  security: { console_origin: string; secure_session_cookie: boolean; require_csrf: boolean; session_ttl_hours: number; session_absolute_ttl_days: number };
  retention: { usage_metadata_days: number | null; idempotency_window_minutes: number };
};
type RetentionPolicy = {
  tenant_id: string;
  retention_hours: number;
  version: number;
  updated_at: string | null;
  changes: { version: number; before_hours: number; after_hours: number; occurred_at: string }[];
};

const RETENTION_PATH = "admin/settings/query-retention";
const TENANT_ID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const RETENTION_HOURS = /^(?:[0-9]|1[0-9]|2[0-4])$/;

export function CacheView({ csrf, platform }: { csrf: string; platform: boolean }) {
  const [tenant, setTenant] = useState("");
  const [type, setType] = useState<CacheType>("image");
  const [confirm, setConfirm] = useState<{ tenant: string; type: CacheType }>();
  const [feedback, setFeedback] = useState("");
  const query = new URLSearchParams();
  if (tenant) query.set("tenant_id", tenant);
  const resource = useResource<CacheInfo>(`admin/cache?${query}`);
  const data = resource.data;

  function chooseTenant(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFeedback("");
    const id = String(new FormData(event.currentTarget).get("tenant_id") ?? "").trim();
    if (id === tenant) resource.reload();
    else setTenant(id);
  }

  return (
    <>
      <div className="actions">
        {platform && (
          <form method="post" className="actions" onSubmit={chooseTenant}>
            <input name="tenant_id" aria-label="缓存租户 ID" placeholder="留空查看当前租户" maxLength={36} />
            <button type="submit">切换租户</button>
          </form>
        )}
        <button type="button" onClick={resource.reload} disabled={resource.pending}>刷新缓存指标</button>
      </div>
      {resource.pending && <Loading />}
      {resource.error !== undefined && <Failure reason={resource.error} retry={resource.reload} />}
      {data && (
        <>
          <div className="status-line"><span className="muted">租户</span><CopyId value={data.tenant_id} /><span className="badge">最近 {data.window_days} 天</span></div>
          <Metrics values={[
            ["搜索请求", data.search_requests, "次"],
            ["缓存命中", data.cache_hits, "次"],
            ["命中率", data.hit_rate === null ? null : data.hit_rate * 100, "%"],
            ["缓存条目", data.storage.available ? data.storage.keys : null, "项"],
            ["估计占用", data.storage.available ? data.storage.bytes : null, "B"],
          ]} />
          {!data.storage.available && <p className="notice warning" role="status">缓存存储指标暂时不可用。</p>}
          {data.storage.available && !data.storage.complete && <p className="notice warning" role="status">缓存容量为有界扫描结果，可能低于实际占用。</p>}
          <section className="panel">
            <h2>按租户使缓存失效</h2>
            <p className="muted">指定范围内的旧结果不再命中，原条目按有效期逐步释放。</p>
            <div className="actions">
              <label>缓存范围<select value={type} onChange={(event) => setType(event.target.value as CacheType)}>
                {Object.entries(CACHE_TYPES).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
              </select></label>
              <span className="muted">当前代数 {formatNumber(data.epochs[type] ?? 0)}</span>
              <button type="button" className="danger" onClick={() => setConfirm({ tenant: data.tenant_id, type })}>预览失效</button>
            </div>
            {feedback && <p className="notice positive" role="status">{feedback}</p>}
          </section>
        </>
      )}
      {confirm && (
        <CacheInvalidate csrf={csrf} tenant={confirm.tenant} type={confirm.type} close={() => setConfirm(undefined)} changed={(epoch) => {
          setFeedback(`${CACHE_TYPES[confirm.type]}缓存已更新到代数 ${epoch}。`);
          resource.reload();
        }} />
      )}
    </>
  );
}

function CacheInvalidate({ csrf, tenant, type, close, changed }: {
  csrf: string; tenant: string; type: CacheType; close: () => void; changed: (epoch: number) => void;
}) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>();
  async function submit() {
    setPending(true);
    setError(undefined);
    try {
      const result = await callApi<{ epoch: number }>("admin/cache/invalidate", {
        method: "POST", csrf, body: { tenant_id: tenant, type, confirm: true },
      });
      changed(result.epoch);
      close();
    } catch (failure) {
      setError(failure);
      setPending(false);
    }
  }
  return (
    <Dialog title="确认使缓存失效" close={close} pending={pending}>
      <div className="stack">
        <dl><dt>目标租户</dt><dd><CopyId value={tenant} /></dd><dt>范围</dt><dd>{CACHE_TYPES[type]}</dd></dl>
        <p className="notice warning">此范围内已有的缓存将跳过，后续检索可能重新调用 Provider。</p>
        {error !== undefined && <Failure reason={error} />}
        <button type="button" className="danger" disabled={pending} onClick={() => void submit()}>{pending ? "正在提交…" : "确认失效"}</button>
      </div>
    </Dialog>
  );
}

const onOff = (value: boolean) => value ? "开启" : "关闭";

function retentionLabel(hours: number) {
  return hours === 0 ? "不保留" : `${hours} 小时`;
}

function RetentionEditor({ policy, csrf, changed }: {
  policy: RetentionPolicy;
  csrf: string;
  changed: (outcome: "saved" | "conflict") => void;
}) {
  const [draft, setDraft] = useState(String(policy.retention_hours));
  const [draftError, setDraftError] = useState("");
  const [preview, setPreview] = useState<number>();
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<unknown>();

  function previewChange(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!RETENTION_HOURS.test(draft)) {
      setDraftError("请输入 0 至 24 的整数。");
      return;
    }
    const hours = Number(draft);
    if (hours === policy.retention_hours) {
      setDraftError("保留时长与当前策略相同。");
      return;
    }
    setDraftError("");
    setSaveError(undefined);
    setPreview(hours);
  }

  async function confirmChange(hours: number) {
    setSaving(true);
    setSaveError(undefined);
    try {
      await callApi<RetentionPolicy>(RETENTION_PATH, {
        method: "PATCH",
        csrf,
        body: {
          tenant_id: policy.tenant_id,
          retention_hours: hours,
          expected_version: policy.version,
          confirm: true,
        },
      });
      setPreview(undefined);
      changed("saved");
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        setPreview(undefined);
        changed("conflict");
      } else {
        setSaveError(error);
      }
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <form method="post" className="actions" onSubmit={previewChange} noValidate>
        <label>原文保留小时数<input inputMode="numeric" value={draft} onChange={(event) => {
          setDraft(event.target.value);
          setDraftError("");
        }} /></label>
        <button type="submit">预览保留变更</button>
      </form>
      {draftError && <p className="notice danger" role="alert">{draftError}</p>}
      {preview !== undefined && (
        <Dialog title="确认查询原文保留变更" close={() => setPreview(undefined)} pending={saving}>
          <div className="stack" aria-busy={saving}>
            <dl>
              <dt>目标租户</dt><dd><CopyId value={policy.tenant_id} /></dd>
              <dt>当前策略</dt><dd>{retentionLabel(policy.retention_hours)}</dd>
              <dt>拟发布策略</dt><dd>{retentionLabel(preview)}</dd>
              <dt>基准版本</dt><dd>{policy.version}</dd>
            </dl>
            <p className="notice warning">确认后，旧版本原文会立即停止提供查看。本次变更会写入审计。</p>
            {saveError !== undefined && <Failure reason={saveError} />}
            <div className="actions">
              <button type="button" disabled={saving} onClick={() => setPreview(undefined)}>返回草稿</button>
              <button type="button" className="danger" disabled={saving} onClick={() => void confirmChange(preview)}>
                {saving ? "正在提交…" : "确认保留变更"}
              </button>
            </div>
          </div>
        </Dialog>
      )}
    </>
  );
}

function QueryRetention({ csrf, platform }: { csrf: string; platform: boolean }) {
  const [tenant, setTenant] = useState("");
  const [tenantError, setTenantError] = useState("");
  const [feedback, setFeedback] = useState<"saved" | "conflict">();
  const path = tenant ? `${RETENTION_PATH}?tenant_id=${encodeURIComponent(tenant)}` : RETENTION_PATH;
  const resource = useResource<RetentionPolicy>(path);
  const policy = resource.data;

  function chooseTenant(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const id = String(new FormData(event.currentTarget).get("tenant_id") ?? "").trim().toLowerCase();
    if (id && !TENANT_ID.test(id)) {
      setTenantError("请输入有效的租户 UUID。");
      return;
    }
    setTenantError("");
    setFeedback(undefined);
    if (id === tenant) resource.reload();
    else setTenant(id);
  }

  return (
    <section className="panel stack" aria-labelledby="query-retention-heading">
      <h2 id="query-retention-heading">查询原文短时保留</h2>
      <p className="muted">默认不保留查询原文。仅为指定租户启用时可设置 1 至 24 小时，输入 0 可关闭。普通运行日志只记录脱敏元数据。</p>
      {platform && (
        <>
          <form method="post" className="actions" onSubmit={chooseTenant} noValidate>
            <input name="tenant_id" aria-label="保留策略租户 ID" placeholder="留空读取当前租户" maxLength={36}
              onChange={() => setTenantError("")} />
            <button type="submit">读取租户策略</button>
            <Link href="/admin/tenants">查看租户 ID</Link>
          </form>
          {tenantError && <p className="notice danger" role="alert">{tenantError}</p>}
        </>
      )}
      {feedback === "saved" && <p className="notice positive" role="status">查询原文保留策略已更新。</p>}
      {feedback === "conflict" && <p className="notice warning" role="alert">策略已发生变更，请按最新版本重新预览。</p>}
      {resource.pending && <Loading />}
      {resource.error !== undefined && <Failure reason={resource.error} retry={resource.reload} />}
      {policy && (
        <>
          <dl>
            <dt>租户</dt><dd><CopyId value={policy.tenant_id} /></dd>
            <dt>当前保留策略</dt><dd>{retentionLabel(policy.retention_hours)}</dd>
            <dt>当前版本</dt><dd>{policy.version}</dd>
            <dt>更新时间</dt><dd>{dateTime(policy.updated_at)}</dd>
          </dl>
          {platform ? !resource.pending && (
            <RetentionEditor key={`${policy.tenant_id}:${policy.version}`} policy={policy} csrf={csrf} changed={(outcome) => {
              setFeedback(outcome);
              resource.reload();
            }} />
          ) : <p className="muted">运维角色可查看当前租户的策略与变更记录。</p>}
          <h3>最近 10 条变更</h3>
          {policy.changes.length ? (
            <Table caption="查询原文保留变更记录" headers={["版本", "原策略", "新策略", "发生时间"]}>
              {policy.changes.map((change) => (
                <tr key={change.version}>
                  <td>{change.version}</td><td>{retentionLabel(change.before_hours)}</td>
                  <td>{retentionLabel(change.after_hours)}</td>
                  <td><time dateTime={change.occurred_at}>{dateTime(change.occurred_at)}</time></td>
                </tr>
              ))}
            </Table>
          ) : <Empty>暂无保留策略变更。</Empty>}
        </>
      )}
    </section>
  );
}

export function SettingsView({ csrf, platform }: { csrf: string; platform: boolean }) {
  const resource = useResource<Settings>("admin/settings");
  const settings = resource.data;
  return (
    <>
      <div className="actions">
        <span className="muted">以下值由当前部署和服务策略决定。</span>
        <button type="button" onClick={resource.reload} disabled={resource.pending}>刷新设置</button>
      </div>
      {resource.pending && <Loading />}
      {resource.error !== undefined && <Failure reason={resource.error} retry={resource.reload} />}
      {settings && <div className="overview-grid">
        <section className="panel">
          <h2>运行环境和网络</h2>
          <dl>
            <dt>API 版本</dt><dd>{settings.environment.api_version}</dd>
            <dt>已配置 Provider</dt><dd>{settings.environment.providers.join("、") || "暂无"}</dd>
            <dt>公共网络搜索</dt><dd>{onOff(settings.environment.public_search)}</dd>
            <dt>图片搜索</dt><dd>{onOff(settings.environment.enable_images)}</dd>
          </dl>
        </section>
        <section className="panel">
          <h2>会话安全</h2>
          <dl>
            <dt>Console 来源</dt><dd>{settings.security.console_origin}</dd>
            <dt>Secure Cookie</dt><dd>{onOff(settings.security.secure_session_cookie)}</dd>
            <dt>CSRF 校验</dt><dd>{onOff(settings.security.require_csrf)}</dd>
            <dt>单次会话时长</dt><dd>{settings.security.session_ttl_hours} 小时</dd>
            <dt>会话总时长上限</dt><dd>{settings.security.session_absolute_ttl_days} 天</dd>
          </dl>
        </section>
        <section className="panel">
          <h2>数据保留</h2>
          <dl>
            <dt>用量元数据保留</dt><dd>{settings.retention.usage_metadata_days === null ? "未配置自动清理" : `${settings.retention.usage_metadata_days} 天`}</dd>
            <dt>请求幂等窗口</dt><dd>{settings.retention.idempotency_window_minutes} 分钟</dd>
          </dl>
        </section>
      </div>}
      <QueryRetention csrf={csrf} platform={platform} />
    </>
  );
}
