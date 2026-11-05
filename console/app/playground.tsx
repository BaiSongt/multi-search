"use client";

import { useState, type FormEvent } from "react";
import { callApi } from "./lib/api";
import type { ImageResult, RightsStatus, SearchResponse } from "./lib/contracts";
import { Copy, CopyId, dateTime, Dialog, Empty, ExternalLink, Failure, Loading, safeLink } from "./ui";

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

function searchBody(fields: FormData, imageOnly: boolean) {
  const choice = imageOnly ? "image" : fields.get("modality") ?? "text";
  const modalities = choice === "mixed" ? ["text", "image"] : [choice];
  const width = fields.get("min_width");
  const height = fields.get("min_height");
  const orientation = fields.get("orientation");
  const imageType = fields.get("image_type");
  const language = fields.get("language");
  const region = fields.get("region");
  return {
    query: String(fields.get("query") ?? "").trim(),
    mode: fields.get("mode") ?? "auto",
    source_types: [choice === "text" ? fields.get("source") ?? "web" : "web"],
    modalities,
    max_results: Number(fields.get("max_results") || "10"),
    ...(language ? { language } : {}),
    ...(region ? { region } : {}),
    ...(choice === "text" ? {} : {
      image_options: {
        source_page_required: fields.get("source_page_required") !== null,
        license_policy: fields.get("license_policy") ?? "any_with_status",
        ...(width ? { min_width: Number(width) } : {}),
        ...(height ? { min_height: Number(height) } : {}),
        ...(orientation ? { orientation } : {}),
        ...(imageType ? { image_type: imageType } : {}),
      },
    }),
  };
}

export function Playground({ csrf, imageOnly = false }: { csrf: string; imageOnly?: boolean }) {
  const [modality, setModality] = useState<ModalityChoice>(imageOnly ? "image" : "text");
  const [request, setRequest] = useState<ReturnType<typeof searchBody>>();
  const [response, setResponse] = useState<SearchResponse>();
  const [selected, setSelected] = useState<ImageResult>();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>();

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
      <form method="post" onSubmit={search} className="panel" aria-busy={pending}>
        <div className="form-grid">
          <label className="wide">关键词<input name="query" required maxLength={1024} placeholder={imageOnly ? "例如：Golden Gate Bridge" : "例如：NASA"} /></label>
          {!imageOnly && (
            <label>模态
              <select name="modality" value={modality} onChange={(event) => setModality(event.target.value as ModalityChoice)}>
                <option value="text">文字</option><option value="image">图片</option><option value="mixed">混合</option>
              </select>
            </label>
          )}
          <label>来源
            <select name="source" disabled={modality !== "text"}>
              <option value="web">Web</option><option value="news">新闻</option>
            </select>
          </label>
          <label>搜索深度
            <select name="mode"><option value="auto">自动</option><option value="quick">快速</option><option value="standard">标准</option><option value="local">本地缓存</option></select>
          </label>
          <label>最多结果<input type="number" name="max_results" defaultValue={10} required min={1} max={30} /></label>
        </div>
        {modality !== "text" && (
          <fieldset>
            <legend>图片筛选</legend>
            <div className="form-grid">
              <label>最小宽度（px）<input type="number" name="min_width" min={1} max={20000} /></label>
              <label>最小高度（px）<input type="number" name="min_height" min={1} max={20000} /></label>
              <label>宽高比<select name="orientation"><option value="">全部</option><option value="landscape">横向</option><option value="portrait">纵向</option><option value="square">方形</option></select></label>
              <label>类型<select name="image_type"><option value="">全部</option><option value="photo">照片</option><option value="illustration">插画</option><option value="transparent">透明背景</option><option value="icon">图标</option></select></label>
              <label>许可策略<select name="license_policy"><option value="any_with_status">全部（含授权未知）</option><option value="declared_only">有声明</option><option value="verified_only">已核验</option></select></label>
            </div>
            <label className="check"><input name="source_page_required" type="checkbox" defaultChecked />要求提供来源页</label>
          </fieldset>
        )}
        <details>
          <summary>高级参数与 API Key</summary>
          <div className="form-grid">
            <label>语言<input name="language" placeholder="zh-CN" maxLength={22} /></label>
            <label>地区<input name="region" pattern="[A-Z]{2}" placeholder="HK" maxLength={2} /></label>
            <label>API Key<input name="api_key" type="password" autoComplete="off" maxLength={128} placeholder="留空使用当前会话" /></label>
          </div>
        </details>
        <div className="actions"><button type="submit" className="primary" disabled={pending}>{pending ? "检索中…" : "搜索"}</button></div>
      </form>
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
        </>
      )}
      {selected && <ImageDetails result={selected} close={() => setSelected(undefined)} />}
    </>
  );
}
