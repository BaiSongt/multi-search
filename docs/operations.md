# 运维验证

## 服务状态与审计提示

Console 的服务总览独立读取 `GET /health/dependencies`。该端点只公开固定服务名、状态和 UTC 检查时间，每个探针限时 1.5 秒，同一进程在两秒内复用采样。Redis 无法读取时，依赖心跳的 Worker 显示为未确认。未配置研究或动态抓取时，对应 Worker 显示为未配置。尚未观测到静态抓取心跳时显示为未确认。

审计与安全页面列出当前角色可见范围内最近 7 天的敏感操作，包含管理员权限变更、签发无 IP 白名单的生产密钥及停用租户。提示基于已记录的管理操作，使用审计事件 ID 进行复核。 [接口契约](api-contracts.md#管理面)

## 受保护的指标

`GET /metrics` 使用独立的指标凭据。部署时通过 `MULTISEARCH_METRICS_TOKEN_FILE` 指向容器内的密钥文件，或者通过 `MULTISEARCH_METRICS_TOKEN` 注入 32–256 个可见 ASCII 字符。采集端在请求中发送 `Authorization: Bearer <metrics-token>`。

`MULTISEARCH_METRICS_ALLOW_LOOPBACK=true` 允许在 API 容器内从直接回环连接检查指标。应用以请求对端地址判断回环，带转发头的请求需要独立令牌。Docker 宿主机访问发布端口时可能经过桥接地址，容器间采集使用独立令牌。

```yaml
scrape_configs:
  - job_name: multisearch
    scrape_interval: 30s
    scrape_timeout: 5s
    metrics_path: /metrics
    authorization:
      type: Bearer
      credentials_file: /run/secrets/multisearch_metrics
    static_configs:
      - targets: ["multisearch-api:8765"]
rule_files:
  - /etc/prometheus/rules/multisearch.yml
```

告警规则包括指标端点故障、依赖采集失败、Worker 未就绪且队列有积压，以及 Provider 持续熔断。将示例规则挂载到 Prometheus 容器的规则文件路径。采集器跨主机部署时通过受控 TLS 网络访问。 [告警规则](../scripts/ops/alerts.yml)

| 指标组 | 统计口径 |
| --- | --- |
| 进程 Counter 与 Histogram | 搜索、Provider、缓存命中、图片归属、鉴权、配额拒绝和 Console 操作。计数在 API 进程重启后重新累计 |
| `multisearch_stored_*_window` | PostgreSQL 最近 30 分钟的有界快照，每数据集至多读取 4000 条。它们是 Gauge，`multisearch_sample_capped` 指示快照可能截断 |
| `multisearch_jobs_active`、`multisearch_oldest_queued_job_seconds` | 持久化活跃队列和最早排队时间，按抓取和研究模式汇总 |
| `multisearch_worker_ready`、`multisearch_redis_used_memory_bytes` | Redis 中 Worker 心跳存在性及 Redis 当前内存 |
| `multisearch_metrics_dependency_up` | PostgreSQL 和 Redis 快照采集是否成功 |
| `multisearch_fetch_total` | API 与 Worker 实际尝试的正文提取次数，按 MIME 类型和 `completed`、`failed`、`blocked`、`timeout`、`ocr_pending`、`cancelled` 汇总。没有取得可确认响应类型时记为 `unknown` |
| `multisearch_evidence_coverage_ratio` | 当前 API 进程观测到的带证据引用结果数占比 |

快照使用 5 秒共享缓存和 1.5 秒采集上限。租户类别当前为 `standard`。图片存在多个 Provider 来源时，每个来源在对应维度分别计数。正文提取 Counter 通过 Redis 在同一部署内共享，采集端应只配置一个 API 实例。缓存命中、幂等重放和因预算未启动的页面都不会增加次数。计数异步提交，Redis 写入故障或进程非正常终止时，可能丢失尚未提交的计数。持久化快照 `multisearch_stored_fetch_observations_window` 继续统计近期独立抓取任务的最终记录。 [Prometheus 指标格式](https://prometheus.io/docs/instrumenting/exposition_formats/)

API 响应提供服务端生成的 `X-Trace-ID`。运行日志记录固定路由名、方法、状态、耗时和该追踪标识。调用方的 `X-Request-ID` 继续用于关联响应和授权查询，结构化日志只记录服务端追踪标识。指标标签使用固定枚举，日志字段与数据面内容分开。

通过 `X-Trace-ID` 可关联 `fetch.extract` 日志中的提取状态、耗时和已知错误代码。抓取日志省略目标 URL、请求头和正文。

```promql
histogram_quantile(0.95,
  sum by (le, mode) (rate(multisearch_request_duration_seconds_bucket[10m]))
)
```

## 逻辑备份与恢复演练

脚本使用 PostgreSQL 自定义归档格式备份业务数据库，输出归档和 SHA-256 校验文件。恢复演练先校验归档，再将它写入随机命名的独立数据库。验证迁移版本、租户、用量、任务及结果、证据记录后，清理该临时库。 [pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html) · [pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html)

项目逻辑备份保留查询保留策略、审计和原文表结构，并排除 `retained_queries` 的数据。恢复演练拒绝含有该表数据的归档，兼容早期版本中没有该表的归档。队列中的待执行任务继续保留恢复所需的任务负载。物理快照与 WAL 由部署方按其备份保留策略管理。 [查询保留设置](api-contracts.md#管理面)

```sh
python scripts/ops/db_backup.py backup .secrets/ops/multisearch-2026-11-05.dump
python scripts/ops/db_backup.py restore-check .secrets/ops/multisearch-2026-11-05.dump
```

使用 `--project` 指定其他 Compose 项目。归档包含账户哈希、租户数据和证据元数据，保存在仅运维可读的目录。Redis 缓存可重建，PostgreSQL 保存任务、计量和证据。恢复部署时需单独从密钥管理位置取得 API Key pepper。恢复较旧快照后，按运维流程检查快照之后的密钥吊销和会话变更。

## Worker 重启

在独立 Compose 项目中启动 `fetch` Profile，并配置独立回环端口。以下为 POSIX shell 示例。

```sh
export MULTISEARCH_API_PORT=18765
export MULTISEARCH_CONSOLE_PORT=13000
export MULTISEARCH_PUBLIC_API_BASE_URL=http://127.0.0.1:18765
export MULTISEARCH_PUBLIC_CONSOLE_URL=http://127.0.0.1:13000
docker compose --project-name multisearch-drill-fetch --profile fetch up --build --wait
```

为该项目签发一个临时 `fetch` Key。由测试环境提供至少两个允许抓取的公开 HTTPS 网页，处理时长应足以观察到 `running`。将密钥注入 `MULTISEARCH_RECOVERY_KEY` 后执行。

```sh
python scripts/ops/rehearse_worker_restart.py \
  --project multisearch-drill-fetch --api-url http://127.0.0.1:18765 \
  --url https://example.org/ --url https://example.com/
```

脚本核对 Compose 项目与 API 端口的对应关系，观察正在运行的任务，再重启该项目的 Worker。验收要求容器启动时间改变、任务 ID 保持一致，且重领次数增加或出现明确的中断终态。若示例网页提取过快，改用可控制响应延迟的公开测试站点。

也可在独立 `research` 项目启用 Crossref、arXiv 和研究 Worker，使用具备 `research` 与 `search:text` 权限的临时 Key，将 `--url` 替换为 `--research-query "attention transformer neural network"`。本机演练中，Worker 容器启动时间改变，同一研究任务的尝试次数从 1 增至 2，最终到达 `partial`。

## 公网正文验收

在启用了 `fetch` 或 `research` 配置的 Compose 项目中运行探针。输入应为已确认允许抓取和保留摘录的公开 URL，Worker 的 DNS 和出站链路应能按安全策略直接连接站点。

```sh
docker compose --profile fetch exec -T multisearch-worker python - \
  https://www.rfc-editor.org/rfc/rfc8785.html < scripts/probe_public_fetch.py
docker compose --profile fetch exec -T multisearch-worker python - \
  https://www.rfc-editor.org/rfc/rfc8785.pdf --method pypdf < scripts/probe_public_fetch.py
```

HTML 默认要求 Crawl4AI 提供提取提示。可用 `--method static_html` 单独验收静态解析。探针会签发并吊销临时抓取 Key，经 REST 入队和重放，校验可定位正文、证据读取以及 PostgreSQL 中的单次用量与追踪。无正文、抓取失败或解析方法不符时返回非零状态。Worker DNS 返回非公网地址时输出策略拒绝诊断。

启用动态配置后，可用 `--method browser_dom` 验收允许保留摘录的公开测试 URL。页面域名、所需资源的 URL 和跨源资源的域名须事先加入对应的部署清单。受控探针使用同一隔离渲染器和临时 PostgreSQL，以夹具模拟来源 DNS 和 HTTP 响应。它覆盖同源模块与跨源脚本、JSON 生成正文，验证来源 CSP、CORS、CORP 和 COEP，以及预检、凭据和未授权子请求的阻断。探针还检查 robots、时限、证据定位、资源包指纹和任务幂等。 [部署清单](deployment.md#可选动态-html-渲染)

```sh
docker compose -f compose.yml -f compose.dynamic.yml --profile dynamic \
  exec -T multisearch-worker python - < scripts/probe_dynamic_fetch.py
```

## 性能与质量基线

样本包含通用知识、开发文档、新闻、照片和插画各 20 条，另有 20 条故障场景供回归设计。这些是整理好的评审候选。Top-10 有效率需要人工检查相关性、可访问性和内容匹配。 [检索样本](../scripts/ops/benchmark_cases.jsonl) · [异常场景](../scripts/ops/fault_cases.json)

注入一个或多个带文字与图片作用域的临时 Key 到 `MULTISEARCH_BENCHMARK_KEYS`，多个值用逗号分隔。默认顺序提交每个样本的 L1/L2 请求，间隔 1.1 秒，最多四个请求同时执行。

```sh
python scripts/ops/benchmark_search.py --compose-stats --output .secrets/ops/public-baseline.json
```

报告按模式和模态分组，列出成功请求的墙钟 P50/P95、HTTP 状态、Provider 限流、图片来源页完整率和许可证核验比例。失败样本保留已返回的 Provider 状态、尝试次数、冷却提示和服务端追踪 ID。`provider_attempted_reason_counts` 和 `provider_skipped_reason_counts` 分别汇总发起调用和零尝试时的诊断。`upstream_failures_without_runs` 统计没有来源诊断的超时或不可用错误。Provider 限流率以报告中 `status=rate_limited` 的 Provider 记录为分子，SearXNG 内部单个引擎可能在 Provider 返回 `partial` 时限流。容器 CPU 与内存来自运行前后两次采样。

使用原始报告生成本地评审工作区，并在浏览器打开生成的 HTML。确认每条查询符合样本要求，并分别判断每个候选的相关性、可访问性和内容匹配。需要暂停时可以导出进度，之后在同一报告的工作区导入继续。

```sh
python scripts/ops/benchmark_review.py --report .secrets/ops/public-baseline.json \
  --output .secrets/ops/public-review.html
python scripts/ops/benchmark_search.py --report .secrets/ops/public-baseline.json \
  --review .secrets/ops/public-labels.json --output .secrets/ops/public-reviewed.json
```

将工作区导出的标注 JSON 保存为 `public-labels.json` 后运行第二条命令。标注与原始报告的 SHA-256 和返回顺序绑定，全组有候选且标注齐全后才计算该组的 Top-10 有效率。工作区只在评审者主动点击时打开候选链接。可使用 `--review-template` 生成原有 JSON 模板，直接按返回顺序填写 `true`、`false` 或待复核的 `null`。离线命令无需 Key，也不会重新检索。

20 条新闻样本各有固定 UTC 日期窗口。报告按模式统计发出日期过滤的请求、成功但没有候选的请求，以及上游因发布日期未知而被过滤的候选。`observable_error_ratio` 的分子是返回候选中发布日期越界、缺失或无效的数量。工作区还要求对范围内候选核对来源页上的真实发布日期，这一判定与 Top-10 判定独立。只有待复核候选全部判定后才输出 `temporal_error_ratio`。发布日期与检索时间相同只列为待查线索。可用 `--case-id` 选择单个或多个样本做复验。

本地日期研究探针使用独立测试租户和五份夹具文档，验证 REST 和 MCP 重用同一持久任务，并按 UTC 保留首尾两天。启用本地索引和 `research` 配置后执行。

```sh
docker compose --profile research exec -T multisearch-api python - --local-date-window \
  < scripts/probe_research.py
```

学术来源使用独立的 12 条标题、DOI 和 arXiv ID 样本。启用 Crossref 与 arXiv 后，使用具备 `search:text` 作用域的临时 Key。报告保留每条样本的原始文献链接，以及候选的 DOI、arXiv ID 和版本，便于复核同一论文。 [学术样本](../scripts/ops/academic_cases.jsonl)

```sh
python scripts/ops/benchmark_search.py --cases scripts/ops/academic_cases.jsonl \
  --concurrency 1 --submit-interval 3.1 --output .secrets/ops/academic-baseline.json
python scripts/ops/academic_quality.py .secrets/ops/academic-baseline.json \
  --fail-on-identifier-miss
```

本地索引的独立样本在临时 PostgreSQL 库中运行，覆盖中文和英文查询、筛选、有序结果上限、更新与两租户隔离。公网 Provider 和共享缓存作为故障哨兵，任何意外调用都会使验收失败。 [本地索引探针](../scripts/probe_local_index.py)

不访问公网的基线使用 Mock Provider、真实 PostgreSQL/Redis 和四个临时租户，每轮并发 20。运行示例使用 Python 传递原始脚本字节，兼容 Windows 与 POSIX shell。

```sh
python -c "import pathlib, subprocess; subprocess.run(['docker','compose','exec','-T','multisearch-api','python','-','--redis-url','redis://redis:6379/15'], input=pathlib.Path('scripts/ops/mock_load.py').read_bytes(), check=True)"
```

2026-11-05 本机 Compose 测得 40 次 Mock 冷请求的 P50 为 129.874 ms、P95 为 159.509 ms、P99 为 170.121 ms。另有 40 次租户内缓存命中通过隔离检查，80 条完成事件落入 PostgreSQL。该计时包含鉴权、计量、Redis、路由与约 2 ms 的模拟 Provider 延迟，未含 HTTP 传输。一次独立临时库恢复验证了迁移 `0007_local_index` 和 1 条持久化任务结果。公网 SLO 应依据完整评审样本和固定出口环境确定。

`0.3.0-alpha.4` 在最终 Alpine API 镜像上复测三次。每次验证 40 个冷请求、40 次隔离缓存命中及 80 条完成事件，P95 分别为 232.004、217.194 和 197.888 ms。后两次未同时运行浏览器测试和镜像扫描。本机测量尚未稳定达到 200 ms。

延迟报告分别记录鉴权、配额入账、检索和结果写入，各阶段分位数独立计算。默认每轮 20 个并发冷请求，随后依次验证 20 次缓存命中。可用 `--warmup-rounds 1 --concurrent-cache-hits` 增加独立预热轮次并以同一并发度测量缓存命中，预热只计入事件校验。

`0.3.0-alpha.5` 在同一 PostgreSQL、Redis 和容器资源限制下，以独立进程交替运行三对冷请求测试。已发布的 `alpha.4` P95 为 198.713、226.247、211.567 ms，优化候选为 183.391、184.534、195.543 ms。三对测试的 P95 中位数从 211.567 ms 降至 184.534 ms，配额入账阶段的 P95 中位数从 141.596 ms 降至 120.238 ms。每次验证 40 个并发冷请求、40 次隔离缓存命中和 80 条完成事件。

2026-11-06 本地 Compose 完成 100 条样本的 L1/L2 请求，共 200 次。延迟按成功响应统计，包含 `partial`。

| 模式与模态 | HTTP 200 / 样本 | P50 / P95 | 图片来源页完整率 |
| --- | ---: | ---: | ---: |
| L1 文字 | 25 / 60 | 1125 / 1734 ms | — |
| L2 文字 | 23 / 60 | 1078 / 2078 ms | — |
| L1 图片 | 40 / 40 | 1125 / 1219 ms | 99.50% |
| L2 图片 | 40 / 40 | 1094 / 1234 ms | 99.25% |

文字的 72 次失败均为 HTTP 502 `PROVIDER_UNAVAILABLE`。L2 图片的 40 次成功均标记为 `partial`，两组图片均未取得许可核验证据。HTTP 429 为零。结束时采样记录 API 103.8 MiB、SearXNG 110.4 MiB、Worker 131.6 MiB 内存，CPU 分别为 0.19%、2.11%、100.38%。该采样是瞬时值。Top-10 人工判读尚待完成。

`0.3.0-alpha.2` 在同一台机器复测同一组 200 个请求。两种模式的图片均成功，标准模式的 SearXNG 在全部 40 次图片请求中提供部分结果。75 次文字 502 均带来源诊断。文字请求成功数随上游状态变化，仍需在目标出口复验。

| 模式与模态 | HTTP 200 / 样本 | P50 / P95 | 图片来源页完整率 |
| --- | ---: | ---: | ---: |
| L1 文字 | 23 / 60 | 1078 / 2421 ms | — |
| L2 文字 | 22 / 60 | 985 / 1203 ms | — |
| L1 图片 | 40 / 40 | 1063 / 1172 ms | 99.50% |
| L2 图片 | 40 / 40 | 1063 / 1187 ms | 99.25% |
