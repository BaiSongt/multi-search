import Link from "next/link";
import { notFound, redirect } from "next/navigation";
import { Account } from "./account";
import { AdminOverview, AuditView, ProvidersView, RequestsView, TenantsView } from "./admin-view";
import { DocsView } from "./docs-view";
import { BrandMark, Icon } from "./icons";
import { KeysView } from "./keys-view";
import { readSession } from "./lib/api-server";
import { adminNav, canView, consoleNav, roleLabels, workspaceHome, type Session, type Workspace as WorkspaceKind } from "./lib/contracts";
import { ImageQualityView, JobsView } from "./operations";
import { Playground } from "./playground";
import { RoutingView } from "./routing-view";
import { CacheView, SettingsView } from "./system-view";
import { TeamView } from "./team-view";
import { ThemeToggle } from "./ui";
import { UsageView } from "./usage-view";
import { sectionIcons, WorkspaceNav } from "./workspace-nav";

const subtitles: Record<string, string> = {
  overview: "按当前权限查看请求与资源使用情况。",
  keys: "管理凭据、授权范围、到期时间和轮换。",
  usage: "核对最近调用、结果用量和租户额度。",
  playground: "通过会话或 API Key 试用文字与图片搜索。",
  images: "使用归属信息、来源链接与许可状态筛选图片。",
  docs: "接口契约、权限和客户端接入示例。",
  team: "邀请成员并调整本租户角色。",
  providers: "查看检索能力、熔断状态、响应延迟和限流。",
  requests: "检查脱敏调用记录和请求状态。",
  jobs: "监控队列、任务进度和失败原因。",
  routing: "试算路由预算、发布版本和回滚策略。",
  tenants: "查看租户状态与资源使用情况。",
  quotas: "查看和配置租户请求上限。",
  audit: "查看密钥、Provider 和配额变更记录。",
  cache: "查看命中率和容量，按租户使指定缓存失效。",
};

function Body({ section, workspace, session, tenant }: { section: string; workspace: WorkspaceKind; session: Session; tenant?: string }) {
  const platform = session.role === "platform_admin";
  if (workspace === "console") {
    switch (section) {
      case "overview": return <UsageView overview />;
      case "usage": return <UsageView />;
      case "keys": return <KeysView csrf={session.csrf_token} />;
      case "playground": return <Playground csrf={session.csrf_token} sessionSearch={session.role !== "operations_admin"} />;
      case "images": return <Playground csrf={session.csrf_token} sessionSearch={session.role !== "operations_admin"} imageOnly />;
      case "docs": return <DocsView canManageKeys={canView(session.role, workspace, "keys")} />;
      case "team": return <TeamView csrf={session.csrf_token} platform={platform} />;
      case "settings": return <Account session={session} />;
    }
  }
  switch (section) {
    case "overview": return <AdminOverview />;
    case "providers": return <ProvidersView canEdit={platform} csrf={session.csrf_token} />;
    case "requests": return <RequestsView showTenant={platform} csrf={session.csrf_token} />;
    case "jobs": return <JobsView csrf={session.csrf_token} showTenant={platform} />;
    case "routing": return <RoutingView csrf={session.csrf_token} canEdit={platform} />;
    case "images": return <ImageQualityView showTenant={platform} />;
    case "tenants": return tenant ? <TeamView csrf={session.csrf_token} platform={platform} tenantId={tenant} /> : <TenantsView csrf={session.csrf_token} />;
    case "quotas": return <TenantsView csrf={session.csrf_token} quotas />;
    case "audit": return <AuditView />;
    case "cache": return <CacheView csrf={session.csrf_token} platform={platform} />;
    case "settings": return <SettingsView csrf={session.csrf_token} platform={platform} />;
  }
  notFound();
}

export async function WorkspacePage({ workspace, route }: { workspace: WorkspaceKind; route?: string[] }) {
  const tenant = workspace === "admin" && route?.length === 2 && route[0] === "tenants" && /^[a-f0-9-]{32,36}$/i.test(route[1])
    ? route[1] : undefined;
  if ((route?.length ?? 0) > 1 && !tenant) notFound();
  const nav: Record<string, string> = workspace === "admin" ? adminNav : consoleNav;
  if (route && !Object.hasOwn(nav, route[0])) notFound();
  const { session, error } = await readSession();
  const section = route?.[0] ?? (workspace === "admin" && (session?.role === "auditor" || session?.role === "tenant_admin") ? "audit" : "overview");
  const target = `/${workspace}/${section}${tenant ? `/${tenant}` : ""}`;
  if (error === "anonymous") redirect(`/auth?next=${encodeURIComponent(target)}`);
  if (!session) return <main className="entry" id="main"><h1>会话服务暂时不可用</h1><Link href={target}>重试</Link></main>;
  if (!route) redirect(target);
  const allowed = canView(session.role, workspace, section);
  const title = tenant ? "租户成员与邀请" : nav[section];
  const description = section === "settings"
    ? workspace === "admin" ? "核对运行环境、会话安全和数据保留策略。" : "查看当前租户、身份和会话有效期。"
    : workspace === "admin" && section === "images" ? "检查来源完整性与图片许可核验结果。"
    : tenant ? "查看成员、调整角色和邀请新成员。" : subtitles[section];
  const sectionIcon = sectionIcons[section as keyof typeof sectionIcons];
  return (
    <div className="workspace-shell">
      <header className="topbar">
        <Link href="/" className="brand"><BrandMark />MultiSearch</Link>
        <div className="actions workspace-actions">
          <div className="workspace-identity">
            <span className="identity-tenant desktop-only">{session.tenant}</span>
            <span className="badge">{roleLabels[session.role]}</span>
          </div>
          <ThemeToggle />
          <Link className="button workspace-icon-button" href="/auth?switch=1" aria-label="切换租户"><Icon name="swap" /><span className="desktop-only">切换租户</span></Link>
          <Link className="button workspace-icon-button" href="/console/settings" aria-label="账户"><Icon name="user" /><span className="desktop-only">账户</span></Link>
        </div>
      </header>
      <div className="layout">
        <WorkspaceNav role={session.role} workspace={workspace} section={section} target={target} />
        <main className="workspace-main" id="main" key={target} tabIndex={-1}>
          <div className="content">
            <div className="heading">
              <span className="heading-icon"><Icon name={sectionIcon} /></span>
              <div className="heading-copy">
                <p className="eyebrow">{workspace === "admin" ? "运维管理台" : "开发者门户"}</p>
                <h1>{title}</h1>
                <p className="muted">{description}</p>
                {tenant && allowed && <Link href="/admin/tenants">返回租户列表</Link>}
              </div>
            </div>
            {allowed ? <Body section={section} workspace={workspace} session={session} tenant={tenant} /> : (
              <div className="notice warning" role="alert"><p>当前角色无权访问此页面。</p><Link href={workspaceHome(session.role)}>返回可用工作区</Link></div>
            )}
          </div>
        </main>
      </div>
    </div>
  );
}
