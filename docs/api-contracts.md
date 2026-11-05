# REST API 契约

在本地服务访问 `http://127.0.0.1:8765/docs` 或 `/openapi.json` 获取当前请求与响应模型。接口前缀为 `/api/v1`，使用 UTF-8 JSON。时间戳为 UTC，耗时单位为毫秒。

| 方法与路径 | 权限 | 行为 |
| --- | --- | --- |
| `GET /health/live` | 公开 | 进程存活 |
| `GET /health/ready` | 公开 | 数据库和 Redis 就绪 |
| `GET /api/v1/capabilities` | 公开 | 当前启用的模式、来源、模态与预算 |
| `GET /api/v1/providers` | API Key | Provider 能力与运行状态 |
| `POST /api/v1/search` | API Key | 统一文字与图片检索 |

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

请求可通过 `max_latency_ms`、`max_results`、`max_image_results` 收紧预算。首轮正文提取与模型调用均为零。请求同时包含可用和不可用的来源模态组合时，返回部分结果并为跳过的组合给出告警。要求证据时返回 `EVIDENCE_UNAVAILABLE`，指定深度研究时返回 `MODE_UNAVAILABLE`。私网与离线请求需要受批准的内部来源，首轮实例无此配置。对应请求返回 `SOURCE_UNAVAILABLE`，请求内容不会送往公网或复用公网缓存。

`language` 和 `region` 按上游能力尽力处理。`time_range` 使用 `{"from":"YYYY-MM-DD","to":"YYYY-MM-DD"}`，保留已知发布日期在范围内的候选。`domain_allowlist` 和 `domain_denylist` 用于过滤结果来源域名。请求的域名字段不会授予访问内网的权限。

`cache_policy=use` 默认读取可用缓存。`refresh` 重新搜索并更新可缓存响应，`bypass` 直接检索且跳过缓存写入。`local` 只支持 `use`，未命中时以部分结果返回 `LOCAL_CACHE_MISS`。缓存按租户隔离，按查询、有效路由参数和 Provider 组合区分。命中时 `cache_hit=true`，返回当前调用的请求标识。完整结果缓存期限为文字一小时、图片半小时、新闻十分钟，新闻优先于图片和普通文字。正常空结果缓存 30 秒，上游部分失败的响应跳过缓存。

首轮默认每租户 60 次每分钟，单 API 进程全局并发 20、每租户并发 5。完整计量和多维配额属于 WP-07。请求级幂等标识也待后续支持，当前传入 `idempotency_key` 返回 `INVALID_REQUEST`。

### 结果与错误

`results` 按 `kind` 区分 `text` 与 `image`，图片详见 [图片归属契约](image-attribution.md)。重复来源合并时 `provider_ids` 保留各 Provider。Provider 级详情在 `provider_runs` 返回。上游部分失败时结合 `partial`、`warnings`、`provider_runs` 判断覆盖范围。文字摘要为发现线索，证据状态为 `unverified`。

错误对象包含 `request_id`、`code`、`message`、`retryable`，适用时含 `retry_after_ms`。响应头 `X-Request-ID` 可用于关联当前请求。典型状态如下。

| HTTP | 典型错误码 | 处理 |
| --- | --- | --- |
| 401 | `AUTH_REQUIRED` | 检查 Key 是否提供且有效 |
| 403 | `FORBIDDEN` | 检查 Key 作用域 |
| 413 | `INVALID_REQUEST` | 请求体超过 32 KiB |
| 422 | `INVALID_REQUEST`、`SOURCE_UNAVAILABLE`、`SOURCE_MODALITY_UNAVAILABLE`、`EVIDENCE_UNAVAILABLE`、`MODE_UNAVAILABLE` | 检查参数与能力端点 |
| 429 | `RATE_LIMITED` | 按重试提示等待 |
| 502 | `PROVIDER_UNAVAILABLE` | 上游不可用时按预算与重试提示处理 |
| 503 | `AUTH_UNAVAILABLE`、`RATE_LIMITER_UNAVAILABLE` | 检查数据库或 Redis |
| 504 | `TIME_BUDGET_EXCEEDED` | 可缩小搜索范围或检查上游 |
