"use client";

import { useState, type FormEvent } from "react";
import { callApi } from "./lib/api";
import { quotaLabels, type Capabilities, type IssuedKey, type KeyPage, type KeyRow } from "./lib/contracts";
import { Copy, CopyId, dateTime, Dialog, Empty, Failure, Loading, Pagination, Table, usePagination, useResource } from "./ui";

const statuses = { active: "启用", expired: "已过期", revoked: "已吊销", rotating: "轮换中" };
const MAX_ALLOWED_NETWORKS = 32;
const missingScope = "请至少选择一个作用域。";
const tooManyNetworks = `IP 白名单最多支持 ${MAX_ALLOWED_NETWORKS} 项。`;
const usageExamples = [
  { scope: "search:text", path: "/api/v1/search", body: { query: "NASA", modalities: ["text"] } },
  { scope: "search:image", path: "/api/v1/search", body: { query: "NASA", modalities: ["image"] } },
  { scope: "fetch", path: "/api/v1/fetch", body: { urls: ["https://example.org/"], max_pages: 1 } },
  { scope: "research", path: "/api/v1/research", body: { query: "NASA", mode: "deep" } },
];

type Action = { type: "create" } | { type: "rotate" | "revoke"; key: KeyRow };

function IpAllowlist({ networks }: { networks?: string[] }) {
  if (!networks?.length) return <span className="muted">未限制</span>;
  return (
    <details>
      <summary>{networks.length} 个地址或网段</summary>
      {networks.map((network) => <div key={network}><code>{network}</code></div>)}
    </details>
  );
}

function KeyUsageExample({ scopes }: { scopes: string[] }) {
  const example = usageExamples.find(({ scope }) => scopes.includes(scope));
  if (!example) return null;
  return (
    <details>
      <summary>curl 使用示例</summary>
      <p>从安全存储设置 <code>MULTISEARCH_KEY</code>，并将 <code>MULTISEARCH_API_URL</code> 设为 API 基础地址。</p>
      <pre>{[
        'printf \'Authorization: Bearer %s\\n\' "$MULTISEARCH_KEY" |',
        `  curl --url "$MULTISEARCH_API_URL${example.path}" \\`,
        "    --header @- --header 'Content-Type: application/json' \\",
        `    --data '${JSON.stringify(example.body)}'`,
      ].join("\n")}</pre>
      <a href="/console/docs" target="_blank" rel="noreferrer">查看接入文档</a>
    </details>
  );
}

export function KeysView({ csrf }: { csrf: string }) {
  const [action, setAction] = useState<Action>();
  const [filter, setFilter] = useState("");
  const [status, setStatus] = useState("");
  const pager = usePagination();
  const params = new URLSearchParams({ limit: "20" });
  if (pager.cursor) params.set("cursor", pager.cursor);
  const resource = useResource<KeyPage>(`keys?${params}`);
  const rows = resource.data?.keys.filter((key) =>
    (!status || status === key.status) &&
    (!filter || `${key.name} ${key.id} ${key.prefix} ${key.suffix}`.toLowerCase().includes(filter.trim().toLowerCase()))
  );
  return (
    <>
      <div className="actions">
        <button type="button" className="primary" onClick={() => setAction({ type: "create" })}>新建密钥</button>
        <input aria-label="筛选本页密钥" placeholder="筛选本页名称或 ID" value={filter} onChange={(event) => setFilter(event.target.value)} />
        <select aria-label="筛选本页状态" value={status} onChange={(event) => setStatus(event.target.value)}>
          <option value="">全部状态</option>
          {Object.entries(statuses).map(([key, title]) => <option key={key} value={key}>{title}</option>)}
        </select>
        <button type="button" onClick={resource.reload} disabled={resource.pending}>刷新</button>
      </div>
      {resource.pending && <Loading />}
      {resource.error !== undefined && <Failure reason={resource.error} retry={resource.reload} />}
      {rows && !rows.length && <Empty>{filter || status ? "本页没有符合筛选条件的密钥。" : "暂无可管理的密钥。"}</Empty>}
      {rows && rows.length > 0 && (
        <Table caption="密钥列表" headers={["名称与密钥", "环境", "作用域", "IP 白名单", "状态", "创建时间", "最后使用", "到期时间", "操作"]}>
          {rows.map((key) => (
            <tr key={key.id}>
              <td><strong>{key.name}</strong><br /><code>{key.prefix}…{key.suffix}</code><br /><CopyId value={key.id} /></td>
              <td><span className="badge">{key.environment}</span></td>
              <td>{key.scopes.map((scope) => <div key={scope}><code>{scope}</code></div>)}</td>
              <td><IpAllowlist networks={key.ip_allowlist} /></td>
              <td><span className={`badge ${key.status === "active" ? "positive" : key.status === "rotating" ? "warning" : ""}`}>{statuses[key.status]}</span></td>
              <td>{dateTime(key.created_at)}</td>
              <td>{dateTime(key.last_used_at)}</td>
              <td>{dateTime(key.expires_at)}</td>
              <td className="actions">
                {key.status === "active" && <button type="button" onClick={() => setAction({ type: "rotate", key })}>轮换</button>}
                {(key.status === "active" || key.status === "rotating") && (
                  <button type="button" className="danger" onClick={() => setAction({ type: "revoke", key })}>吊销</button>
                )}
              </td>
            </tr>
          ))}
        </Table>
      )}
      {resource.data && <Pagination next={resource.data.next_cursor} pager={pager} />}
      {action && (
        <KeyAction
          action={action}
          csrf={csrf}
          close={() => setAction(undefined)}
          changed={() => { if (action.type !== "revoke") pager.reset(); resource.reload(); }}
        />
      )}
    </>
  );
}

