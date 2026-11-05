"use client";

import { useState, type FormEvent } from "react";
import { callApi } from "./lib/api";
import type { Capabilities, EvidenceRecord, ImageResult, RightsStatus, SearchResponse } from "./lib/contracts";
import { Copy, CopyId, dateTime, Dialog, Empty, ExternalLink, Failure, Loading, safeLink, useResource } from "./ui";

const RIGHTS: Record<RightsStatus, { label: string; color: string }> = {
  unknown: { label: "授权未知", color: "warning" },
  declared_unverified: { label: "已声明 · 未核验", color: "warning" },
  verified_license: { label: "许可已核验", color: "positive" },
  conflict: { label: "许可信息冲突", color: "danger" },
};

function RightsBadge({ status }: { status: RightsStatus }) {
  const rights = RIGHTS[status] ?? RIGHTS.unknown;
  return <span className={`badge ${rights.color}`} data-rights-status={status}>{rights.label}</span>;
}

function Thumbnail({ result }: { result: ImageResult }) {
  const [failed, setFailed] = useState(false);
  const url = safeLink(result.image.thumbnail_url, { thumbnail: true });
  return (
    <div className="thumbnail">
      {url && !failed
        // 浏览器直接加载不可执行的图片资源，避免服务端读取候选地址。
        // eslint-disable-next-line @next/next/no-img-element
        ? <img src={url} alt="" loading="lazy" referrerPolicy="no-referrer" onError={() => setFailed(true)} />
        : <span>暂无可用预览</span>}
    </div>
  );
}

function ImageDetails({ result, close }: { result: ImageResult; close: () => void }) {
  const image = result.image;
  const citation = [
    result.title,
    `来源页 ${safeLink(image.source_page_url) ?? "未提供"}`,
    `作者 ${image.creator ?? "未提供"}`,
    `权利人 ${image.copyright_holder ?? "未提供"}`,
    `许可状态 ${(RIGHTS[image.rights_status] ?? RIGHTS.unknown).label}`,
    `许可依据 ${safeLink(image.rights_evidence_url) ?? "未核验"}`,
    `检索时间 ${result.retrieved_at}`,
  ].join("\n");
  return (
    <Dialog title="图片归属与许可" close={close}>
      <div className="stack">
        <h3>{result.title}</h3>
        <dl>
          <dt>来源页</dt><dd><ExternalLink href={image.source_page_url}>{image.source_page_url ?? "未提供"}</ExternalLink></dd>
          <dt>来源页核查</dt><dd>{image.source_page_verified ? "已核查" : "待核查"}</dd>
          <dt>原图</dt><dd><ExternalLink href={image.original_url}>打开原图</ExternalLink></dd>
          <dt>来源站点</dt><dd>{image.source_site_name ?? image.source_site_domain ?? "未提供"}</dd>
          <dt>原图尺寸</dt><dd>{image.width && image.height ? `${image.width} × ${image.height} px` : "未提供"}</dd>
          <dt>搜索引擎</dt><dd>{image.search_engines.join("、") || "未提供"}</dd>
          <dt>Provider</dt><dd>{result.provider_ids.join("、")}</dd>
          <dt>作者</dt><dd>{image.creator ?? "未提供"}</dd>
          <dt>权利人</dt><dd>{image.copyright_holder ?? "未提供"}</dd>
          <dt>许可证</dt><dd>{image.license_url ? <ExternalLink href={image.license_url}>{image.license_name ?? "许可证链接"}</ExternalLink> : image.license_name ?? "未提供"}</dd>
          <dt>署名信息</dt><dd>{image.attribution_text ?? "未提供"}</dd>
          <dt>许可状态</dt><dd><RightsBadge status={image.rights_status} /></dd>
          <dt>许可依据</dt><dd><ExternalLink href={image.rights_evidence_url}>{image.rights_evidence_url ? "打开许可依据" : "未核验"}</ExternalLink></dd>
          <dt>核验时间</dt><dd>{dateTime(image.rights_checked_at)}</dd>
          <dt>检索时间</dt><dd>{dateTime(result.retrieved_at)}</dd>
        </dl>
        {image.rights_status === "unknown" && <p className="notice warning" role="status">授权未知。使用前请从来源页确认适用许可。</p>}
        <Copy value={citation} label="复制引用信息" />
      </div>
    </Dialog>
  );
}

