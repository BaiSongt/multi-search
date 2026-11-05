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
| `image.source_page_verified` | 来源页是否已经核查 |

首轮平台只做链接发现与来源字段规范化。默认 `image_options.source_page_required=true`，缺少可用来源页的候选被过滤。设为 `false` 时，可以返回 `source_page_url=null` 并附缺失告警。此处来源页有效性指链接格式与来源字段符合契约，实际页面可访问性仍待核验。

首轮候选的 `rights_status` 为 `unknown`，作者、权利人和许可证证据没有可靠来源时为 `null`。过滤策略默认 `any_with_status`；`declared_only` 和 `verified_only` 排除未知状态，因此当前通常返回零条。检索引擎名称只用于溯源。使用者应从来源页取得与用途相符的权利依据。

`min_width`、`min_height`、`orientation` 基于上游尺寸元数据筛选。缺少所需尺寸的候选无法满足条件。图片类型取决于引擎支持，对无法执行的筛选返回告警。结果不因去重或预览而触发服务端下载原图。
