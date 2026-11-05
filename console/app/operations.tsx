"use client";

import { useEffect, useState } from "react";
import { callApi } from "./lib/api";
import type { ImageReport, ImageReportRow, Job, JobStatus, Page, RightsStatus } from "./lib/contracts";
import { CopyId, dateTime, Dialog, Empty, ExternalLink, Failure, formatNumber, Loading, Pagination, safeLink, Table, usePagination, useResource } from "./ui";
import { Metrics } from "./usage-view";

const STATUS: Record<JobStatus, string> = {
  queued: "排队中", running: "运行中", cancelling: "正在取消", completed: "完成",
  partial: "部分完成", failed: "失败", cancelled: "已取消",
};
const ACTIVE: JobStatus[] = ["queued", "running", "cancelling"];
const KIND = { fetch: "网页提取", research: "深度研究" };

function JobBadge({ status }: { status: JobStatus }) {
  const color = status === "completed" ? "positive" : status === "failed" ? "danger" : ACTIVE.includes(status) || status === "partial" ? "warning" : "";
  return <span className={`badge ${color}`}>{STATUS[status] ?? status}</span>;
}

function Progress({ job }: { job: Job }) {
  const { completed, total, stage } = job.progress;
  return (
    <div className="stack">
      <span>{stage} · {completed}/{total}</span>
      {total > 0 && <progress value={Math.min(completed, total)} max={total} aria-label="任务进度" />}
    </div>
  );
}

