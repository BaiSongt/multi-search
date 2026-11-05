"use client";

import { useState, type FormEvent } from "react";
import { callApi } from "./lib/api";
import { roleLabels, type Invitation, type IssuedInvitation, type Member, type Page, type Role } from "./lib/contracts";
import { Copy, CopyId, dateTime, Dialog, Empty, Failure, Loading, Pagination, Table, usePagination, useResource } from "./ui";

const INVITATION_STATUS: Record<string, string> = { pending: "待接受", accepted: "已接受", expired: "已过期", revoked: "已撤销" };
const BASIC_ROLES: Role[] = ["developer", "tenant_admin", "auditor"];
type Action = { type: "invite" } | { type: "member"; member: Member } | { type: "revoke"; invite: Invitation };

export function TeamView({ csrf, platform, tenantId }: { csrf: string; platform: boolean; tenantId?: string }) {
  const [section, setSection] = useState<"members" | "invitations">("members");
  const [filter, setFilter] = useState("");
  const [action, setAction] = useState<Action>();
  const pager = usePagination();
  const prefix = tenantId ? `admin/tenants/${encodeURIComponent(tenantId)}` : "team";
  const query = new URLSearchParams({ limit: "20" });
  if (pager.cursor) query.set("cursor", pager.cursor);
  const resource = useResource<Page<Member | Invitation>>(`${prefix}/${section}?${query}`);
  const rows = resource.data?.items.filter((row) => row.email.toLowerCase().includes(filter.trim().toLowerCase()));
  const roles = platform ? Object.keys(roleLabels) as Role[] : BASIC_ROLES;
  return (
    <>
      <div className="actions">
        <button type="button" className={section === "members" ? "primary" : ""} aria-pressed={section === "members"} onClick={() => { pager.reset(); setSection("members"); }}>成员</button>
        <button type="button" className={section === "invitations" ? "primary" : ""} aria-pressed={section === "invitations"} onClick={() => { pager.reset(); setSection("invitations"); }}>邀请记录</button>
        <input aria-label="筛选本页邮箱" placeholder="筛选本页邮箱" value={filter} onChange={(event) => setFilter(event.target.value)} />
        <button type="button" className="primary" onClick={() => setAction({ type: "invite" })}>邀请成员</button>
        <button type="button" disabled={resource.pending} onClick={resource.reload}>刷新</button>
      </div>
      {resource.pending && <Loading />}
      {resource.error !== undefined && <Failure reason={resource.error} retry={resource.reload} />}
      {rows?.length === 0 && <Empty>{section === "members" ? "没有符合条件的成员。" : "没有符合条件的邀请。"}</Empty>}
      {rows && rows.length > 0 && (section === "members" ? (
        <Table caption="团队成员" headers={["邮箱", "用户 ID", "角色", "成员状态", "账户状态", "操作"]}>
          {(rows as Member[]).map((member) => (
            <tr key={member.user_id}>
              <td>{member.email}</td><td><CopyId value={member.user_id} /></td><td>{roleLabels[member.role]}</td>
              <td><span className={`badge ${member.enabled ? "positive" : "warning"}`}>{member.enabled ? "启用" : "停用"}</span></td>
              <td>{!member.account_enabled ? "账户停用" : member.has_password ? "已激活" : "待激活"}</td>
              <td>{roles.includes(member.role) && <button type="button" onClick={() => setAction({ type: "member", member })}>调整权限</button>}</td>
            </tr>
          ))}
        </Table>
      ) : (
        <Table caption="成员邀请" headers={["邮箱", "邀请 ID", "角色", "状态", "创建时间", "到期时间", "操作"]}>
          {(rows as Invitation[]).map((invite) => (
            <tr key={invite.id}>
              <td>{invite.email}</td><td><CopyId value={invite.id} /></td><td>{roleLabels[invite.role]}</td>
              <td><span className="badge">{INVITATION_STATUS[invite.status] ?? invite.status}</span></td>
              <td>{dateTime(invite.created_at)}</td><td>{dateTime(invite.expires_at)}</td>
              <td>{invite.status === "pending" && <button type="button" className="danger" onClick={() => setAction({ type: "revoke", invite })}>撤销邀请</button>}</td>
            </tr>
          ))}
        </Table>
      ))}
      {resource.data && <Pagination next={resource.data.next_cursor} pager={pager} />}
      {action && (
        <TeamAction action={action} roles={roles} csrf={csrf} prefix={prefix} close={() => setAction(undefined)} changed={() => {
          pager.reset();
          setFilter("");
          if (action.type === "invite") setSection("invitations");
          resource.reload();
        }} />
      )}
    </>
  );
}

