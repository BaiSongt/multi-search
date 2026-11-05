import { redirect } from "next/navigation";
import Link from "next/link";
import { readSession } from "../lib/api-server";
import { roleLabels, workspaceHome, workspaceTarget } from "../lib/contracts";
import { ThemeToggle } from "../ui";
import { SignIn } from "./sign-in";
import { SwitchMembership } from "./switch-membership";

export default async function AuthPage({ searchParams }: { searchParams: Promise<{ next?: string; switch?: string }> }) {
  const params = await searchParams;
  const next = workspaceTarget(params.next);
  const { session, error } = await readSession();
  if (session && params.switch !== "1") redirect(next ?? workspaceHome(session.role));
  return (
    <>
      <header className="topbar">
        <Link href="/" className="brand"><span className="brand-mark" aria-hidden>MS</span> MultiSearch</Link>
        <ThemeToggle />
      </header>
      <main id="main" className="entry">
        {session ? <>
          <p className="eyebrow">账户 / 租户身份</p>
          <h1>切换租户身份</h1>
          <p className="muted">当前在「{session.tenant}」以{roleLabels[session.role]}身份登录。</p>
          <SwitchMembership csrf={session.csrf_token} />
          <Link href={workspaceHome(session.role)}>返回工作区</Link>
        </> : <>
          <p className="eyebrow">账户 / 登录</p>
          <h1>登录 MultiSearch</h1>
          <p className="muted">使用管理员提供的租户、邮箱和密码进入工作区。</p>
          {error === "unavailable" && <p role="alert" className="notice warning">暂时无法检查现有会话。</p>}
          <SignIn next={next} />
        </>}
      </main>
    </>
  );
}
