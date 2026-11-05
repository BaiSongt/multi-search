"use client";

import { useState, type FormEvent } from "react";
import { callApi } from "./lib/api";
import { CopyId, Dialog, Failure, formatNumber, Loading, useResource } from "./ui";
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

export function SettingsView() {
  const resource = useResource<Settings>("admin/settings");
  if (resource.pending) return <Loading />;
  if (!resource.data) return <Failure reason={resource.error} retry={resource.reload} />;
  const { environment, security, retention } = resource.data;
  return (
    <>
      <div className="actions">
        <span className="muted">以下值由当前部署和服务策略决定。</span>
        <button type="button" onClick={resource.reload}>刷新设置</button>
      </div>
      <div className="overview-grid">
        <section className="panel">
          <h2>运行环境和网络</h2>
          <dl>
            <dt>API 版本</dt><dd>{environment.api_version}</dd>
            <dt>已配置 Provider</dt><dd>{environment.providers.join("、") || "暂无"}</dd>
            <dt>公共网络搜索</dt><dd>{onOff(environment.public_search)}</dd>
            <dt>图片搜索</dt><dd>{onOff(environment.enable_images)}</dd>
          </dl>
        </section>
        <section className="panel">
          <h2>会话安全</h2>
          <dl>
            <dt>Console 来源</dt><dd>{security.console_origin}</dd>
            <dt>Secure Cookie</dt><dd>{onOff(security.secure_session_cookie)}</dd>
            <dt>CSRF 校验</dt><dd>{onOff(security.require_csrf)}</dd>
            <dt>单次会话时长</dt><dd>{security.session_ttl_hours} 小时</dd>
            <dt>会话总时长上限</dt><dd>{security.session_absolute_ttl_days} 天</dd>
          </dl>
        </section>
        <section className="panel">
          <h2>数据保留</h2>
          <dl>
            <dt>用量元数据保留</dt><dd>{retention.usage_metadata_days === null ? "未配置自动清理" : `${retention.usage_metadata_days} 天`}</dd>
            <dt>请求幂等窗口</dt><dd>{retention.idempotency_window_minutes} 分钟</dd>
          </dl>
        </section>
      </div>
    </>
  );
}
