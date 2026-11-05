# 实施验收

阶段 I 覆盖 WP-01 至 WP-08。`v0.1.0-alpha.1` 为工程与检索基线，`v0.1.0-alpha.2` 加入图片核验、MCP、账户与用量及角色工作区。

## 本地质量检查

安装依赖后可以在无公网检索的环境运行单测与契约测试。Python 依赖由 `backend/uv.lock` 固定，Console 依赖由 `console/package-lock.json` 固定。

```sh
cd backend
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest
cd ..
npm --prefix console ci
npm --prefix console run check
npm --prefix console run build
```

Console 浏览器回归需要先安装 Playwright Chromium，再启动本地 Compose 服务。

```sh
cd console
npx playwright install chromium
npm run test:workspace
```

## Compose 冒烟

按 [部署文档](deployment.md) 启动服务，然后运行以下命令。

```sh
python scripts/smoke.py
python scripts/smoke_management.py
```

脚本验证 API 存活与就绪、Console 首页、公开能力发现、搜索权限、图片作用域、请求校验、离线边界以及吊销生效。它在两个固定的本地验收租户中签发短期测试 Key，并在结束时吊销。公开检索探测仅发送通用测试关键词。脚本使用本地回环地址，密钥不会出现在输出中。

公网探测包含 SearXNG 图片分类 JSON 请求与 MultiSearch 文字、图片检索。上游部分失败且仍返回合法候选时报告“部分成功”。某一模态零候选或上游全部失败时报告“未验收”，默认以非零状态退出。`--allow-upstream-failure` 可用于仅判断容器和本地契约是否通过。完整的可缓存公网响应可验证同租户本地请求命中，文字响应还用于验证跨租户缓存隔离。上游部分失败或无结果时，相应缓存场景显示未验收。

如已修改宿主机端口，使用回环地址提供端点。

```sh
python scripts/smoke.py --api-url http://127.0.0.1:18765 --console-url http://127.0.0.1:13000
```

## 首轮本机验收记录

2026-11-05 在 Docker Desktop 的 Linux 容器环境中验证。

| 检查 | 结果 |
| --- | --- |
| Compose | 五个服务全部健康，Console 生产构建与 Alembic 迁移成功 |
| 脱网自动化 | 55 项契约、Provider 和路由测试通过，Ruff 与 Console TypeScript 检查通过 |
| PostgreSQL 身份 | 验证密钥签发、模态作用域、成员停用、吊销和审计，同一用户在两个租户中的机器身份保持隔离 |
| Redis 缓存 | Mock Provider 在真实 Redis 上处理 20 个并发请求，验证四个租户分别命中各自缓存，以及未检索的租户缓存未命中 |
| 公网 REST | 文字和图片都返回真实候选，部分上游受验证码、限流或错误影响，响应保留结果并标记为 `partial` |
| SearXNG JSON | 直接验证一般、新闻与图片分类，图片候选包含独立原图和落地页字段 |

公网部分响应跳过缓存。缓存验收使用 Mock Provider 与真实 Redis。本轮对 `NASA` 和 `Golden Gate Bridge` 取得了候选，一次开发者文档查询曾因上游不可用返回 HTTP 502。此记录反映当时的出口与上游状态。

## 阶段 I 本机验收记录

