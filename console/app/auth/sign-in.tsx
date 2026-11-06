"use client";

import { useState, type FormEvent } from "react";
import { Icon } from "../icons";
import { callApi, errorMessage } from "../lib/api";
import { workspaceHome, type Session } from "../lib/contracts";

export function SignIn({ next }: { next?: string }) {
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);

  async function signIn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const fields = new FormData(form);
    setError("");
    setPending(true);
    try {
      const session = await callApi<Session>("auth/login", {
        method: "POST",
        body: { tenant: fields.get("tenant"), email: fields.get("email"), password: fields.get("password") },
      });
      form.reset();
      window.location.assign(next ?? workspaceHome(session.role));
    } catch (reason) {
      setError(errorMessage(reason));
      setPending(false);
    }
  }

  return (
    <form method="post" onSubmit={signIn} className="stack login" aria-busy={pending}>
      <label>租户标识<span className="login-input"><Icon name="tenants" /><input name="tenant" required pattern="[a-z0-9][a-z0-9\-]{0,62}" autoComplete="organization" maxLength={63} placeholder="例如 platform" /></span></label>
      <label>邮箱<span className="login-input"><Icon name="mail" /><input name="email" type="email" required autoComplete="username" maxLength={254} placeholder="name@example.com" /></span></label>
      <label>密码<span className="login-input"><Icon name="lock" /><input name="password" type="password" required autoComplete="current-password" maxLength={128} placeholder="输入密码" /></span></label>
      {error && <p className="notice danger" role="alert">{error}</p>}
      <button type="submit" className="primary" disabled={pending}>{pending ? "正在登录…" : "登录"}{!pending && <Icon name="arrow" />}</button>
    </form>
  );
}