function KeyAction({ action, csrf, close, changed }: { action: Action; csrf: string; close: () => void; changed: () => void }) {
  const [pending, setPending] = useState(false);
  const [issued, setIssued] = useState<IssuedKey>();
  const [error, setError] = useState<unknown>();
  const capabilities = useResource<Capabilities>("capabilities");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setError(undefined);
    const fields = new FormData(event.currentTarget);
    try {
      if (action.type === "revoke") {
        await callApi(`keys/${action.key.id}/revoke`, { method: "POST", csrf });
        changed();
        close();
      } else {
        const ttl = fields.get("ttl_hours");
        let path: string;
        let body: unknown;
        if (action.type === "create") {
          const scopes = fields.getAll("scope");
          if (!scopes.length) throw new Error(missingScope);
          const ipAllowlist = String(fields.get("ip_allowlist") ?? "").split(/[,，\s]+/).filter(Boolean);
          if (ipAllowlist.length > MAX_ALLOWED_NETWORKS) throw new Error(tooManyNetworks);
          const limits = Object.fromEntries(Object.keys(quotaLabels).flatMap((name) => {
            const value = fields.get(name);
            return value ? [[name, Number(value)]] : [];
          }));
          path = "keys";
          body = {
            name: fields.get("name"), environment: fields.get("environment"), scopes, ttl_hours: Number(ttl), limits,
            ...(ipAllowlist.length ? { ip_allowlist: ipAllowlist } : {}),
          };
        } else {
          path = `keys/${action.key.id}/rotate`;
          body = { grace_seconds: Number(fields.get("grace_seconds")), ...(ttl ? { ttl_hours: Number(ttl) } : {}) };
        }
        setIssued(await callApi<IssuedKey>(path, { method: "POST", csrf, body }));
        changed();
      }
    } catch (reason) {
      setError(reason);
    } finally {
      setPending(false);
    }
  }

  const title = issued ? "保存 API Key" : action.type === "create" ? "新建密钥" : action.type === "rotate" ? "轮换密钥" : "吊销密钥";
  return (
    <Dialog title={title} close={close} pending={pending}>
      {issued ? (
        <div className="stack">
          <p className="notice warning" role="status">密钥明文仅在此处展示一次。请安全保存。</p>
          <code className="secret" data-secret>{issued.secret}</code>
          <Copy value={issued.secret} label="复制密钥" />
          <KeyUsageExample scopes={issued.key.scopes} />
          <button type="button" className="primary" onClick={close}>已安全保存</button>
        </div>
      ) : (
        <form method="post" onSubmit={submit} className="stack" aria-busy={pending}>
          {action.type === "create" ? (
            <>
              <label>密钥名称<input name="name" required maxLength={80} placeholder="例如：集成测试" /></label>
              <div className="form-grid">
                <label>环境<select name="environment" defaultValue="dev"><option>dev</option><option>test</option><option>prod</option></select></label>
                <label>有效期（小时）<input name="ttl_hours" type="number" defaultValue={168} required min={1} max={720} /></label>
              </div>
              <fieldset>
                <legend>作用域</legend>
                <label className="check"><input type="checkbox" name="scope" value="search:text" defaultChecked />文字搜索</label>
                <label className="check"><input type="checkbox" name="scope" value="search:image" defaultChecked />图片搜索</label>
                <label className="check"><input type="checkbox" name="scope" value="fetch" />网页提取</label>
                {capabilities.data?.research === "available" && <label className="check"><input type="checkbox" name="scope" value="research" />深度研究</label>}
              </fieldset>
              <label>IP 白名单（可选）
                <textarea name="ip_allowlist" rows={3} maxLength={2048} autoComplete="off" spellCheck={false}
                  placeholder="192.0.2.17, 2001:db8::/32" aria-describedby="key-ip-allowlist-help" />
              </label>
              <p id="key-ip-allowlist-help" className="muted">填写 IPv4、IPv6 地址或 CIDR 网段，逗号或换行分隔，最多 {MAX_ALLOWED_NETWORKS} 项。留空允许任意来源。受限密钥通过直连 REST、MCP 或可信反向代理使用。Console Playground 无法核验客户端地址。</p>
              <details>
                <summary>可选密钥额度</summary>
                <div className="form-grid">
                  {Object.entries(quotaLabels).map(([id, label]) => (
                    <label key={id}>{label}<input name={id} type="number" min={1} placeholder="继承租户上限" /></label>
                  ))}
                </div>
              </details>
            </>
          ) : action.type === "rotate" ? (
            <>
              <p>将为「{action.key.name}」创建一个新密钥。</p>
              {Boolean(action.key.ip_allowlist?.length) && <div>
                <p>新密钥将继承原 IP 白名单。</p>
                <IpAllowlist networks={action.key.ip_allowlist} />
              </div>}
              <label>旧密钥过渡期（秒）<input name="grace_seconds" type="number" defaultValue={0} required min={0} max={86400} /></label>
              <label>新密钥有效期（小时）<input name="ttl_hours" type="number" min={1} max={720} placeholder="沿用当前策略" /></label>
            </>
          ) : (
            <p className="notice warning">吊销「{action.key.name}」后，使用该密钥的请求将被拒绝。</p>
          )}
          {error !== undefined && (
            error instanceof Error && [missingScope, tooManyNetworks].includes(error.message)
              ? <p className="notice danger" role="alert">{error.message}</p>
              : <Failure reason={error} />
          )}
          <button type="submit" disabled={pending} className={action.type === "revoke" ? "danger" : "primary"}>
            {pending ? "正在处理…" : action.type === "create" ? "生成密钥" : action.type === "rotate" ? "确认轮换" : "确认吊销"}
          </button>
        </form>
      )}
    </Dialog>
  );
}
