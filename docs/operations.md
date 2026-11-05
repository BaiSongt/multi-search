# 运维验证

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
| `multisearch_evidence_coverage_ratio` | 当前 API 进程观测到的带证据引用结果数占比 |

快照使用 5 秒共享缓存和 1.5 秒采集上限。租户类别当前为 `standard`。图片存在多个 Provider 来源时，每个来源在对应维度分别计数。API 进程的提取指标记录同步取得的证据，后台批量提取以持久化快照的 `multisearch_stored_fetch_observations_window` 为准。 [Prometheus 指标格式](https://prometheus.io/docs/instrumenting/exposition_formats/)

API 响应提供服务端生成的 `X-Trace-ID`。运行日志记录固定路由名、方法、状态、耗时和该追踪标识。调用方的 `X-Request-ID` 继续用于关联响应和授权查询，结构化日志只记录服务端追踪标识。指标标签使用固定枚举，日志字段与数据面内容分开。

```promql
histogram_quantile(0.95,
  sum by (le, mode) (rate(multisearch_request_duration_seconds_bucket[10m]))
)
```

## 逻辑备份与恢复演练

脚本使用 PostgreSQL 自定义归档格式备份业务数据库，输出归档和 SHA-256 校验文件。恢复演练先校验归档，再将它写入随机命名的独立数据库。验证迁移版本、租户、用量、任务及结果、证据记录后，清理该临时库。 [pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html) · [pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html)

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

## 性能与质量基线

样本包含通用知识、开发文档、新闻、照片和插画各 20 条，另有 20 条故障场景供回归设计。这些是整理好的评审候选。Top-10 有效率需要人工检查相关性、可访问性和内容匹配。 [检索样本](../scripts/ops/benchmark_cases.jsonl) · [异常场景](../scripts/ops/fault_cases.json)

注入一个或多个带文字与图片作用域的临时 Key 到 `MULTISEARCH_BENCHMARK_KEYS`，多个值用逗号分隔。默认顺序提交每个样本的 L1/L2 请求，间隔 1.1 秒，最多四个请求同时执行。

```sh
python scripts/ops/benchmark_search.py --compose-stats --output .secrets/ops/public-baseline.json
```

报告按模式和模态分组，列出成功请求的墙钟 P50/P95、HTTP 状态、Provider 限流、图片来源页完整率和许可证核验比例。失败样本保留已返回的 Provider 状态和服务端追踪 ID。`upstream_failures_without_runs` 统计没有来源诊断的超时或不可用错误。Provider 限流率以报告中 `status=rate_limited` 的 Provider 调用为分子，SearXNG 内部单个引擎可能在 Provider 返回 `partial` 时限流。容器 CPU 与内存来自运行前后两次采样。

在原始报告上离线评审，确保候选顺序和内容与评审时完全一致。

```sh
python scripts/ops/benchmark_search.py --report .secrets/ops/public-baseline.json \
  --review-template --output .secrets/ops/public-labels.json
python scripts/ops/benchmark_search.py --report .secrets/ops/public-baseline.json \
  --review .secrets/ops/public-labels.json --output .secrets/ops/public-reviewed.json
```

先生成模板并人工打开原始报告中的结果链接。仅当结果直接相关、可访问且正文或图片与查询匹配时标 `true`，判定无效标 `false`，留待复核标 `null`。每个数组严格按同次请求的返回顺序填写。模板和结果绑定原始报告的 SHA-256，全组结果标注完毕后才计算 Top-10 有效率。离线命令无需 Key，也不会重新检索。

不访问公网的基线使用 Mock Provider、真实 PostgreSQL/Redis 和四个临时租户，每轮并发 20。运行示例使用 Python 传递原始脚本字节，兼容 Windows 与 POSIX shell。

```sh
python -c "import pathlib, subprocess; subprocess.run(['docker','compose','exec','-T','multisearch-api','python','-','--redis-url','redis://redis:6379/15'], input=pathlib.Path('scripts/ops/mock_load.py').read_bytes(), check=True)"
```

2026-11-05 本机 Compose 测得 40 次 Mock 冷请求的 P50 为 129.874 ms、P95 为 159.509 ms、P99 为 170.121 ms。另有 40 次租户内缓存命中通过隔离检查，80 条完成事件落入 PostgreSQL。该计时包含鉴权、计量、Redis、路由与约 2 ms 的模拟 Provider 延迟，未含 HTTP 传输。一次独立临时库恢复验证了迁移 `0007_local_index` 和 1 条持久化任务结果。公网 SLO 应依据完整评审样本和固定出口环境确定。

2026-11-06 本地 Compose 完成 100 条样本的 L1/L2 请求，共 200 次。延迟按成功响应统计，包含 `partial`。

| 模式与模态 | HTTP 200 / 样本 | P50 / P95 | 图片来源页完整率 |
| --- | ---: | ---: | ---: |
| L1 文字 | 25 / 60 | 1125 / 1734 ms | — |
| L2 文字 | 23 / 60 | 1078 / 2078 ms | — |
| L1 图片 | 40 / 40 | 1125 / 1219 ms | 99.50% |
| L2 图片 | 40 / 40 | 1094 / 1234 ms | 99.25% |

文字的 72 次失败均为 HTTP 502 `PROVIDER_UNAVAILABLE`。L2 图片的 40 次成功均标记为 `partial`，两组图片均未取得许可核验证据。HTTP 429 为零。结束时采样记录 API 103.8 MiB、SearXNG 110.4 MiB、Worker 131.6 MiB 内存，CPU 分别为 0.19%、2.11%、100.38%。该采样是瞬时值。Top-10 人工判读尚待完成。