type ModalityChoice = "text" | "image" | "mixed";
const MODES = { auto: "自动", quick: "快速", standard: "标准", local: "本地缓存", evidence: "证据检索" };
const SOURCES: Record<string, string> = { web: "Web", news: "新闻", academic: "学术", code: "代码", local: "本地", patent: "专利" };
const SCOPES: Record<string, string> = { public: "公共网络", private: "私有网络", offline: "离线" };
const DEFAULT_PAIRS = [
  { source_type: "web", modality: "text" },
  { source_type: "web", modality: "image" },
  { source_type: "news", modality: "text" },
];

function searchBody(fields: FormData, imageOnly: boolean) {
  const choice = imageOnly ? "image" : fields.get("modality") ?? "text";
  const modalities = choice === "mixed" ? ["text", "image"] : [choice];
  const width = fields.get("min_width");
  const height = fields.get("min_height");
  const orientation = fields.get("orientation");
  const imageType = fields.get("image_type");
  const language = fields.get("language");
  const region = fields.get("region");
  const latency = fields.get("max_latency_ms");
  const pages = fields.get("max_pages");
  const metadataFetches = fields.get("max_image_metadata_fetches");
  return {
    query: String(fields.get("query") ?? "").trim(),
    mode: fields.get("mode") ?? "auto",
    source_types: [fields.get("source") ?? "web"],
    modalities,
    max_results: Number(fields.get("max_results") || "10"),
    ...(language ? { language } : {}),
    ...(region ? { region } : {}),
    ...(latency ? { max_latency_ms: Number(latency) } : {}),
    ...(pages ? { max_pages: Number(pages) } : {}),
    ...(metadataFetches ? { max_image_metadata_fetches: Number(metadataFetches) } : {}),
    ...(fields.has("require_evidence") ? { require_evidence: true } : {}),
    ...(fields.has("network_scope") ? { network_scope: fields.get("network_scope") } : {}),
    ...(choice === "text" ? {} : {
      image_options: {
        source_page_required: !fields.has("image_options_present") || fields.has("source_page_required"),
        license_policy: fields.get("license_policy") ?? "any_with_status",
        ...(width ? { min_width: Number(width) } : {}),
        ...(height ? { min_height: Number(height) } : {}),
        ...(orientation ? { orientation } : {}),
        ...(imageType ? { image_type: imageType } : {}),
      },
    }),
  };
}

function EvidenceDetails({ record, close }: { record: EvidenceRecord; close: () => void }) {
  const locator = record.locator;
  return (
    <Dialog title="可定位证据" close={close}>
      <div className="stack">
        <h3>{record.title ?? "已提取的来源摘录"}</h3>
        <blockquote className="notice evidence-excerpt">{record.evidence_text}</blockquote>
        <dl>
          <dt>来源页面</dt><dd><ExternalLink href={record.origin_url}>{record.origin_url}</ExternalLink></dd>
          <dt>证据 ID</dt><dd><CopyId value={record.evidence_id} /></dd>
          <dt>抓取时间</dt><dd>{dateTime(record.retrieved_at)}</dd>
          <dt>位置类型</dt><dd>{locator.kind}</dd>
          <dt>章节</dt><dd>{locator.section ?? "未标注"}</dd>
          <dt>字符偏移</dt><dd>{locator.text_offset_start}–{locator.text_offset_end}</dd>
          {locator.page_number !== null && <><dt>页码</dt><dd>{locator.page_number}</dd></>}
          {record.document_identifier && <><dt>文档标识</dt><dd>{record.document_identifier.type} · {record.document_identifier.value}</dd></>}
          <dt>提取方式</dt><dd>{record.extraction_method} / {record.extraction_version}</dd>
          <dt>内容 SHA-256</dt><dd><CopyId value={record.content_sha256} /></dd>
          <dt>证据状态</dt><dd>{record.verification_status}</dd>
        </dl>
        <p className="muted">字符偏移定位到本次抓取所提取的文本。</p>
      </div>
    </Dialog>
  );
}

