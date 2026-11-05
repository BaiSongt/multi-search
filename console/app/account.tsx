"use client";

import { useEffect, useState } from "react";
import { callApi } from "./lib/api";
import { roleLabels, type Session } from "./lib/contracts";
import { CopyId, dateTime, Dialog, Empty, Failure, Loading, Pagination, Table, usePagination, useResource } from "./ui";

type ManagedSession = {
  id: string;
  created_at: string;
  expires_at: string;
  current: boolean;
};

function ActiveSessions({ csrf, currentPending }: { csrf: string; currentPending: boolean }) {
  const pager = usePagination();
  const path = pager.cursor ? `auth/sessions?cursor=${encodeURIComponent(pager.cursor)}` : "auth/sessions";
  const resource = useResource<{ sessions: ManagedSession[]; next_cursor: string | null }>(path);
  const [selected, setSelected] = useState<ManagedSession>();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>();
  const [revoked, setRevoked] = useState(false);

  function reload() {
    pager.reset();
    resource.reload();
  }

  async function revoke(target: ManagedSession) {
    setPending(true);
    setError(undefined);
    try {
      await callApi(`auth/sessions/${encodeURIComponent(target.id)}/revoke`, { method: "POST", csrf });
      setSelected(undefined);
      setRevoked(true);
      reload();
    } catch (reason) {
      setError(reason);
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="panel stack" aria-labelledby="active-sessions-title">
      <h2 id="active-sessions-title">活跃会话</h2>
      <p className="muted">查看你在当前租户的浏览器会话，撤销其他浏览器中的访问。</p>
      <div className="actions">
        <button type="button" disabled={resource.pending || currentPending} onClick={reload}>刷新会话列表</button>
      </div>
      {revoked && <p role="status" className="notice">会话已撤销。</p>}
      {resource.pending && <Loading />}
      {resource.error !== undefined && <Failure reason={resource.error} retry={reload} />}
      {resource.data && <>
        {resource.data.sessions.length === 0
          ? <Empty>此页没有活跃会话。</Empty>
          : <Table caption="浏览器会话" headers={["会话 ID", "登录时间", "有效期", "操作"]}>
              {resource.data.sessions.map((row) => (
                <tr key={row.id}>
                  <td><CopyId value={row.id} /></td>
                  <td><time dateTime={row.created_at}>{dateTime(row.created_at)}</time></td>
                  <td><time dateTime={row.expires_at}>{dateTime(row.expires_at)}</time></td>
                  <td>{row.current ? <span className="badge">当前会话</span>
                    : <button type="button" disabled={resource.pending || currentPending} onClick={() => {
                      setError(undefined);
                      setRevoked(false);
                      setSelected(row);
                    }}>撤销会话</button>}</td>
                </tr>
              ))}
            </Table>}
        <Pagination next={resource.data.next_cursor} pager={pager} />
      </>}
      {selected && (
        <Dialog title="撤销其他会话" pending={pending} close={() => setSelected(undefined)}>
          <div className="stack">
            <p>确认撤销会话 <CopyId value={selected.id} />。该浏览器需要重新登录。</p>
            {error !== undefined && <Failure reason={error} />}
            <button type="button" className="danger" disabled={pending} onClick={() => void revoke(selected)}>
              {pending ? "正在撤销…" : "确认撤销会话"}
            </button>
          </div>
        </Dialog>
      )}
    </section>
  );
}

export function Account({ session }: { session: Session }) {
  const [pending, setPending] = useState<"refresh" | "logout">();
  const [error, setError] = useState<unknown>();
  const [localTime, setLocalTime] = useState(false);
  useEffect(() => setLocalTime(true), []);

  async function change(action: "refresh" | "logout") {
    setPending(action);
    setError(undefined);
    try {
      await callApi(`auth/${action}`, { method: "POST", csrf: session.csrf_token });
      if (action === "logout") window.location.assign("/auth");
      else window.location.reload();
    } catch (reason) {
      setError(reason);
      setPending(undefined);
    }
  }

  return (
    <div className="stack">
      <section className="panel">
        <h2>当前会话</h2>
        <dl>
          <dt>租户</dt><dd>{session.tenant}</dd>
          <dt>邮箱</dt><dd>{session.email}</dd>
          <dt>角色</dt><dd>{roleLabels[session.role]}</dd>
          <dt>会话有效期</dt>
          <dd><time dateTime={session.expires_at}>{localTime ? dateTime(session.expires_at) : session.expires_at}</time></dd>
        </dl>
        <div className="actions">
          <button type="button" disabled={pending !== undefined} onClick={() => void change("refresh")}>{pending === "refresh" ? "正在续期…" : "续期会话"}</button>
          <button type="button" className="danger" disabled={pending !== undefined} onClick={() => void change("logout")}>{pending === "logout" ? "正在登出…" : "登出"}</button>
        </div>
        {error !== undefined && <Failure reason={error} />}
      </section>
      <ActiveSessions csrf={session.csrf_token} currentPending={pending !== undefined} />
    </div>
  );
}
