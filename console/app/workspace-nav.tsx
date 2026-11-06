import Link from "next/link";
import type { ComponentProps } from "react";
import { Icon } from "./icons";
import { adminNav, canView, consoleNav, type Role, type Workspace } from "./lib/contracts";

const workspaces = {
  console: { label: "开发者门户", icon: "code", sections: consoleNav },
  admin: { label: "运维管理台", icon: "admin", sections: adminNav },
} as const;

const groups = {
  console: [
    { title: "概览", sections: ["overview"] },
    { title: "搜索与接入", sections: ["playground", "images", "keys", "docs"] },
    { title: "用量与账户", sections: ["usage", "team", "settings"] },
  ],
  admin: [
    { title: "服务监控", sections: ["overview", "requests", "jobs"] },
    { title: "搜索服务", sections: ["providers", "routing", "images"] },
    { title: "平台管理", sections: ["tenants", "quotas", "audit", "cache", "settings"] },
  ],
} as const satisfies {
  [Space in Workspace]: readonly { title: string; sections: readonly (keyof typeof workspaces[Space]["sections"])[] }[];
};

export const sectionIcons: Record<keyof typeof adminNav | keyof typeof consoleNav, ComponentProps<typeof Icon>["name"]> = {
  overview: "overview", providers: "providers", requests: "requests", jobs: "jobs",
  routing: "routing", images: "images", tenants: "tenants", quotas: "quotas",
  audit: "audit", cache: "cache", settings: "settings", keys: "keys", usage: "quotas",
  playground: "search", docs: "docs", team: "team",
};

export function WorkspaceNav({ role, workspace, section, target }: {
  role: Role;
  workspace: Workspace;
  section: string;
  target: string;
}) {
  const { sections, label, icon } = workspaces[workspace];
  const labels: Record<string, string> = sections;
  const available = (["console", "admin"] as const).flatMap((space) => {
    const info = workspaces[space];
    // Match the preferred entry for roles that cannot open the overview.
    const first = Object.keys(info.sections).find((id) => canView(role, space, id));
    if (!first) return [];
    const home = space === "admin" && (role === "auditor" || role === "tenant_admin") ? "audit" : first;
    return [{ space, ...info, href: `/${space}/${home}` }];
  });

  function Menu() {
    return (
      <>
        {available.length === 1 && available[0].space === workspace ? (
          <div className="workspace-nav-title"><Icon name={icon} />{label}</div>
        ) : (
          <nav className="workspace-switch" aria-label="切换工作区">
            {available.map(({ space, label, icon, href }) => (
              <Link key={space} href={href} aria-current={workspace === space ? "true" : undefined}>
                <Icon name={icon} />{label}
              </Link>
            ))}
          </nav>
        )}
        <nav className="sidenav" aria-label={`${label}功能`}>
          {groups[workspace].map((group) => {
            const entries = group.sections.filter((id) => canView(role, workspace, id));
            if (!entries.length) return null;
            return (
              <div className="nav-group" key={group.title}>
                <h2>{group.title}</h2>
                <ul>{entries.map((id) => (
                  <li key={id}>
                    <Link aria-current={id === section ? "page" : undefined} href={`/${workspace}/${id}`}>
                      <Icon name={sectionIcons[id]} /><span>{labels[id]}</span>
                    </Link>
                  </li>
                ))}</ul>
              </div>
            );
          })}
        </nav>
      </>
    );
  }

  return (
    <aside className="workspace-sidebar">
      <div className="workspace-sidebar-desktop"><Menu /></div>
      <details className="workspace-sidebar-mobile" key={target}>
        <summary>
          <Icon name="menu" />
          <span className="mobile-menu-label">功能菜单</span>
          <span className="mobile-menu-section">{labels[section]}</span>
          <Icon name="chevron" />
        </summary>
        <div className="workspace-mobile-menu"><Menu /></div>
      </details>
    </aside>
  );
}
