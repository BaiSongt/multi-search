import { redirect } from "next/navigation";
import Link from "next/link";
import { readSession } from "../lib/api-server";
import { workspaceHome, workspaceTarget } from "../lib/contracts";
import { ThemeToggle } from "../ui";
import { SignIn } from "./sign-in";

export default async function AuthPage({ searchParams }: { searchParams: Promise<{ next?: string }> }) {
  const next = workspaceTarget((await searchParams).next);
  const { session, error } = await readSession();
  if (session) redirect(next ?? workspaceHome(session.role));
  return (
    <>
      <header className="topbar">
        <Link href="/" className="brand"><span className="brand-mark" aria-hidden>MS</span> MultiSearch</Link>
        <ThemeToggle />
      </header>
      <main id="main" className="entry">
        <p className="eyebrow">账户 / 登录</p>
        <h1>登录 MultiSearch</h1>
        <p className="muted">使用管理员提供的租户、邮箱和密码进入工作区。</p>
        {error === "unavailable" && <p role="alert" className="notice warning">暂时无法检查现有会话。</p>}
        <SignIn next={next} />
      </main>
    </>
  );
}
