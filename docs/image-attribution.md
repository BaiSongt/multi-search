# 图片归属契约

请求以 `source_types=["web"]` 和 `modalities=["image"]` 发现图片候选。结果为 `kind=image`，公共字段包含 `retrieved_at` 和 `provider_ids`。图片字段记录发现位置、媒体链接与版权元数据。

| 字段 | 含义 |
| --- | --- |
| `image.original_url` | 上游返回的原图直链 |
| `image.thumbnail_url` | 可选预览链接 |
| `image.source_page_url` | 作品出现的来源页 |
| `image.source_site_domain` | 来源页面所在域 |
| `image.search_engines` | 返回候选的检索引擎 |
| `image.width`、`image.height` | 上游提供的尺寸 |
| `image.creator`、`image.copyright_holder` | 有可靠依据时记录的创作者与权利人 |
| `image.license_name`、`image.license_url` | 许可证名称与声明链接 |
| `image.rights_status` | `unknown`、`declared_unverified`、`verified_license` 或 `conflict` |
| `image.rights_checked_at`、`image.rights_evidence_url` | 许可核验时间与证据链接 |
| `image.source_page_verified` | 来源页是否已核对并匹配原图 |
| `image.source_page_status`、`image.source_checked_at` | 来源页访问状态与最近核查时间 |
| `image.asset_hash`、`image.asset_hash_algorithm`、`image.asset_hash_source` | 上游报告的原图哈希、算法与提供者 |

默认 `image_options.source_page_required=true`，缺少可用来源页的候选被过滤。设为 `false` 时，可以返回 `source_page_url=null` 并附缺失告警。链接格式合规与实际核验分开记录。

过滤策略默认 `any_with_status`。设为 `declared_only` 或 `verified_only` 时，默认最多核查 3 个候选，可用 `max_image_metadata_fetches` 收紧或调整至 5。也可在默认策略下显式开启核查。响应预算记录实际发起的核查次数。

当前核验器只访问固定的 Wikimedia Commons HTTPS API。文件页与 API 返回的原图一致时设置 `source_page_verified`。已识别的许可链接、名称和模板一致且有页面版本证据时标记 `verified_license`。证据不足、互相矛盾或来源不支持时分别保留声明、冲突或未知状态。没有可靠依据的作者或权利人字段保持 `null`。`declared_only` 接受已声明或已核验记录，`verified_only` 仅接受已核验记录。检索引擎名称只用于溯源。

`min_width`、`min_height`、`orientation` 基于上游尺寸元数据筛选。缺少所需尺寸的候选无法满足条件。图片类型取决于引擎支持，对无法执行的筛选返回告警。结果不因去重或预览而触发服务端下载原图。

L3 还可以通过安全抓取检查普通 HTML 来源页。原图必须与页面明确的图片引用一致才设置 `source_page_verified`。404 或 410 记为失效，网络限制与超时保留未核查状态。普通来源页的链接关联不会提升版权状态。Commons 提供的 SHA-1 以 `asset_hash_source=wikimedia_commons` 标明，平台无需重新下载原图。

管理台图片来源质量页将缺失、失效、核验失败、未匹配原图、待核查和已核查分开显示。核验受网络或策略限制时计入核验失败。汇总按可见租户和最近 30 个 UTC 自然日内已返回的图片记录计算。旧记录按已保存的来源状态归类。
