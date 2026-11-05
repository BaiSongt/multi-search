"use client";

import { useState, type FormEvent } from "react";
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
      <label>租户标识<input name="tenant" required pattern="[a-z0-9][a-z0-9\-]{0,62}" autoComplete="organization" maxLength={63} /></label>
      <label>邮箱<input name="email" type="email" required autoComplete="username" maxLength={254} /></label>
      <label>密码<input name="password" type="password" required autoComplete="current-password" maxLength={128} /></label>
      {error && <p className="notice danger" role="alert">{error}</p>}
      <button type="submit" className="primary" disabled={pending}>{pending ? "正在登录…" : "登录"}</button>
    </form>
  );
}
