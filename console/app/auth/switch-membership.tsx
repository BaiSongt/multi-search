"use client";

import { useState } from "react";
import { callApi } from "../lib/api";
import { roleLabels, workspaceHome, type Role, type Session } from "../lib/contracts";
import { Dialog, Empty, Failure, Loading, Pagination, usePagination, useResource } from "../ui";

type Membership = { tenant_id: string; tenant: string; role: Role };
type MembershipPage = { memberships: Membership[]; next_cursor: string | null };

export function SwitchMembership({ csrf }: { csrf: string }) {
  const pager = usePagination();
  const query = pager.cursor ? `?cursor=${encodeURIComponent(pager.cursor)}` : "";
  const resource = useResource<MembershipPage>(`auth/memberships${query}`);
  const [selected, setSelected] = useState<Membership>();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>();

  async function enter(membership: Membership) {
    setPending(true);
    setError(undefined);
    try {
      const session = await callApi<Session>("auth/switch", {
        method: "POST",
        body: { tenant_id: membership.tenant_id },
        csrf,
      });
      window.location.assign(workspaceHome(session.role));
    } catch (reason) {
      setError(reason);
      setPending(false);
    }
  }

  return (
    <div className="stack">
      {resource.pending && <Loading />}
      {resource.error !== undefined && <Failure reason={resource.error} retry={resource.reload} />}
      {resource.data && <>
        {resource.data.memberships.length === 0
          ? <Empty>此页没有可切换的租户。</Empty>
          : <ul className="stack">
              {resource.data.memberships.map((membership) => (
                <li key={membership.tenant_id} className="status-line">
                  <span>{membership.tenant}</span>
                  <span className="badge">{roleLabels[membership.role]}</span>
                  <button type="button" disabled={resource.pending} onClick={() => {
                    setError(undefined);
                    setSelected(membership);
                  }}>切换到 {membership.tenant}</button>
                </li>
              ))}
            </ul>}
        <Pagination next={resource.data.next_cursor} pager={pager} />
      </>}
      {selected && (
        <Dialog title="确认切换租户" pending={pending} close={() => setSelected(undefined)}>
          <div className="stack">
            <p>以{roleLabels[selected.role]}身份进入「{selected.tenant}」。此浏览器中的原会话将结束。</p>
            {error !== undefined && <Failure reason={error} />}
            <button type="button" className="primary" disabled={pending} onClick={() => void enter(selected)}>
              {pending ? "正在切换…" : "确认切换"}
            </button>
          </div>
        </Dialog>
      )}
    </div>
  );
}