export function JobsView({ csrf, showTenant }: { csrf: string; showTenant: boolean }) {
  const [status, setStatus] = useState("");
  const [kind, setKind] = useState("");
  const [tenant, setTenant] = useState("");
  const [selected, setSelected] = useState<string>();
  const pager = usePagination();
  const query = new URLSearchParams({ limit: "20" });
  for (const [name, value] of Object.entries({ status, kind, tenant_id: tenant })) {
    if (value) query.set(name, value);
  }
  if (pager.cursor) query.set("cursor", pager.cursor);
  const resource = useResource<Page<Job>>(`admin/jobs?${query}`);
  const active = resource.data?.items.some((job) => ACTIVE.includes(job.status));
  useEffect(() => {
    if (!active || resource.pending) return;
    const timer = window.setTimeout(resource.reload, 5_000);
    return () => window.clearTimeout(timer);
  }, [active, resource.pending, resource.reload]);

  return (
    <>
      <form method="post" className="panel" onSubmit={(event) => {
        event.preventDefault();
        const fields = new FormData(event.currentTarget);
        setStatus(String(fields.get("status") ?? ""));
        setKind(String(fields.get("kind") ?? ""));
        setTenant(String(fields.get("tenant_id") ?? "").trim());
        pager.reset();
      }}>
        <div className="form-grid">
          <label>任务状态<select name="status"><option value="">全部</option>{Object.entries(STATUS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          <label>任务类型<select name="kind"><option value="">全部</option>{Object.entries(KIND).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          {showTenant && <label>租户 ID<input name="tenant_id" maxLength={36} /></label>}
        </div>
        <div className="actions">
          <button type="submit" className="primary">筛选</button>
          <button type="button" disabled={resource.pending} onClick={resource.reload}>刷新任务</button>
          {active && <span className="muted">存在未完成任务，每 5 秒更新。</span>}
        </div>
      </form>
      {!resource.data && resource.pending && <Loading />}
      {resource.error !== undefined && <Failure reason={resource.error} retry={resource.reload} />}
      {resource.data?.items.length === 0 && <Empty>没有符合条件的任务。</Empty>}
      {resource.data && resource.data.items.length > 0 && (
        <Table caption="任务列表" headers={["任务 ID", "类型", "状态", "进度", "尝试", ...(showTenant ? ["租户 ID"] : []), "创建时间", "最后更新", "详情"]}>
          {resource.data.items.map((job) => (
            <tr key={job.job_id}>
              <td><CopyId value={job.job_id} /></td><td>{KIND[job.kind] ?? job.kind}</td>
              <td><JobBadge status={job.status} />{job.error && <p className="muted">{job.error.code}</p>}</td>
              <td><Progress job={job} /></td><td>{job.attempts}/{job.max_attempts}</td>
              {showTenant && <td><CopyId value={job.tenant_id} /></td>}
              <td>{dateTime(job.created_at)}</td><td>{dateTime(job.updated_at)}</td>
              <td><button type="button" onClick={() => setSelected(job.job_id)}>详情</button></td>
            </tr>
          ))}
        </Table>
      )}
      {resource.data && <Pagination next={resource.data.next_cursor} pager={pager} />}
      {selected && <JobDetails id={selected} csrf={csrf} close={() => setSelected(undefined)} changed={resource.reload} />}
    </>
  );
}

function JobDetails({ id, csrf, close, changed }: { id: string; csrf: string; close: () => void; changed: () => void }) {
  const [confirm, setConfirm] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>();
  const path = `admin/jobs/${encodeURIComponent(id)}`;
  const resource = useResource<Job>(path);
  const job = resource.data;
  const active = job && ACTIVE.includes(job.status);
  useEffect(() => {
    if (!active || resource.pending) return;
    const timer = window.setTimeout(resource.reload, 3_000);
    return () => window.clearTimeout(timer);
  }, [active, resource.pending, resource.reload]);

  async function cancel() {
    setPending(true);
    setError(undefined);
    try {
      await callApi(`${path}/cancel`, { method: "POST", csrf });
      setConfirm(false);
      resource.reload();
      changed();
    } catch (reason) {
      setError(reason);
    } finally {
      setPending(false);
    }
  }

  return (
    <Dialog title="任务详情" close={close} pending={pending}>
      {!job && resource.pending && <Loading />}
      {resource.error !== undefined && <Failure reason={resource.error} retry={resource.reload} />}
      {job && (
        <div className="stack">
          <dl>
            <dt>任务 ID</dt><dd><CopyId value={job.job_id} /></dd>
            <dt>租户 ID</dt><dd><CopyId value={job.tenant_id} /></dd>
            <dt>任务状态</dt><dd><JobBadge status={job.status} /></dd>
            <dt>类型</dt><dd>{KIND[job.kind] ?? job.kind}</dd>
            <dt>进度</dt><dd><Progress job={job} /></dd>
            <dt>尝试次数</dt><dd>{job.attempts}/{job.max_attempts}</dd>
            <dt>截止时间</dt><dd>{dateTime(job.deadline_at)}</dd>
            <dt>最后更新</dt><dd>{dateTime(job.updated_at)}</dd>
          </dl>
          {job.error && <p className="notice danger">{job.error.code} · {job.error.message}</p>}
          {job.result != null && <details><summary>查看任务结果 JSON</summary><pre>{JSON.stringify(job.result, null, 2)}</pre></details>}
          {error !== undefined && <Failure reason={error} />}
          {(job.status === "queued" || job.status === "running") && (confirm ? (
            <div className="notice warning">
              <p>确认停止此任务。已产生的记录仍可查询。</p>
              <div className="actions">
                <button type="button" disabled={pending} onClick={() => setConfirm(false)}>返回</button>
                <button type="button" className="danger" disabled={pending} onClick={() => void cancel()}>{pending ? "正在取消…" : "确认取消任务"}</button>
              </div>
            </div>
          ) : <button type="button" className="danger" onClick={() => setConfirm(true)}>取消任务</button>)}
        </div>
      )}
    </Dialog>
  );
}

const RIGHTS: Record<RightsStatus, string> = {
  unknown: "授权未知", declared_unverified: "已声明 · 未核验", verified_license: "许可已核验", conflict: "许可信息冲突",
};

function ImageQualityBadge({ status }: { status: RightsStatus }) {
  return <span className={`badge ${status === "verified_license" ? "positive" : status === "conflict" ? "danger" : "warning"}`}>{RIGHTS[status] ?? RIGHTS.unknown}</span>;
}

function SourceStatus({ row }: { row: ImageReportRow }) {
  if (!row.source_page_url) return <span className="badge danger">来源缺失</span>;
  if (row.source_page_verified) return <span className="badge positive">已核查</span>;
  return <span className="badge warning">{row.source_page_status ?? "待核查"}</span>;
}

export function ImageQualityView({ showTenant }: { showTenant: boolean }) {
  const [status, setStatus] = useState("");
  const [selected, setSelected] = useState<ImageReportRow>();
  const [preview, setPreview] = useState(false);
  const pager = usePagination();
  const query = new URLSearchParams({ limit: "20" });
  if (status) query.set("rights_status", status);
  if (pager.cursor) query.set("cursor", pager.cursor);
  const resource = useResource<ImageReport>(`admin/images?${query}`);
  const data = resource.data;
  const previewUrl = safeLink(selected?.thumbnail_url, { thumbnail: true });
  const total = data ? Object.keys(RIGHTS).reduce((sum, code) => sum + (data.counts[code] ?? 0), 0) : 0;
  return (
    <>
      <div className="actions">
        <label>许可状态<select value={status} onChange={(event) => { setStatus(event.target.value); pager.reset(); }}>
          <option value="">全部</option>
          {Object.entries(RIGHTS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select></label>
        <button type="button" disabled={resource.pending} onClick={resource.reload}>刷新记录</button>
      </div>
      {resource.pending && <Loading />}
      {resource.error !== undefined && <Failure reason={resource.error} retry={resource.reload} />}
      {data && (
        <>
          <Metrics values={Object.entries(RIGHTS).map(([code, label]) => [label, data.counts[code] ?? 0, "项"])} />
          <p className="muted">许可核验比例 {total ? `${formatNumber((data.counts.verified_license ?? 0) * 100 / total)}%` : "—"}</p>
          {data.items.length === 0 ? <Empty>暂无符合条件的图片来源记录。</Empty> : (
            <Table caption="图片来源和核验记录" headers={["来源页", "状态", "许可", "请求 ID", ...(showTenant ? ["租户 ID"] : []), "检索时间", "详情"]}>
              {data.items.map((row) => (
                <tr key={row.id}>
                  <td><ExternalLink href={row.source_page_url}>{row.source_page_url ? "打开来源页" : "未提供"}</ExternalLink></td>
                  <td><SourceStatus row={row} /></td><td><ImageQualityBadge status={row.rights_status} /></td>
                  <td><CopyId value={row.request_id} /></td>{showTenant && <td><CopyId value={row.tenant_id} /></td>}
                  <td>{dateTime(row.retrieved_at)}</td><td><button type="button" onClick={() => { setPreview(false); setSelected(row); }}>查看详情</button></td>
                </tr>
              ))}
            </Table>
          )}
          <Pagination next={data.next_cursor} pager={pager} />
        </>
      )}
      {selected && (
        <Dialog title="图片质量记录" close={() => setSelected(undefined)}>
          <div className="stack">
            <dl>
              <dt>来源页</dt><dd><ExternalLink href={selected.source_page_url}>{selected.source_page_url ?? "未提供"}</ExternalLink></dd>
              <dt>来源状态</dt><dd><SourceStatus row={selected} /></dd>
              <dt>原图</dt><dd><ExternalLink href={selected.original_url}>打开原图</ExternalLink></dd>
              <dt>许可状态</dt><dd><ImageQualityBadge status={selected.rights_status} /></dd>
              <dt>上次核查</dt><dd>{dateTime(selected.last_check_at ?? null)}</dd>
              <dt>记录 ID</dt><dd><CopyId value={selected.id} /></dd>
            </dl>
            {previewUrl && (preview
              ? <div className="thumbnail"><img src={previewUrl} alt="图片缩略预览" referrerPolicy="no-referrer" onError={() => setPreview(false)} /></div>
              : <button type="button" onClick={() => setPreview(true)}>加载外部缩略图</button>)}
          </div>
        </Dialog>
      )}
    </>
  );
}
