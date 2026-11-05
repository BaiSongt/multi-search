# REST API 契约

在本地服务访问 `http://127.0.0.1:8765/docs` 或 `/openapi.json` 获取当前请求与响应模型。接口前缀为 `/api/v1`，使用 UTF-8 JSON。时间戳为 UTC，耗时单位为毫秒。

| 方法与路径 | 权限 | 行为 |
| --- | --- | --- |
| `GET /health/live` | 公开 | 进程存活 |
| `GET /health/ready` | 公开 | 数据库和 Redis 就绪 |
| `GET /api/v1/capabilities` | 公开 | 当前启用的模式、来源、模态与预算 |
| `GET /api/v1/providers` | API Key | Provider 能力与运行状态 |
| `POST /api/v1/search` | API Key | 统一文字与图片检索 |
| `POST /api/v1/playground` | Console 会话与 CSRF | 使用同一搜索契约并按当前租户计量 |
| `POST /api/v1/fetch` | API Key 的 `fetch` | 批量网页提取，返回 202 和任务标识 |
| `POST /api/v1/research` | API Key 的 `research` 与所需搜索作用域 | 创建 L4 研究任务，返回 202 |
| `GET /api/v1/jobs/{id}` | 对应任务作用域 | 查看当前租户的任务状态与结果 |
| `POST /api/v1/jobs/{id}/cancel` | 对应任务作用域 | 取消当前租户的非终态任务 |
| `GET /api/v1/evidence/{id}` | API Key 的 `fetch` 或 `research` | 读取当前租户的证据 |