- 97 项阶段 I 后端测试通过，Ruff 与 TypeScript 检查通过。
- Compose 生产构建通过，真实 PostgreSQL 与 Redis 管理面冒烟通过，覆盖 CSRF、角色隔离、租户创建、幂等扣额、超额拒绝、密钥轮换吊销和审计。
- 官方 MCP 客户端对 Compose 实例发现并调用两个工具，验证各计一次和吊销即时生效。
- Chromium 验证真实登录、角色页面隔离、一次性密钥、分页、用量、租户额度审计、主题和会话生命周期。图片未知授权提示使用可重复的夹具验证。
- Commons 样本核验到 CC BY-SA 2.5 声明与文件页固定版本。 [来源证据](https://commons.wikimedia.org/w/index.php?oldid=1265385258)
- 公网冒烟返回真实文字和图片候选。两种模态均有部分上游失败，公网部分结果跳过缓存。缓存与租户隔离已有 Mock Provider 加真实 Redis 验收。

## 阶段 II 本机验收记录

- 224 项阶段 II 后端测试通过，覆盖 L3 证据、SSRF、图片来源、专业适配、本地索引、队列和运维权限。
- `fetch` 配置的七个服务健康。独立 PostgreSQL 验证幂等、租户隔离、租约重领、旧 Worker 隔离、取消和证据计量。
- Crawl4AI 在断网、只读和非 root 环境通过令牌与原始 HTML 解析验收。安全抓取的 TLS 测试验证目标 IP 固定及证书主机名校验。
- 当前出口对公网抓取样本返回非全局 DNS 地址，真实 Worker 在一次尝试后记录 `FETCH_TARGET_BLOCKED`。成功提取和持久化另由受控页面夹具与独立数据库验证。
- Chromium 通过真实团队邀请、角色更新、路由试算、缓存失效、任务列表和 CSV。证据定位与取消界面采用固定响应测试。
- Compose 中 Crossref 与 arXiv 均返回真实候选。离线索引同租户命中一条，另一租户返回零条，混合公网来源被拒绝。

在本地 Compose 运行状态下，可通过以下命令重复执行独立队列数据库验收。探针创建并移除专用临时数据库，生产运行时角色应使用其受限权限。

```sh
docker compose exec -T multisearch-api python - < scripts/probe_job_store.py
```

## 阶段 III 本机验收记录

- 258 项后端测试通过。`research` 配置的七个服务健康，数据库迁移到 `0008_trace_and_cost`。
- 官方 MCP 客户端发现全部六个工具。REST 与 MCP 重试关联同一任务和追踪 ID，计量只写一次。其他租户查询返回 404，指标端点要求独立凭据。
- 独立 PostgreSQL 中四个租户并发提交 20 个任务，每个任务完成并计量一次。队列与同步请求共享 Key 并发额度，吊销后阻止执行排队任务。
- Chromium 复验 Playground 研究进度、报告、取消及追踪 ID，验证真实租户创建后可从分页结果中筛选，以及登录、密钥生命周期、权限和审计。研究界面使用固定响应测试，任务执行由独立 Compose 探针验证。
- 真实学术研究返回 30 条候选。出口的安全抓取限制使任务无正文证据，报告为 `partial`。独立 Compose 实际重启 Worker 后，同一任务从第 1 次尝试恢复到第 2 次并到达终态。
- PDF 受控测试覆盖原页码和页内偏移、大小和时间限制、损坏与无文本层。只读、无网络的 Linux 容器验证解析进程的资源和文件访问限制。OCR 仍需集成。
- Python 与 TypeScript SDK 通过本地 HTTP 契约测试。SBOM、锁文件漏洞检查范围以及部署后续见安全记录。 [安全验收](security.md)
- Mock 冷请求 P95 为 159.509 ms。100 条样本的两种公网模式均已测量。文字成功率、图片许可核验和 Top-10 人工评审仍有缺口，公网 SLO 尚未冻结。 [性能基线](operations.md#性能与质量基线)

### 公网对照与评审

- 同一通识查询的直接对照复现了 SearXNG 返回零候选，内部 Brave 限流、DuckDuckGo 验证码，以及 DDGS 暂时没有候选。稍后同一查询恢复。混合结果保留来源诊断，交付候选的部分成功响应仍允许继续调用该 Provider。
- Commons 对照在 Compose 容器中使用真实许可接口，L2 和 L3 的 `verified_only` 只保留具备固定版本证据的 CC BY-SA 2.5 候选，零元数据预算返回空结果。探针固定检索候选，L3 对照页仍受当前出口的非公网 DNS 限制。 [许可证据](https://commons.wikimedia.org/w/index.php?oldid=1265385258)
- 默认图片基准未指定元数据核验预算，用于测量候选和来源页字段覆盖。严格许可核验单独执行上述对照。

```sh
docker compose exec -T multisearch-api python - < scripts/probe_image_license.py
```

从同一份原始基准报告生成离线评审模板，完成人工判读后可验证并计算 Top-10 有效率。 [评审流程](operations.md#性能与质量基线)
