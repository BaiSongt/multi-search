import Link from "next/link";
import { notFound, redirect } from "next/navigation";
import { Account } from "./account";
import { AdminOverview, AuditView, ProvidersView, RequestsView, TenantsView } from "./admin-view";
import { DocsView } from "./docs-view";
import { KeysView } from "./keys-view";
import { readSession } from "./lib/api-server";
import { adminNav, canView, consoleNav, roleLabels, workspaceHome, type Session, type Workspace as WorkspaceKind } from "./lib/contracts";
import { Playground } from "./playground";
import { ThemeToggle } from "./ui";
import { UsageView } from "./usage-view";

const subtitles: Record<string, string> = {
  overview: "按当前权限查看请求与资源使用情况。",
  keys: "管理凭据、授权范围、到期时间和轮换。",
  usage: "核对最近调用、结果用量和租户额度。",
  playground: "通过会话或 API Key 试用文字与图片搜索。",
  images: "使用归属信息、来源链接与许可状态筛选图片。",
  docs: "接口契约、权限和客户端接入示例。",
  settings: "查看当前租户、身份和会话有效期。",
  providers: "查看检索能力、熔断状态、响应延迟和限流。",
  requests: "检查脱敏调用记录和请求状态。",
  tenants: "查看租户状态与资源使用情况。",
  quotas: "查看和配置租户请求上限。",
  audit: "查看密钥、Provider 和配额变更记录。",
};

function Body({ section, workspace, session }: { section: string; workspace: WorkspaceKind; session: Session }) {
  if (workspace === "console") {
    switch (section) {
      case "overview": return <UsageView overview />;
      case "usage": return <UsageView />;
      case "keys": return <KeysView csrf={session.csrf_token} />;
      case "playground": return <Playground csrf={session.csrf_token} />;
      case "images": return <Playground csrf={session.csrf_token} imageOnly />;
      case "docs": return <DocsView />;
      case "settings": return <Account session={session} />;
    }
  }
  switch (section) {
    case "overview": return <AdminOverview />;
    case "providers": return <ProvidersView canEdit={session.role === "platform_admin"} csrf={session.csrf_token} />;
    case "requests": return <RequestsView showTenant={session.role === "platform_admin"} />;
    case "tenants": return <TenantsView csrf={session.csrf_token} />;
    case "quotas": return <TenantsView csrf={session.csrf_token} quotas />;
    case "audit": return <AuditView />;
  }
  notFound();
}

export async function WorkspacePage({ workspace, route }: { workspace: WorkspaceKind; route?: string[] }) {
  if ((route?.length ?? 0) > 1) notFound();
  const nav: Record<string, string> = workspace === "admin" ? adminNav : consoleNav;
  if (route && !Object.hasOwn(nav, route[0])) notFound();
  const { session, error } = await readSession();
  const section = route?.[0] ?? (workspace === "admin" && (session?.role === "auditor" || session?.role === "tenant_admin") ? "audit" : "overview");
  const target = `/${workspace}/${section}`;
  if (error === "anonymous") redirect(`/auth?next=${encodeURIComponent(target)}`);
  if (!session) return <main className="entry" id="main"><h1>会话服务暂时不可用</h1><Link href={target}>重试</Link></main>;
  if (!route) redirect(target);
  const allowed = canView(session.role, workspace, section);
  return (
    <>
      <header className="topbar">
        <Link href="/" className="brand"><span className="brand-mark" aria-hidden>MS</span> MultiSearch</Link>
        <div className="actions">
          <span className="muted desktop-only">{session.tenant}</span>
          <span className="badge">{roleLabels[session.role]}</span>
          <ThemeToggle />
          <Link className="button" href="/console/settings">账户</Link>
        </div>
      </header>
      <div className="layout">
        <nav className="sidenav" aria-label="工作区导航">
          {([["console", consoleNav, "开发者门户"], ["admin", adminNav, "运维管理台"]] as const).map(([space, sections, label]) => {
            const entries = Object.entries(sections).filter(([id]) => canView(session.role, space, id));
            return entries.length > 0 && (
              <div key={space}>
                <h2>{label}</h2>
                <ul>{entries.map(([id, text]) => (
                  <li key={id}><Link aria-current={space === workspace && id === section ? "page" : undefined} href={`/${space}/${id}`}>{text}</Link></li>
                ))}</ul>
              </div>
            );
          })}
        </nav>
        <main className="content" id="main">
          <div className="heading">
            <p className="eyebrow">{workspace === "admin" ? "运维管理台" : "开发者门户"} / {nav[section]}</p>
            <h1>{nav[section]}</h1>
            <p className="muted">{subtitles[section]}</p>
          </div>
          {allowed ? <Body section={section} workspace={workspace} session={session} /> : (
            <div className="notice warning" role="alert"><p>当前角色无权访问此页面。</p><Link href={workspaceHome(session.role)}>返回可用工作区</Link></div>
          )}
        </main>
      </div>
    </>
  );
}
