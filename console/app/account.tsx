"use client";

import { useEffect, useState } from "react";
import { callApi } from "./lib/api";
import { roleLabels, type Session } from "./lib/contracts";
import { dateTime, Failure } from "./ui";

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
  );
}
