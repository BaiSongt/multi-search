"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { callApi } from "./lib/api";
import type { EvidenceRecord, JobAccepted, JobStatus, ResearchBudget, ResearchJobView, ResearchResult, SearchResult } from "./lib/contracts";
import { CopyId, dateTime, ExternalLink, Failure } from "./ui";

const STATUS: Record<JobStatus, string> = {
  queued: "排队中", running: "运行中", cancelling: "正在取消", completed: "完成",
  partial: "部分完成", failed: "失败", cancelled: "已取消",
};
const STAGES: Record<string, string> = {
  queued: "等待 Worker", starting: "准备任务", searching: "检索与证据提取",
  researching: "汇总检索轮次", synthesizing: "生成模型综合", completed: "研究结束",
};
const STEPS: Record<string, string> = {
  original: "原始问题", subquestion: "子问题", primary_sources: "一手来源", limitations: "局限与反例",
  methodology: "方法与评估", independent_sources: "其他来源",
};
const ROUND_STATUS = { completed: "完成", partial: "部分完成", failed: "失败", interrupted: "中断" };

function isActive(status: JobStatus) {
  return status === "queued" || status === "running" || status === "cancelling";
}

function Budget({ budget, usage }: { budget: ResearchBudget; usage?: ResearchResult["usage"] }) {
  return (
    <details open>
      <summary>研究预算与用量</summary>
      <dl>
        <dt>时限</dt><dd>{budget.max_latency_ms} ms</dd>
        <dt>检索轮次</dt><dd>{usage ? `${usage.search_rounds} / ` : "最多 "}{budget.max_search_rounds}</dd>
        <dt>抓取页数额度</dt><dd>{usage ? `${usage.pages_charged} / ` : "最多 "}{budget.max_pages}</dd>
        {usage && <><dt>抓取尝试 / 成功</dt><dd>{usage.pages_attempted ?? "未报告"} / {usage.pages_fetched}</dd></>}
        <dt>结果 / 图片结果上限</dt><dd>{budget.max_results} / {budget.max_image_results}</dd>
        <dt>图片来源核验额度</dt><dd>{usage ? `${usage.image_metadata_charged} / ` : "最多 "}{budget.max_image_metadata_fetches}</dd>
        {usage && <><dt>图片来源核验尝试</dt><dd>{usage.image_metadata_fetches ?? "未报告"}</dd></>}
        <dt>模型 Token 上限</dt><dd>{budget.max_llm_tokens}</dd>
        {usage && <><dt>模型 Token 实际 / 预留</dt><dd>{usage.llm_tokens ?? "未报告"} / {usage.llm_tokens_reserved}</dd></>}
      </dl>
    </details>
  );
}

