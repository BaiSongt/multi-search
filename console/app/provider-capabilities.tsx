import type { ReactNode } from "react";
import type { Provider, ProviderFilters } from "./lib/contracts";

const localeLabels: Record<ProviderFilters["language"], string> = {
  unsupported: "不支持",
  upstream_best_effort: "交给上游，效果取决于引擎",
  upstream_requires_language: "需同时指定语言，交给上游",
  upstream_requires_region: "非默认语言需附带地区，交给上游",
};

const dateLabels: Record<ProviderFilters["date_range"], string> = {
  metadata_postfilter: "按候选元数据过滤",
  upstream_and_metadata_postfilter: "上游过滤后核对元数据",
};

const dateFields: Record<ProviderFilters["date_field"], string> = {
  published_at: "发布日期",
  submitted_at: "提交日期",
  created_at: "创建日期",
};

const imageFields: Record<string, string> = {
  min_width: "最小宽度", min_height: "最小高度", orientation: "方向",
  source_page_required: "需要来源页", license_policy: "许可策略", image_type: "图片类型",
};

function Definition({ label, children }: { label: string; children: ReactNode }) {
  return <div className="definition"><dt>{label}</dt><dd>{children}</dd></div>;
}

function ImageFilters({ options }: { options: NonNullable<ProviderFilters["image_options"]> }) {
  const groups = [
    ["上游图片过滤", options.upstream],
    ["候选图片过滤", options.candidate_postfilter],
    ["平台结果过滤", options.platform_postfilter],
    ["不支持的图片过滤", options.unsupported],
  ] as const;
  return groups.map(([label, fields]) => fields.length > 0 && (
    <Definition key={label} label={label}>{fields.map((field) => imageFields[field] ?? field).join("、")}</Definition>
  ));
}

export function ProviderCapabilities({ provider }: { provider: Provider }) {
  const capabilities = provider.capabilities;
  if (!capabilities) return null;

  return (
    <details>
      <summary>能力详情</summary>
      <dl>
        <Definition label="网络范围">{provider.network_scope === "public" ? "公网" : provider.network_scope}</Definition>
        <Definition label="适配器分页">{capabilities.supports_pagination === null ? "未声明" : capabilities.supports_pagination ? "支持" : "不支持"}</Definition>
        <Definition label="上游密钥">{capabilities.requires_api_key === null ? "未声明" : capabilities.requires_api_key ? "必需" : "非必需"}</Definition>
        <Definition label="每进程并行槽位">{capabilities.max_concurrent_requests}</Definition>
        <Definition label="每进程上游并发上限">{capabilities.max_upstream_concurrent_requests ?? "未声明"}</Definition>
      </dl>
      {provider.pair_health.map(({ source_type, modality, filters }) => filters && (
        <dl key={`${source_type}/${modality}`}>
          <Definition label="来源与模态">{source_type} / {modality}</Definition>
          <Definition label="语言">{localeLabels[filters.language]}</Definition>
          <Definition label="地区">{localeLabels[filters.region]}</Definition>
          <Definition label="日期">{dateFields[filters.date_field]} · {dateLabels[filters.date_range]}</Definition>
          {filters.exact_identifier_date_range && <Definition label="精确标识符日期">{dateLabels[filters.exact_identifier_date_range]}</Definition>}
          {filters.image_options && <ImageFilters options={filters.image_options} />}
        </dl>
      ))}
    </details>
  );
}