搜索凭据使用 `Authorization: Bearer <API_KEY>`。文字需要 `search:text`，图片需要 `search:image`，混合请求需要两者。按 [部署文档](deployment.md#签发测试凭据) 签发凭据后，在当前终端将明文存入 `MULTISEARCH_KEY`。以下 POSIX shell 示例将同一个 Web 查询同时用于文字和图片。

```sh
curl --silent --show-error --fail-with-body http://127.0.0.1:8765/api/v1/search \
  -H "Authorization: Bearer ${MULTISEARCH_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{"query":"Golden Gate Bridge","mode":"standard","source_types":["web"],"modalities":["text","image"],"max_results":10}'
```

### 路由与预算

`query` 去除首尾空白后长度为 1–1024 字符。深度 `mode`、`source_types` 和 `modalities` 独立选择。Web 支持文字与图片，新闻支持文字。以能力端点为准选择当前实例启用的组合。

| 模式 | 路由 | 时限上限 | 结果上限 |
| --- | --- | --- | --- |
| `auto` | 请求时限不超过 4000 ms 时选 `quick`，其余选 `standard` | 继承所选模式 | 继承所选模式 |
| `local` | 读取当前租户的兼容缓存 | 2000 ms | 30 |
| `quick` | 首选 DDGS，按需回退 SearXNG | 4000 ms | 10 |
| `standard` | 可用 Provider 并行检索与融合 | 12000 ms | 30 |
| `evidence` | 并行发现候选并提取有定位的正文或核查图片来源 | 45000 ms | 30，最多 8 页正文 |
| `deep` | 异步多轮检索、证据汇总与可选模型综合 | 900000 ms，含排队 | 每轮最多 30 条，整项任务最多 5 轮与 30 页 |

请求可通过 `max_latency_ms`、`max_results`、`max_image_results` 和 `max_pages` 收紧预算。请求同时包含可用和不可用的来源模态组合时，返回部分结果及告警。`mode=auto` 配合 `require_evidence=true` 使用 L3。机器身份执行证据检索需要 `fetch` 和相应搜索作用域。显式指定快速、标准或本地模式并要求证据时返回 `EVIDENCE_UNAVAILABLE`。部署未启用深度研究时返回 `MODE_UNAVAILABLE`。

私网与离线检索只接受已启用的 `source_types=["local"]`，请求直接进入当前租户的本地索引。未启用或混入公网来源时返回 `SOURCE_UNAVAILABLE`，请求内容不会送往公网。

`language` 和 `region` 按上游能力尽力处理。`time_range` 使用 `{"from":"YYYY-MM-DD","to":"YYYY-MM-DD"}`，按 UTC 日期比较，含起止两天。仅保留来源时间字段已知且位于范围内的候选，新闻使用发布日期。深度研究的各轮检索沿用提交时的日期范围。Playground 的新闻卡片分别显示发布日期和检索时间。`domain_allowlist` 和 `domain_denylist` 用于过滤结果来源域名。请求的域名字段不会授予访问内网的权限。

启用 Wikipedia 后，适用的公网网页文字请求在常规来源无候选时可回退到英文或中文项目。结果的 `provider_ids` 包含 `wikipedia`，返回标题和条目链接，摘要为空。带显式地区或日期约束的请求仍按支持对应条件的常规来源执行。 [候补部署](deployment.md#可选-wikipedia-候补)

`cache_policy=use` 默认读取可用缓存。`refresh` 重新搜索并更新可缓存响应，`bypass` 直接检索且跳过缓存写入。`local` 只支持 `use`，未命中时以部分结果返回 `LOCAL_CACHE_MISS`。缓存按租户隔离，按查询、有效路由参数和 Provider 组合区分。命中时 `cache_hit=true`，返回当前调用的请求标识。完整结果缓存期限为文字一小时、图片半小时、新闻十分钟，新闻优先于图片和普通文字。正常空结果缓存 30 秒，上游部分失败的响应跳过缓存。

默认每租户 60 次每分钟，单 API 进程全局并发 20、每租户并发上限 5。租户与 Key 可配置分钟、日、月、模态、模式和并发上限。各周期使用 UTC。文字与图片额度分别按请求包含相应模态的次数计算，混合请求同时占用两类模态额度。入账在 PostgreSQL 同租户事务内完成，缓存命中和已接受但上游失败的请求各计一次。上游重试与回退只增加 Provider 调用记录。

可用 `idempotency_key` 在同一租户与调用身份内去重，保存 24 小时。相同请求重放原响应且不再扣额。不同请求复用同一标识或首次请求尚在执行时返回 409。公开使用量明细保留请求 ID、计量与错误码，默认不记录查询原文或凭据。

### 结果与错误

`results` 按 `kind` 区分 `text` 与 `image`，图片详见 [图片归属契约](image-attribution.md)。重复来源合并时 `provider_ids` 保留各 Provider。Provider 级详情在 `provider_runs` 返回。上游部分失败时结合 `partial`、`warnings`、`provider_runs` 判断覆盖范围。快速模式只为当前可调度的来源预留时限。严格图片许可检索会在首源缺少符合要求的候选时继续检查可用候补。文字摘要为发现线索，证据状态为 `unverified`。

错误对象包含 `request_id`、`code`、`message`、`retryable`，适用时含 `retry_after_ms`。响应头 `X-Request-ID` 可用于关联当前请求。典型状态如下。

`provider_runs.attempts=0` 表示没有发起来源调用。熔断跳过时的 `reason_codes` 可沿用之前触发熔断的诊断。本地并发等待和来源限额分别记录为 `local_provider_queue_timeout` 和 `local_provider_rate_limited`。所有失败来源均处于可估计的冷却时，`PROVIDER_UNAVAILABLE` 以最早可恢复来源的时间给出 `retry_after_ms`，REST 同时返回 `Retry-After`。

响应头 `X-Trace-ID` 由服务端生成，与脱敏运行日志中的追踪标识对应。

检索失败时，如果已经观察到来源调用结果，REST 与 MCP 的错误正文会带 `provider_runs`。字段沿用成功响应的来源、状态、耗时、候选数和尝试次数。授权、参数、配额错误或尚未返回结果的超时可能没有该字段。重放返回原始来源诊断，调用与 Provider 计量不重复写入。

SearXNG 来源记录可带 `reason_codes`，取值为 `upstream_captcha`、`upstream_rate_limited`、`upstream_timeout` 或 `upstream_unavailable`。一次调用可能观察到多个原因。有可交付候选时仍报告部分结果。正常空结果单独标为 `empty`。

| HTTP | 典型错误码 | 处理 |
| --- | --- | --- |
| 401 | `AUTH_REQUIRED` | 检查 Key 是否提供且有效 |
| 403 | `FORBIDDEN` | 检查 Key 作用域 |
| 409 | `IDEMPOTENCY_CONFLICT`、`REQUEST_IN_PROGRESS` | 使用新幂等标识或按提示等待 |
| 413 | `INVALID_REQUEST` | 请求体超过 32 KiB |
| 422 | `INVALID_REQUEST`、`SOURCE_UNAVAILABLE`、`SOURCE_MODALITY_UNAVAILABLE`、`EVIDENCE_UNAVAILABLE`、`MODE_UNAVAILABLE` | 检查参数与能力端点 |
| 429 | `RATE_LIMITED`、`QUOTA_EXCEEDED` | 按重试提示等待 |
| 502 | `PROVIDER_UNAVAILABLE` | 上游不可用时按预算与重试提示处理 |
| 503 | `AUTH_UNAVAILABLE`、`USAGE_UNAVAILABLE` | 检查数据库或 Redis |
| 504 | `TIME_BUDGET_EXCEEDED` | 可缩小搜索范围或检查上游 |

### 证据与任务

正文提取成功时，`evidence_records` 返回来源、取得时间、实际摘录、规范化正文 SHA-256 和字符偏移。结果以 `evidence_refs` 指向记录，并标记为 `content_extracted`。摘要、提取失败或预算外结果保持 `unverified`。L3 图片区分来源页访问、原图匹配与许可核验。

公开 PDF 的 `locator.kind` 为 `pdf_text`，`page_number` 从 1 开始，偏移针对该页规范化文本。`content_sha256` 覆盖已检查的规范化页及其边界，触及检查上限时证据带有截断警告。图像型 PDF 需要 OCR 时，检索候选标记 `evidence_status=ocr_pending` 并返回 `OCR_PENDING` 告警，无证据引用。空白、加密或损坏的 PDF 分别返回相应的提取错误。

`POST /api/v1/fetch` 接受一个 `url` 或至多 20 个 `urls`，可用 `max_pages`、`max_latency_ms`、`domain_allowlist` 和 `domain_denylist` 收紧处理范围。默认总时限 120 秒，包含排队时间。带 `idempotency_key` 的相同提交返回同一任务。按 `status_url` 查询进度、错误与证据列表。Worker 未就绪时返回 `WORKER_UNAVAILABLE`。

`POST /api/v1/fetch` 与 MCP `web_fetch` 接受 `extraction_mode`，默认 `static`。设置 `dynamic` 时，仅对部署允许的 HTTPS 域名执行隔离渲染，重定向逐跳应用同一授权。`GET /api/v1/capabilities` 的 `fetch_dynamic` 表示配置和渲染 Worker 就绪状态。部署未授权动态抓取时返回 `MODE_UNAVAILABLE`，已授权但暂时无可用 Worker 时返回 `WORKER_UNAVAILABLE`。同一幂等标识从静态模式改为动态模式会产生冲突。

动态证据使用 `extraction_method=browser_dom`、`locator.kind=rendered_html_text`。偏移和 `content_sha256` 基于规范化的渲染正文，`extraction_version` 记录 Chromium 和规范化器版本。标题、DOI 和发布日期使用原始响应的元数据。动态抓取支持 UTF-8 或 ASCII HTML 的内嵌脚本，以及部署方为该页面逐项授权的同源和跨源 JavaScript 与 JSON GET 资源。 [部署配置](deployment.md#可选动态-html-渲染)

使用资源清单时，`render_bundle_sha256` 对按 URL 排序的资源 URL、种类、MIME 和响应体 SHA-256 计算指纹。包含跨源资源时，指纹同时纳入页面的 COEP 策略和跨源资源的 CORS、CORP 响应头。同源资源包沿用原有指纹格式。

资源预取失败时任务返回对应诊断，包括 `FETCH_RESOURCE_REDIRECT`、`FETCH_ROBOTS_DENIED`、`FETCH_CONTENT_UNSUPPORTED`、`FETCH_BODY_TOO_LARGE`、`FETCH_RESOURCE_CORS_DENIED` 和 `FETCH_RESOURCE_POLICY_UNSUPPORTED`。含跨源资源的页面若提供无法安全回放的 COEP，则返回 `FETCH_RENDER_POLICY_UNSUPPORTED`。

抓取任务遇到图像型 PDF 时会进入 `partial` 终态，并在 `result.ocr_pending` 中返回待 OCR 的原始 URL 序号，从 0 开始。这些条目消耗抓取尝试预算，已提取页数仅统计实际生成证据的条目。当前 OCR 由外部授权适配器扩展。

`POST /api/v1/research` 使用 SearchRequest，接受 `auto` 或 `deep`。同一请求也可通过 `POST /api/v1/search` 并设置 `mode=deep` 提交。REST 与 MCP 共享任务与幂等标识。研究启动只扣一次，结果记录轮次、来源、证据、页数和模型计量。恢复时保留已预留的预算，执行中断不会令预算重新计数。未知的模型用量保持未知。

任务响应正文中的 `trace_id` 始终关联首次成功提交，查询和幂等重试的 `X-Trace-ID` 响应头关联当次传输。研究结果区分已预留 Token、实际 Token 和用量是否已知。发起模型生成前持久化预留额度及未知用量，响应中的用量确认后才写入实际值。`estimated_cost` 仅估算模型费用，以十进制字符串返回并按最多 14 位小数入账。未调用模型时为零，缺少用量或费率时为 `null`。报告按轮记录去重来源数和证据覆盖率，Console 展示分轮指标和最终综合阶段的模型输入、输出及费用。

研究 Worker 发起检索前保存该轮及其预算预留。进行中的轮次为 `running`。若在取得结果前丢失租约，轮次改为 `interrupted`，未观测的 `elapsed_ms`、`result_count` 和 `source_count` 为 `null`，重新领取时跳过已预留的轮次。最后一次租约结束时仍有可用候选的研究返回 `partial`。

启用本地索引时，纯本地研究按离线执行，正文公网抓取与模型预算均为零。Console 使用同源 Playground 提交研究，再通过 `/api/v1/console/jobs/{id}` 查询或取消本人任务。

### 管理面

管理接口使用浏览器会话，写操作还要求匹配配置的 `Origin` 以及 `X-CSRF-Token`。Console 通过同源 `/gateway` 路径转发。登录、会话刷新和登出分别使用 `/api/v1/auth/login`、`/api/v1/auth/refresh`、`/api/v1/auth/logout`。登录与刷新返回新的 CSRF 令牌，`GET /api/v1/me` 返回当前身份。

- `GET /api/v1/auth/sessions` 分页返回本人当前租户的活跃会话，包含稳定的管理 ID、创建时间、到期时间和 `current` 标记。分页参数为 `limit`（默认 20，最多 50）及 `cursor`，响应返回 `sessions` 和 `next_cursor`。
- `POST /api/v1/auth/sessions/{id}/revoke` 撤销其他活跃会话，成功返回 HTTP 204 并写入 `session.revoke` 审计。其他用户、其他租户和无效会话统一返回 `404 SESSION_NOT_FOUND`。撤销当前会话返回 `409 SESSION_CURRENT`，当前会话通过登出结束。刷新轮换登录凭据并保留管理 ID。
- `GET /api/v1/auth/memberships` 分页返回同一账户可切换的其他启用租户，字段为 `tenant_id`、`tenant` 和服务端角色 `role`。响应包含 `memberships` 和 `next_cursor`。`POST /api/v1/auth/switch` 接受 `tenant_id`，校验 Origin、CSRF 和有效成员资格后撤销当前凭据，返回目标租户的会话和新 CSRF。无权访问的目标统一返回 `404 MEMBERSHIP_NOT_FOUND`，当前租户返回 `409 MEMBERSHIP_CURRENT`。源租户和目标租户分别记录 `session.switch_out` 和 `session.switch_in`。
- `GET/POST /api/v1/keys` 查看掩码列表或创建密钥。创建与轮换响应的 `secret` 只返回一次。调用 `POST /api/v1/keys/{id}/rotate` 或 `/revoke` 更新生命周期。
- 创建密钥时可提交最多 32 项的 `ip_allowlist`。支持 IPv4、IPv6 单地址或主机位为零的 CIDR。单地址分别规范为 `/32` 或 `/128`，重复的规范条目会被拒绝。省略或传空列表表示不限 IP。列表、创建和轮换响应返回规范化白名单，轮换继承原白名单。REST 与 MCP 对不匹配或无法验证的地址返回 `401 AUTH_REQUIRED`。 [可信代理与网关](deployment.md#api-key-的-ip-白名单)
- `GET /api/v1/usage` 返回最近 30 个 UTC 自然日的计量、趋势、模式分布 `mode_counts` 和失败原因 `failure_counts`，以及当前租户配额。汇总覆盖可见范围内的整个时间窗。开发者仅能查看自己的用量，具备权限的角色可查看本租户用量。失败原因统计涵盖 `failed` 及已过预留期限的 `running` 请求，后者归为 `REQUEST_INTERRUPTED`。未分类的失败码归入 `OTHER_ERROR`。取消仍计入总览的错误数。
- `/api/v1/admin/` 提供总览、Provider、脱敏请求、租户和审计。平台管理员可创建租户、修改租户配额与 Provider 策略。运维、审计及租户管理员按各自权限读取。
- 平台管理员与运维可通过 `GET /api/v1/admin/requests/{id}` 读取脱敏请求及已存的来源运行记录。追踪返回实际尝试次数，早期记录的 `attempts` 为 `null`。运维仅能读取所在租户，跨租户 ID 与不存在的 ID 均返回 404。列表支持来源、模式、模态、租户、状态、时间与追踪 ID 筛选，租户筛选按角色授权。
- `GET /api/v1/admin/settings/query-retention` 返回查询原文保留设置和最近十次变更，`tenant_id` 缺省时使用当前租户。平台管理员可选择目标租户，运维仅能读取当前租户。默认 `retention_hours=0`、`version=0`。
- 平台管理员通过 `PATCH /api/v1/admin/settings/query-retention` 提交 `tenant_id`、整数 `retention_hours`（0 至 24）、`expected_version` 和 `confirm: true`。版本冲突返回 `409 SETTINGS_VERSION_CONFLICT`。实质变更写入 `settings.query_retention.update` 并使旧版本的原文立即不可查看。
- 平台管理员通过 `POST /api/v1/admin/requests/{id}/query` 提交记录所属 `tenant_id` 和 `confirm: true`。请求需要有效会话、Origin 和 CSRF。成功返回 `query` 与 `expires_at`，并写入 `request.query.reveal`。原文缺失、到期、版本失效或与租户不符时返回 `404 RETAINED_QUERY_NOT_FOUND`。原文响应使用 `Cache-Control: no-store`。
- `GET /api/v1/admin/images` 返回最近 30 个 UTC 自然日内已返回图片的许可计数 `counts`、来源计数 `source_counts` 和分页记录。列表可组合使用 `rights_status` 和 `source_status`，统计覆盖角色可见的整个时间窗。来源状态为 `missing`、`invalid`、`failed`、`not_matched`、`unverified` 或 `verified`。平台管理员查看全平台，运维仅查看当前租户。
- `GET /api/v1/admin/overview` 的 `job_queue` 返回 `queued`、`running`、`cancelling`、`queued_for_retry` 及最早有效排队的时间 `oldest_queued_at`。仅统计截止时间尚未到达的活动任务，并按角色限定可见租户。
- `GET /health/dependencies` 返回独立服务探针的 `checked_at` 和固定名称的 `checks`。状态为 `healthy`、`unavailable`、`unknown` 或 `not_configured`。响应仅包含服务名、状态和检查时间，Console 总览经同源路径独立加载。
- `GET /api/v1/admin/providers` 返回来源组合、过滤执行位置、分页支持、凭据要求和进程并发能力。实时熔断反映服务级状态。运维和审计员只读取当前租户的持久化运行量和延迟，平台管理员读取全平台并可修改 Provider 策略。
- `GET /api/v1/admin/audit/risks` 按审计权限返回最近 7 天最多 20 项敏感操作提示，包含事件 ID、租户、发生时间和规则码。覆盖管理权限变更、签发无 IP 白名单的生产密钥以及停用租户。用 `truncated` 表示是否省略更早的匹配事件。
- 平台管理员可通过 `GET /api/v1/admin/tenants/{id}/quota-usage` 查看 UTC 当日租户用量、实时活动数以及分页后的脱敏 Key 用量与有效配额。分页接受 `limit`、`cursor`，Key 页只返回掩码。

MCP 客户端使用相同的搜索鉴权与计量。 [MCP 接入](mcp.md)
