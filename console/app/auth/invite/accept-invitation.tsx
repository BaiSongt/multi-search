"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { callApi } from "../../lib/api";
import { Failure } from "../../ui";

export function AcceptInvitation() {
  const token = useRef("");
  const [available, setAvailable] = useState<boolean>();
  const [accepted, setAccepted] = useState<{ tenant: string; email: string }>();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>();

  useEffect(() => {
    const fragment = location.hash.slice(1);
    if (fragment) {
      const value = new URLSearchParams(fragment).get("token") ?? "";
      token.current = value.length <= 512 ? value : "";
      history.replaceState(history.state, "", location.pathname);
    }
    setAvailable(Boolean(token.current));
  }, []);

  async function accept(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const fields = new FormData(form);
    const password = fields.get("password");
    if (password !== fields.get("confirm_password")) {
      setError("两次输入的密码不同。");
      return;
    }
    setError(undefined);
    setPending(true);
    try {
      const result = await callApi<{ tenant: string; email: string }>("auth/invitations/accept", {
        method: "POST", body: { token: token.current, password },
      });
      token.current = "";
      form.reset();
      setAccepted(result);
    } catch (reason) {
      setError(reason);
    } finally {
      setPending(false);
    }
  }

  if (accepted) return (
    <div className="stack">
      <p className="notice positive" role="status">已加入租户 {accepted.tenant}。</p>
      <p className="muted">登录邮箱 {accepted.email}</p>
      <a className="button primary" href="/auth">进入登录</a>
    </div>
  );
  if (available === undefined) return <p role="status">正在读取邀请链接…</p>;
  if (!available) return <p className="notice warning" role="alert">邀请令牌缺失。请重新打开管理员发送的完整链接。</p>;
  return (
    <form method="post" onSubmit={accept} className="stack" aria-busy={pending}>
      <p className="muted">首次加入时设置密码。已有 MultiSearch 账户时输入当前密码。</p>
      <label>账户密码<input type="password" name="password" minLength={12} maxLength={128} autoComplete="off" required /></label>
      <label>确认密码<input type="password" name="confirm_password" minLength={12} maxLength={128} autoComplete="off" required /></label>
      {typeof error === "string" ? <p className="notice danger" role="alert">{error}</p> : error !== undefined && <Failure reason={error} />}
      <button type="submit" className="primary" disabled={pending}>{pending ? "正在接受…" : "接受邀请"}</button>
    </form>
  );
}