export function Playground({ csrf, imageOnly = false, sessionSearch = true }: { csrf: string; imageOnly?: boolean; sessionSearch?: boolean }) {
  const [modality, setModality] = useState<ModalityChoice>(imageOnly ? "image" : "text");
  const [mode, setMode] = useState("auto");
  const [draft, setDraft] = useState<ReturnType<typeof searchBody>>();
  const [request, setRequest] = useState<ReturnType<typeof searchBody>>();
  const [response, setResponse] = useState<SearchResponse>();
  const [selected, setSelected] = useState<ImageResult>();
  const [evidence, setEvidence] = useState<EvidenceRecord>();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>();
  const capabilities = useResource<Capabilities>("capabilities");
  const limits = capabilities.data?.limits;
  const evidenceAvailable = capabilities.data?.modes.evidence === "available";
  const imageAvailable = capabilities.data ? capabilities.data.modalities.image === "available" : true;
  const allowedModes = Object.entries(MODES).filter(([name]) => capabilities.data
    ? capabilities.data.modes[name] === "available" : name !== "evidence");
  const pairs = capabilities.data?.combinations ?? DEFAULT_PAIRS;
  const scopes = Object.entries(SCOPES).filter(([scope]) => capabilities.data
    ? capabilities.data.network_scopes[scope] === "available" : scope === "public");
  const sources = [...new Set(pairs.filter((pair) => modality === "mixed"
    ? pairs.some((item) => item.source_type === pair.source_type && item.modality !== pair.modality && ["text", "image"].includes(item.modality))
    : pair.modality === modality).map((pair) => pair.source_type))];
  const modeLimit = limits?.modes[mode === "auto" ? "standard" : mode];

  async function search(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const fields = new FormData(event.currentTarget);
    const key = String(fields.get("api_key") ?? "").trim();
    const body = searchBody(fields, imageOnly);
    setError(undefined);
    setResponse(undefined);
    setRequest(body);
    setPending(true);
    try {
      const result = await callApi<SearchResponse>(key ? "search" : "playground", {
        method: "POST", body, ...(key ? { key } : { csrf }),
      });
      setResponse(result);
    } catch (reason) {
      setError(reason);
    } finally {
      setPending(false);
    }
  }

  return (
    <>
      {capabilities.error !== undefined && <Failure reason={capabilities.error} retry={capabilities.reload} />}
      {imageOnly && !imageAvailable && <p className="notice warning" role="status">当前部署没有可用的图片搜索来源。</p>}
      <form method="post" onSubmit={search} onChange={(event) => setDraft(searchBody(new FormData(event.currentTarget), imageOnly))} className="panel" aria-busy={pending}>
        <div className="form-grid">
          <label className="wide">关键词<input name="query" required maxLength={1024} placeholder={imageOnly ? "例如：Golden Gate Bridge" : "例如：NASA"} /></label>
          {!imageOnly && (
            <label>模态
              <select name="modality" value={modality} onChange={(event) => setModality(event.target.value as ModalityChoice)}>
                <option value="text">文字</option><option value="image" disabled={!imageAvailable}>图片</option><option value="mixed" disabled={!imageAvailable}>混合</option>
              </select>
            </label>
          )}
          <label>来源
            <select name="source" key={modality} disabled={!sources.length}>
              {sources.length
                ? sources.map((source) => <option key={source} value={source}>{SOURCES[source] ?? source}</option>)
                : <option value="">暂无可用来源</option>}
            </select>
          </label>
          <label>搜索深度
            <select name="mode" value={mode} onChange={(event) => setMode(event.target.value)}>
              {allowedModes.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>
          <label>最多结果<input type="number" name="max_results" defaultValue={10} required min={1} max={30} /></label>
        </div>
        {modality !== "text" && (
          <fieldset>
            <legend>图片筛选</legend>
            <input type="hidden" name="image_options_present" value="1" />
            <div className="form-grid">
              <label>最小宽度（px）<input type="number" name="min_width" min={1} max={20000} /></label>
              <label>最小高度（px）<input type="number" name="min_height" min={1} max={20000} /></label>
              <label>宽高比<select name="orientation"><option value="">全部</option><option value="landscape">横向</option><option value="portrait">纵向</option><option value="square">方形</option></select></label>
              <label>类型<select name="image_type"><option value="">全部</option><option value="photo">照片</option><option value="illustration">插画</option><option value="transparent">透明背景</option><option value="icon">图标</option></select></label>
              <label>许可策略<select name="license_policy"><option value="any_with_status">全部（含授权未知）</option><option value="declared_only">有声明</option><option value="verified_only">已核验</option></select></label>
              {limits && limits.max_image_metadata_fetches > 0 && <label>最多来源核验<input name="max_image_metadata_fetches" type="number" min={0} max={limits.max_image_metadata_fetches} /></label>}
            </div>
            <label className="check"><input name="source_page_required" type="checkbox" defaultChecked />要求提供来源页</label>
          </fieldset>
        )}
        <details open={sessionSearch ? undefined : true}>
          <summary>高级参数与 API Key</summary>
          <div className="form-grid">
            <label>语言<input name="language" placeholder="zh-CN" maxLength={22} /></label>
            <label>地区<input name="region" pattern="[A-Z]{2}" placeholder="HK" maxLength={2} /></label>
            <label>网络范围<select name="network_scope">{scopes.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
            <label>请求时限（ms）<input name="max_latency_ms" type="number" min={100} max={modeLimit?.max_latency_ms ?? 900000} placeholder={String(modeLimit?.max_latency_ms ?? "")} /></label>
            {evidenceAvailable && (
              <>
                <label>最多抓取页数<input name="max_pages" type="number" min={0} max={30} /></label>
                <label className="check"><input type="checkbox" name="require_evidence" />要求提供证据</label>
              </>
            )}
            <label>API Key<input name="api_key" type="password" autoComplete="off" maxLength={128} required={!sessionSearch}
              placeholder={sessionSearch ? "留空使用当前会话" : "输入获授权的 API Key"} /></label>
          </div>
        </details>
        <div className="actions"><button type="submit" className="primary" disabled={pending || !sources.length}>{pending ? "检索中…" : "搜索"}</button></div>
      </form>
      {draft && <details className="panel"><summary>查看请求预览</summary><pre>{JSON.stringify(draft, null, 2)}</pre></details>}
      {request && <details className="panel"><summary>查看请求 JSON</summary><pre>{JSON.stringify(request, null, 2)}</pre></details>}
      {pending && <Loading />}
      {error !== undefined && <Failure reason={error} />}
      {response && (
        <>
          <section className="panel" aria-label="搜索摘要">
            <h2>{response.result_count} 条结果</h2>
            <div className="status-line">
              <span className="badge">{response.mode_executed} · {response.elapsed_ms} ms</span>
              {response.cache_hit && <span className="badge">缓存命中</span>}
              {response.partial && <span className="badge warning">部分结果</span>}
              <CopyId value={response.request_id} />
            </div>
            {response.warnings.length > 0 && <ul>{response.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>}
          </section>
          {response.results.length === 0 ? <Empty>没有符合条件的结果。</Empty> : (
            <section aria-label="搜索结果" className="result-grid">
              {response.results.map((result) => (
                <article className="result-card" key={`${result.kind}-${result.result_id}`}>
                  {result.kind === "image" && <Thumbnail key={result.image.thumbnail_url ?? result.result_id} result={result} />}
                  <h3>{result.title}</h3>
                  {result.kind === "image" ? (
                    <>
                      <div className="actions">
                        <RightsBadge status={result.image.rights_status} />
                        <span className="muted">{result.image.source_site_name ?? result.image.source_site_domain ?? "来源未提供"}</span>
                      </div>
                      {result.image.width && result.image.height && <span className="muted">{result.image.width} × {result.image.height} px</span>}
                      <div className="actions">
                        <ExternalLink href={result.image.source_page_url}>{result.image.source_page_url ? "来源页" : "来源页未提供"}</ExternalLink>
                        <ExternalLink href={result.image.original_url}>原图</ExternalLink>
                        <button type="button" onClick={() => setSelected(result)}>图片详情</button>
                      </div>
                    </>
                  ) : (
                    <>
                      <p className="muted">{result.snippet ?? "暂无摘要"}</p>
                      <ExternalLink href={result.url}>查看来源</ExternalLink>
                    </>
                  )}
                  <span className="muted">{result.provider_ids.join("、")}</span>
                </article>
              ))}
            </section>
          )}
          <details className="panel"><summary>查看原始响应 JSON</summary><pre>{JSON.stringify(response, null, 2)}</pre></details>
          {response.evidence_records && response.evidence_records.length > 0 && (
            <section className="panel" aria-label="证据列表">
              <h2>可定位证据 · {response.evidence_records.length} 条</h2>
              {response.evidence_records.map((record) => (
                <div className="actions" key={record.evidence_id}>
                  <ExternalLink href={record.origin_url}>{record.title ?? "来源页"}</ExternalLink>
                  <button type="button" onClick={() => setEvidence(record)}>证据详情</button>
                </div>
              ))}
            </section>
          )}
        </>
      )}
      {selected && <ImageDetails result={selected} close={() => setSelected(undefined)} />}
      {evidence && <EvidenceDetails record={evidence} close={() => setEvidence(undefined)} />}
    </>
  );
}