function ResearchReport({ report, showEvidence }: { report: ResearchResult; showEvidence: (record: EvidenceRecord) => void }) {
  const evidence = new Map(report.evidence_records.map((record) => [record.evidence_id, record]));
  function references(refs: string[]) {
    return refs.length ? (
      <div className="actions">
        {refs.map((id) => {
          const record = evidence.get(id);
          return record
            ? <button key={id} type="button" onClick={() => showEvidence(record)}>证据 · {record.title ?? "来源摘录"}</button>
            : <span key={id} className="muted">证据暂不可查看 · {id}</span>;
        })}
      </div>
    ) : null;
  }
  return (
    <section className="panel stack" aria-label="研究报告">
      <h2>研究报告</h2>
      <p>{report.summary}</p>
      {report.warnings.length > 0 && <ul>{report.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>}
      <h3>来源摘录</h3>
      {report.findings.length === 0 && <p className="muted">暂无可定位的来源摘录。</p>}
      {report.findings.map((finding, index) => (
        <article className="stack" key={`${finding.evidence_refs[0]}-${index}`}>
          <div className="status-line">
            <ExternalLink href={finding.origin_url}>{finding.title ?? "来源页面"}</ExternalLink>
            <span className="badge">已提取</span>
          </div>
          <blockquote className="notice evidence-excerpt">{finding.excerpt}</blockquote>
          {references(finding.evidence_refs)}
        </article>
      ))}
      {report.model_findings.length > 0 && <h3>模型综合 · 未核验</h3>}
      {report.model_findings.map((finding, index) => (
        <article className="stack" key={index}>
          <p>{finding.claim}</p>
          {references(finding.evidence_refs)}
        </article>
      ))}
      {report.comparisons.length > 0 && <h3>跨来源比对 · 来源独立性待核验</h3>}
      {report.comparisons.map((comparison) => (
        <article className="stack" key={comparison.round_number}>
          <p>第 {comparison.round_number} 轮 · {comparison.source_hosts.join("、")}</p>
          {references(comparison.evidence_refs)}
        </article>
      ))}
      <details>
        <summary>检索轮次 · {report.rounds.length} / {report.planned_rounds}</summary>
        <ul>{report.rounds.map((round) => (
          <li key={round.number}>
            第 {round.number} 轮 · {STEPS[round.step] ?? round.step} · {ROUND_STATUS[round.status]} · {round.result_count} 条 · {round.elapsed_ms} ms
            {round.error_code && <span className="muted"> · {round.error_code}</span>}
            {references(round.evidence_refs)}
          </li>
        ))}</ul>
      </details>
      {report.evidence_records.length > 0 && <details>
        <summary>证据列表 · {report.evidence_records.length} 条</summary>
        {references(report.evidence_records.map((record) => record.evidence_id))}
      </details>}
    </section>
  );
}

export function ResearchJob({ accepted, csrf, showEvidence, renderResults }: {
  accepted: JobAccepted;
  csrf: string;
  showEvidence: (record: EvidenceRecord) => void;
  renderResults: (results: SearchResult[]) => ReactNode;
}) {
  const path = `console/jobs/${encodeURIComponent(accepted.job_id)}`;
  const [job, setJob] = useState<ResearchJobView>();
  const [pollError, setPollError] = useState<unknown>();
  const [cancelError, setCancelError] = useState<unknown>();
  const [cancelPending, setCancelPending] = useState(false);
  const [pollVersion, setPollVersion] = useState(0);
  const controller = useRef<AbortController>(null);
  const status = job?.status ?? accepted.status;
  const terminal = job !== undefined && !isActive(status);

  useEffect(() => {
    if (terminal || cancelPending) return;
    const request = new AbortController();
    controller.current = request;
    let timer: ReturnType<typeof setTimeout> | undefined;
    callApi<ResearchJobView>(path, { csrf, signal: request.signal }).then((next) => {
      if (request.signal.aborted) return;
      setJob(next);
      setPollError(undefined);
      if (isActive(next.status)) timer = setTimeout(() => setPollVersion((version) => version + 1), 1250);
    }).catch((reason) => {
      if (!request.signal.aborted) setPollError(reason);
    });
    return () => {
      request.abort();
      clearTimeout(timer);
    };
  }, [path, csrf, pollVersion, terminal, cancelPending]);

  async function cancel() {
    controller.current?.abort();
    setCancelError(undefined);
    setCancelPending(true);
    try {
      setJob(await callApi<ResearchJobView>(`${path}/cancel`, { method: "POST", csrf }));
      setPollError(undefined);
    } catch (reason) {
      setCancelError(reason);
    } finally {
      setCancelPending(false);
    }
  }

  const progress = job?.progress;
  const report = job?.result;
  const traceId = job?.trace_id ?? accepted.trace_id;
  return (
    <>
      <section className="panel stack" aria-label="深度研究任务">
        <h2>深度研究任务</h2>
        <div className="status-line">
          <span role="status" className={`badge ${status === "completed" ? "positive" : status === "failed" ? "danger" : "warning"}`}>{STATUS[status]}</span>
          <CopyId value={accepted.job_id} />
        </div>
        {(job?.request_id || traceId) && <dl>
          {job?.request_id && <><dt>请求 ID</dt><dd><CopyId value={job.request_id} /></dd></>}
          {traceId && <><dt>追踪 ID</dt><dd><CopyId value={traceId} /></dd></>}
        </dl>}
        {progress && <div className="stack">
          <span>{STAGES[progress.stage] ?? STATUS[status]} · {progress.completed}/{progress.total} 轮</span>
          {progress.total > 0 && <progress value={Math.min(progress.completed, progress.total)} max={progress.total} aria-label="研究进度" />}
          <span className="muted">已抓取 {progress.pages_fetched ?? 0} 页 · 截止 {dateTime(job?.deadline_at ?? null)}</span>
        </div>}
        <Budget budget={report?.budget ?? accepted.budget} usage={report?.usage} />
        {(status === "queued" || status === "running") && <div className="actions">
          <button type="button" onClick={cancel} disabled={cancelPending}>{cancelPending ? "正在发送…" : "取消研究"}</button>
        </div>}
        {pollError !== undefined && !terminal && <Failure reason={pollError} retry={() => setPollVersion((version) => version + 1)} />}
        {cancelError !== undefined && !terminal && <Failure reason={cancelError} />}
        {job?.error && <p className="notice warning" role="status">{job.error.message} · {job.error.code}</p>}
      </section>
      {report && terminal && <><ResearchReport report={report} showEvidence={showEvidence} />{renderResults(report.results)}</>}
    </>
  );
}
