import { redirect } from "next/navigation";
import Link from "next/link";
import { BrandMark, Icon } from "../icons";
import { readSession } from "../lib/api-server";
import { roleLabels, workspaceHome, workspaceTarget } from "../lib/contracts";
import { ThemeToggle } from "../ui";
import { SignIn } from "./sign-in";
import { SwitchMembership } from "./switch-membership";

function SignInIntro() {
  return (
    <div className="auth-intro">
      <div className="auth-intro-copy">
        <p className="auth-kicker"><span /> MULTISEARCH WORKSPACE</p>
        <h2>统一检索<br />与运维工作区</h2>
        <p>在同一处管理检索服务、租户权限和调用记录。</p>
      </div>
      <svg className="auth-art" viewBox="0 0 440 310" fill="none" aria-hidden="true">
        <path d="M-15 223h133l52-52h110l52 52h126 M-15 270h112l53-53h137l55 55h116 M220 20v65m0 142v65" stroke="#8bb7ff" strokeOpacity=".23" />
        <circle cx="220" cy="156" r="116" stroke="#87a9ff" strokeOpacity=".2" />
        <circle cx="220" cy="156" r="93" stroke="#87a9ff" strokeOpacity=".35" strokeDasharray="3 7" />
        <circle cx="220" cy="156" r="71" fill="#3862d9" fillOpacity=".22" stroke="#98b8ff" strokeOpacity=".6" />
        <circle cx="220" cy="156" r="47" fill="#4d75eb" fillOpacity=".24" stroke="#aac7ff" />
        <path d="m201 155 13 13 25-26" stroke="#c6f2ff" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
        <path d="M58 138h74l33 32m111 0 33-32h74" stroke="#81c6fa" strokeOpacity=".5" />
        <circle cx="58" cy="138" r="4" fill="#94dfff" /><circle cx="383" cy="138" r="4" fill="#94dfff" />
        <circle cx="220" cy="40" r="4" fill="#bcefff" /><circle cx="220" cy="272" r="4" fill="#bcefff" />
        <circle cx="108" cy="223" r="3" fill="#87a9ff" /><circle cx="332" cy="223" r="3" fill="#87a9ff" />
      </svg>
      <div className="auth-features">
        <span><Icon name="search" />多源检索</span>
        <span><Icon name="team" />租户权限</span>
        <span><Icon name="audit" />调用审计</span>
      </div>
    </div>
  );
}

export default async function AuthPage({ searchParams }: { searchParams: Promise<{ next?: string; switch?: string }> }) {
  const params = await searchParams;
  const next = workspaceTarget(params.next);
  const { session, error } = await readSession();
  if (session && params.switch !== "1") redirect(next ?? workspaceHome(session.role));
  return (
    <div className="auth-shell">
      <header className="topbar">
        <Link href="/" className="brand"><BrandMark />MultiSearch</Link>
        <ThemeToggle />
      </header>
      {session ? <main id="main" className="entry auth-switch">
          <p className="eyebrow">账户 / 租户身份</p>
          <h1>切换租户身份</h1>
          <p className="muted">当前在「{session.tenant}」以{roleLabels[session.role]}身份登录。</p>
          <SwitchMembership csrf={session.csrf_token} />
          <Link href={workspaceHome(session.role)}>返回工作区</Link>
        </main> : <main id="main" className="auth-main">
          <div className="auth-card">
            <SignInIntro />
            <div className="auth-form">
              <span className="heading-icon"><Icon name="lock" /></span>
              <h1>登录 MultiSearch</h1>
              <p className="muted">使用管理员提供的租户、邮箱和密码进入工作区。</p>
              {error === "unavailable" && <p role="alert" className="notice warning">暂时无法检查现有会话。</p>}
              <SignIn next={next} />
            </div>
          </div>
        </main>}
    </div>
  );
}