function TeamAction({ action, roles, csrf, prefix, close, changed }: {
  action: Action; roles: Role[]; csrf: string; prefix: string; close: () => void; changed: () => void;
}) {
  const [link, setLink] = useState("");
  const [draft, setDraft] = useState<{ role: Role; enabled: boolean }>();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>();
  async function mutate(path: string, method: "POST" | "PATCH", body?: unknown) {
    setPending(true);
    setError(undefined);
    try {
      if (action.type === "invite") {
        const result = await callApi<IssuedInvitation>(path, { method, body, csrf });
        const url = new URL("/auth/invite", window.location.origin);
        // 使用片段传递单次令牌，避免进入服务端访问日志。
        url.hash = new URLSearchParams({ token: result.token }).toString();
        setLink(url.href);
      } else {
        await callApi(path, { method, body, csrf });
        close();
      }
      changed();
    } catch (reason) {
      setError(reason);
    } finally {
      setPending(false);
    }
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const fields = new FormData(event.currentTarget);
    if (action.type === "invite") {
      void mutate(`${prefix}/invitations`, "POST", {
        email: fields.get("email"), role: fields.get("role"), expires_in_hours: Number(fields.get("expires_in_hours")),
      });
    } else if (action.type === "revoke") {
      void mutate(`${prefix}/invitations/${encodeURIComponent(action.invite.id)}/revoke`, "POST");
    } else {
      setDraft({ role: fields.get("role") as Role, enabled: fields.get("enabled") === "true" });
    }
  }

  const title = link ? "保存邀请链接" : action.type === "invite" ? "邀请成员" : action.type === "revoke" ? "撤销邀请" : "调整成员权限";
  return (
    <Dialog title={title} close={close} pending={pending}>
      {link ? (
        <div className="stack">
          <p className="notice warning" role="status">邀请链接只展示一次。请将链接发送给预期的成员。</p>
          <code className="secret" data-invitation-link>{link}</code>
          <Copy value={link} label="复制邀请链接" />
          <button type="button" className="primary" onClick={close}>已保存邀请链接</button>
        </div>
      ) : draft && action.type === "member" ? (
        <div className="stack">
          <h3>确认权限变更</h3>
          <p>{action.member.email} · {roleLabels[action.member.role]} → {roleLabels[draft.role]}</p>
          <p>成员状态 {action.member.enabled ? "启用" : "停用"} → {draft.enabled ? "启用" : "停用"}</p>
          {error !== undefined && <Failure reason={error} />}
          <div className="actions">
            <button type="button" disabled={pending} onClick={() => setDraft(undefined)}>返回</button>
            <button type="button" className="primary" disabled={pending || (draft.role === action.member.role && draft.enabled === action.member.enabled)}
              onClick={() => void mutate(`${prefix}/members/${encodeURIComponent(action.member.user_id)}`, "PATCH", draft)}>确认调整</button>
          </div>
        </div>
      ) : (
        <form method="post" className="stack" onSubmit={submit} aria-busy={pending}>
          {action.type === "invite" && (
            <>
              <label>邀请邮箱<input name="email" type="email" required maxLength={254} /></label>
              <label>有效期（小时）<input name="expires_in_hours" type="number" min={1} max={168} defaultValue={48} required /></label>
            </>
          )}
          {action.type !== "revoke" && (
            <label>角色<select name="role" defaultValue={action.type === "member" ? action.member.role : "developer"}>
              {roles.map((role) => <option key={role} value={role}>{roleLabels[role]}</option>)}
            </select></label>
          )}
          {action.type === "member" && (
            <label>成员状态<select name="enabled" defaultValue={String(action.member.enabled)}>
              <option value="true">启用</option><option value="false">停用</option>
            </select></label>
          )}
          {action.type === "revoke" && <p className="notice warning">确认撤销发送给 {action.invite.email} 的邀请。</p>}
          {error !== undefined && <Failure reason={error} />}
          <button type="submit" className={action.type === "revoke" ? "danger" : "primary"} disabled={pending}>
            {pending ? "正在提交…" : action.type === "invite" ? "生成邀请链接" : action.type === "revoke" ? "确认撤销" : "预览调整"}
          </button>
        </form>
      )}
    </Dialog>
  );
}
