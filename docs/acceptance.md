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

### `0.3.0-alpha.2` 复验

- 在最终 Compose 镜像上，用固定失败来源得到 REST 502，并验证幂等重放带相同来源诊断。PostgreSQL 记录一次用量和两次来源调用。真实学术研究与 MCP 共享任务复验也通过。
- 100 条公网样本共 200 次请求，125 次返回成功，75 次文字检索失败均保留来源诊断。图片 L2 的 40 次聚合部分成功都保留了 SearXNG 候选。 [基线数据](operations.md#性能与质量基线)
- Crossref 与 arXiv 的 12 条独立学术样本完成两种模式的 24 次请求，全部返回 HTTP 200。按候选标识符比对原始文献，L1 命中 4／12，L2 命中 6／12。四条直接 DOI 查询在两种模式中均未命中对应文献，精确标识符检索仍需修正。 [学术样本](../scripts/ops/academic_cases.jsonl)
- 独立 PostgreSQL 中导入 40 份本地文档，验证 11 次本地查询与 4 次公网来源拒绝，覆盖有序上限、重导入、租户隔离以及 `offline` 和 `private`。本机端到端 P50 为 10 ms。 [本地索引探针](../scripts/probe_local_index.py)
- 扫描七个实际运行镜像并生成 SBOM。移除构建工具后，已识别的 Python 和 npm 包未发现严重或高危记录，底层镜像的高危门禁仍未通过。 [镜像报告](security.md#运行镜像-sbom-与漏洞扫描)

### `0.3.0-alpha.3` 复验

- 287 项后端测试、Console 类型检查和 Chromium 回归通过。七个 Compose 服务健康。
- 学术基准的四个 DOI 和三个 arXiv ID 在 L1、L2 中均排在第一，包含旧式 ID 和明确指定的 `v2`。12 条样本的原始文献前十命中从 L1 4／12、L2 6／12 提升到 11／12、12／12。OpenAlex 另用一条真实 DOI 验证单文献接口。 [学术复核命令](operations.md#性能与质量基线)
- 十次公网诊断样本中，四次图片检索成功，六次文字检索返回 502。按追踪 ID 确认真实失败的验证码与限流原因码已写入 PostgreSQL。完整公网质量与人工 Top-10 门禁仍待复验。
- 升级前备份可恢复到 `0008_trace_and_cost`。派生 PostgreSQL 镜像使用原数据卷启动，保留 575 条既有用量记录并完成 `0009_provider_reasons`。独立 PostgreSQL 队列复验覆盖四租户 20 并发、吊销、租约恢复和取消。 [镜像安全](security.md#运行镜像-sbom-与漏洞扫描)

### `0.3.0-alpha.4` 复验

- 307 项后端测试、9 项 SDK 测试、Console 类型检查和 Chromium 工作区回归通过。七个 Compose 服务健康，七个实际运行镜像的严重及高危扫描门禁通过。 [镜像安全](security.md#运行镜像-sbom-与漏洞扫描)
- 扫描 PDF 的 Worker 夹具在实际 Alpine 容器内执行隔离解析，返回 `partial`、OCR 待处理序号和空证据。Wolfi Crawl4AI 拒绝无凭据解析请求，使用专用凭据成功提取 HTML。 [接口契约](api-contracts.md#证据与任务)
- Chromium 使用持久化来源诊断验证追踪 ID 和学术来源筛选、验证码／限流明细、普通开发者拒绝与运维跨租户 404。 [管理接口](api-contracts.md#管理面)
- 独立 Compose 从自定义 YAML 加载快速模式 2500 ms／5 条预算、来源优先级和图片过滤。主 Compose 的四租户 20 并发队列和 REST／MCP 研究探针通过。 [部署配置](deployment.md#部署策略基线)
- 公网冒烟分别返回 3 条文字和 3 条图片候选，均为部分成功。三次 Mock 并发测量的 P95 为 232.004、217.194 和 197.888 ms，本轮仍未稳定达到 200 ms 的设计目标。 [性能基线](operations.md#性能与质量基线)

### `0.3.0-alpha.5` 复验

- 314 项后端测试与 Console 类型检查通过，七个 Compose 服务健康。
- 同一容器资源与 PostgreSQL／Redis 下交替运行三对测量，候选版 P95 均低于 200 ms。最终 Compose 的独立 20 并发复测 P95 为 180.876 ms，40 次冷请求、40 次隔离缓存命中及 80 条完成事件一致。 [性能基线](operations.md#性能与质量基线)
- 独立 PostgreSQL 验证等待租户锁期间过期的 Key 被拒绝且不计量，四租户队列、租约、取消及共享并发额度回归通过。
- Chromium 使用实际 PostgreSQL 数据验证运维 Provider 计数的租户隔离、平台管理员额度报告权限、脱敏 Key 分页和逐 Key 用量，并展开真实 SearXNG 的能力声明。 [管理接口](api-contracts.md#管理面)
- 受控来源返回验证码错误时，REST 502 重放保留原追踪 ID 和两条类型化诊断，PostgreSQL 只写入一次用量。真实 REST／MCP 共用研究任务、追踪、单次计量和指标鉴权通过。该次公网研究收集到 30 条候选，正文证据为 0，返回 `partial`。
- 七个实际运行镜像均生成 SBOM，并通过严重及高危漏洞扫描门禁。 [镜像安全](security.md#运行镜像-sbom-与漏洞扫描)

### `0.3.0-alpha.6` 复验

- 317 项后端测试、9 项 SDK 测试、Console 类型检查和 Chromium 角色工作区回归通过。七个 Compose 服务健康，七个运行镜像生成 SBOM 并通过严重及高危漏洞扫描门禁。 [镜像安全](security.md#运行镜像-sbom-与漏洞扫描)
- 隔离源站夹具通过 REST、Worker、Crawl4AI、pypdf 和 PostgreSQL，HTML 与 PDF 各返回一条可定位证据。损坏 PDF 失败，扫描 PDF 返回 OCR 待处理，两者都没有生成证据。受保护的提取 Counter 对四种对应结果分别计数一次。
- 真实 Compose 对公开 RFC 的 HTML 和 PDF 分别执行抓取。Worker DNS 返回非公网地址，两项正向公网门禁均返回 `FETCH_TARGET_BLOCKED`。受保护的 `unknown/blocked` 指标恰好增加 2，两次结构化追踪均记录相同诊断码。 [公网探针](operations.md#公网正文验收)
- Chromium 打开由 100 条查询、200 次公网请求生成的离线评审工作区，验证查询审定、分项标注、部分进度导入导出、报告摘要不匹配拒绝及无自动外部请求。原报告有 1157 条候选，开发文档两种模式均无候选。实际人工标注和公网覆盖率复验仍待完成。 [评审操作](operations.md#性能与质量基线)

### `0.3.0-alpha.7` 复验

- 318 项后端测试、9 项 SDK 测试、Console 类型检查和 Chromium 角色工作区回归通过。七个 Compose 服务健康，七个运行镜像生成 SBOM 并通过严重及高危漏洞扫描门禁。 [镜像安全](security.md#运行镜像-sbom-与漏洞扫描)
- 实际 API、MCP、PostgreSQL 和 Worker 复用同一个带日期范围的本地研究任务。五份夹具中只返回 UTC 窗口首尾的两份文档，排除窗口外和无日期的三份。任务完成后清除原始负载。 [日期探针](operations.md#性能与质量基线)
- 20 条日期固定的新闻样本在 `0.3.0-alpha.6` 的同步检索上运行 L1/L2，共 40 次 HTTP 200。L1 返回 22 条候选、12 次空结果，L2 返回 35 条候选、11 次空结果。57 条候选的发布日期格式有效且均落在请求范围内。两种模式的适配器共过滤 200 次发布日期未知的上游候选。原站发布日期的人工复核尚未完成，最终时效性错误率留空。
- Chromium 离线打开该新闻报告的评审页，可独立标注原站发布日期和 Top-10 三项条件。页面无自动外部请求。 [评审操作](operations.md#性能与质量基线)

### `0.3.0-alpha.8` 复验

- 335 项后端测试、9 项 SDK 测试、Console 类型检查和 Chromium 角色工作区回归通过。默认静态配置七个服务健康，动态能力显示为不可用。七个静态服务通过严重及高危漏洞门禁。
- 独立动态 Compose 的八个服务健康，API 显示动态能力可用。运行包为 Chromium `157.0.8103.38-1.1.x86_64`，已校验 RPM 签名和固定摘要。渲染容器断网、只读，以 UID 1000 运行并移除全部 Linux capabilities，浏览器启动时确认自身命名空间与 Seccomp-BPF 沙箱。终止浏览器主进程后，容器自动重启并恢复渲染。 [动态部署](deployment.md#可选动态-html-渲染)
- 受控来源夹具通过真实 Unix socket 渲染内嵌脚本，校验正文字符偏移、SHA-256、逐跳地址授权、robots、原始及渲染后的摘录限制和来源 CSP。探针覆盖 1 MiB 请求上限、脚本超时后的恢复、外部请求与文件访问阻断。独立 PostgreSQL 验证 REST／MCP 幂等、静态 Worker 跳过动态任务、证据归档及跨租户拒绝。 [动态探针](operations.md#公网正文验收)
- 动态配置八个实际运行镜像均生成 SBOM 并通过严重及高危漏洞扫描门禁。渲染镜像被识别为 openSUSE Tumbleweed `20261103`，SBOM 含 386 个组件。 [镜像扫描](security.md#运行镜像-sbom-与漏洞扫描)
- 动态预览覆盖内嵌脚本。依赖外部脚本或接口的动态站点、公网正文正向验证和人工 Top-10 仍待验收。

### `0.3.0-alpha.9` 复验

- 370 项后端测试、9 项 SDK 测试、Console 类型检查和 Chromium 工作区回归通过。默认静态配置七个服务健康，动态模式保持按部署启用。控制台可显示和复制动态资源包指纹。
- 独立动态 Compose 的八个服务健康。实际断网 Chromium 157 在原始 HTTPS 地址回放获准的同源 JavaScript 模块和 JSON GET，生成可定位正文。受控夹具验证了来源 CSP、禁止凭据和 Range 请求、按类型匹配及仅回放授权 URL。 [动态探针](operations.md#公网正文验收)
- Worker 复用 DNS 固定、robots、主机授权和响应体限制。测试覆盖资源拒绝、重定向拒绝、256 KiB 单资源与 512 KiB 合计上限，以及页面和资源间最严格的摘录限制。资源内容变化会更新资源包指纹和证据 ID，即使正文相同。独立 PostgreSQL 复验 REST／MCP 幂等、证据归档、跨租户拒绝和单次计量通过。 [抓取接口](api-contracts.md#证据与任务)
- 动态配置的八个实际运行镜像均生成 SBOM，并通过严重及高危漏洞扫描门禁。 [镜像扫描](security.md#运行镜像-sbom-与漏洞扫描)
- 同一版本的真实 RFC Editor HTML 探针因 Worker DNS 返回非公网地址而收到 `FETCH_TARGET_BLOCKED`。正向公网正文、跨源资源和人工 Top-10 仍待验收。

### `0.3.0-alpha.10` 复验

- 393 项后端测试、9 项 SDK 测试、Console 类型检查和 Chromium 工作区回归通过。默认静态配置的七个服务健康，动态能力保持按部署启用。独立动态配置的八个服务健康，API 报告动态抓取可用。
- 断网 Chromium 157 的 17 个受控用例通过，覆盖跨源普通脚本、模块及 JSON、来源 CSP、CORS、CORP 和 COEP，以及预检和凭据请求阻断。跨源无 CORS 脚本采用保守策略，`CORP: same-site` 要求同源。 [动态部署](deployment.md#可选动态-html-渲染)
- 隔离 Compose 通过 Worker、Unix socket 和 Chromium 将受控跨源响应生成可定位正文。测试验证了独立的资源主机授权、DNS 与 robots 拒绝、资源指纹及同源行为，并以真实 PostgreSQL 复验 REST／MCP 幂等、证据归档、跨租户隔离和单次计量。 [动态探针](operations.md#公网正文验收)
- 八个实际运行镜像均生成 SBOM。Trivy 0.76.1 未报告高危或严重漏洞，渲染器 SBOM 含 386 个组件。 [镜像扫描](security.md#运行镜像-sbom-与漏洞扫描)
- 公网 RFC Editor HTML 探针收到 `FETCH_TARGET_BLOCKED`，本次 Worker DNS 仍返回非公网地址。正向公网正文和人工 Top-10 仍待验收。 [公网探针](operations.md#公网正文验收)

### 公网基准复测（`0.3.0-alpha.10`）

100 条样本各执行 L1、L2，共 200 次真实请求。下表的延迟来自 HTTP 200 响应，原始报告还保留失败原因、追踪 ID 和容器资源采样。 [基准方法](operations.md#性能与质量基线)

| 模态 | 模式 | HTTP 200 | P50 | P95 | 图片来源页完整率 |
| --- | --- | --- | --- | --- | --- |
| 文字 | L1 | 26／60 | 1,625 ms | 1,906 ms | — |
| 文字 | L2 | 22／60 | 1,250 ms | 1,391 ms | — |
| 图片 | L1 | 40／40 | 1,344 ms | 1,547 ms | 99.23% |
| 图片 | L2 | 40／40 | 1,328 ms | 1,500 ms | 98.75% |

- 72 次文字请求返回 HTTP 502，追踪记录包含上游验证码、限流和不可用。40 次开发文档请求有 5 次返回候选，20 次通用知识 L2 请求均返回 502。本轮客户端 HTTP 429 为 0。
- 新闻返回 61 条日期落在筛选区间内的候选，实际来源发布日期仍待复核。原始报告的 925 条候选已生成离线评审工作区。Chromium 确认工作区摘要与原报告一致，并在加载和切换样本时保持离线。人工 Top-10 有效率待评审。
- Worker 对 RFC Editor、IETF、W3C 等九个公开文档站点的入口域名均得到 `198.18.0.0/15` 内的地址。此环境下公网正文正向验收仍未通过。

### `0.3.0-alpha.11` 复验

- 398 项后端测试、9 项 SDK 测试和 Console 类型检查通过。隔离 Compose 的八个服务健康，同源与跨源动态抓取、REST／MCP 单次计量和本地研究日期边界复验通过。
- 独立 PostgreSQL 的 8 个研究用量场景通过。新旧模型检查点均能在租约耗尽后保留预留、报告未知用量与费用。重新领取后无需再次调用模型，已确认用量的取消任务仍入账。费用迁移保留旧账单值，支持精确保存 `0.00000000000001` 及未知费用 `NULL`。模型响应使用受控夹具。
- 真实 PostgreSQL 验证图片来源六种状态、旧记录归类、组合筛选、游标分页和租户隔离。队列汇总覆盖空租户、租户隔离、全局、重试和最早等待时间，已终结或过截止的任务从积压中排除。
- Chromium 工作区回归通过。审计员可只读打开 Provider 页面，统计限当前租户。图片来源筛选、旧 API 兼容和研究报告费用展示通过。八个最终运行镜像均生成 SBOM，Trivy 0.76.1 未报告高危或严重漏洞。 [镜像扫描](security.md#运行镜像-sbom-与漏洞扫描)

### `0.3.0-alpha.12` 复验

- 411 项后端测试、9 项 SDK 测试、Console 类型检查、管理冒烟和 Chromium 工作区回归通过。隔离 Compose 的八个服务健康。动态抓取与本地研究探针复验 REST／MCP、证据归档、租户隔离和单次计量。
- 真实 Uvicorn 的 REST 和 MCP 均验证直连地址、可信代理的 IPv4／IPv6、伪造或重复转发头拒绝、Console 网关无法核验地址时的拒绝，以及轮换宽限期内新旧 Key 的白名单继承。独立 PostgreSQL 验证已有 Key 迁移兼容、吊销和降级保护。 [IP 白名单部署](deployment.md#api-key-的-ip-白名单)
- Chromium 验证创建和轮换时的 IP 白名单及一次性密钥示例。账户会话列表和撤销经过真实 Cookie、Origin 和 CSRF 检查。独立 PostgreSQL 验证旧 Cookie 迁移、分页、租户与所有者隔离，以及续期与撤销并发时撤销仍能定位对应会话。 [会话契约](api-contracts.md#管理面)
- 八个实际运行镜像均生成 SBOM，Trivy 0.76.1 未报告高危或严重漏洞。公网 RFC Editor 正文探针在同版本再次收到 `FETCH_TARGET_BLOCKED`，Worker DNS 返回了非公网地址。 [镜像扫描](security.md#运行镜像-sbom-与漏洞扫描) · [公网探针](operations.md#公网正文验收)

### `0.3.0-alpha.13` 复验

- 421 项后端测试、9 项 SDK 测试与 Console 类型检查通过。隔离 Compose 的八个服务健康，动态抓取与本地研究探针通过 REST／MCP、证据归档、租户隔离和单次计量复验。Chromium 验证总览指标错误时独立状态仍可见、按角色显示审计提示、受邀账户切换及中断轮次展示。
- 分别停止并恢复隔离 Compose 的 Redis 和 PostgreSQL。Redis 停止时三个 Worker 状态为未确认，PostgreSQL 停止时 API 就绪检查返回 HTTP 503，Console 网关仍返回独立依赖状态。两项依赖恢复后八项检查均回到健康。 [服务状态](operations.md#服务状态与审计提示)
- 真实 PostgreSQL 覆盖审计风险提示的 7 天窗口、稳定排序、最小字段和双租户隔离。角色切换覆盖双向并发、目标成员停用、原 Cookie 失效、会话期限和其他浏览器隔离。 [管理面契约](api-contracts.md#管理面)
- 研究 Worker 在第二轮检索中断后，独立 PostgreSQL 分别验证末次租约结束和租约重领。两条路径保留候选、预算和中断轮次，返回部分完成并计量一次，未观测的轮次指标保持未知。
- 四租户的 20 并发 Mock Provider 基准完成 40 次冷请求和 40 次租户内缓存命中，80 条完成事件与实际调用一致。冷请求 P50 为 132.633 ms，P95 为 178.352 ms，P99 为 183.009 ms。 [基准方法](operations.md#性能与质量基线)
- 八个实际运行镜像均生成 SBOM，Trivy 0.76.1 未报告高危或严重漏洞。公网 RFC Editor 正文探针仍因 Worker DNS 返回非公网地址而收到 `FETCH_TARGET_BLOCKED`。 [镜像扫描](security.md#运行镜像-sbom-与漏洞扫描) · [公网探针](operations.md#公网正文验收)

### `0.3.0-alpha.14` 复验

- 434 项后端测试、9 项 SDK 测试、Console 类型检查及 Chromium 工作区回归通过。隔离 Compose 八个服务健康，同源与跨源动态抓取、REST／MCP、本地研究和单次计量复验通过。
- 独立真实 PostgreSQL 验证旧库迁移默认关闭、两事务版本冲突、权限隔离和最近十条设置历史。Chromium 验证差异预览、确认提交、版本冲突刷新、显式查看及脱敏审计。 [设置和查看接口](api-contracts.md#管理面)
- 真实 REST／MCP 验证同步与研究首次计量时才写原文，跨入口重放不会补采。关闭或更改策略、重新启用、到期、其他租户或未授权会话均无法获取不适用的原文。后台清理移除已到期或作废的原文，保留用量记录。真实逻辑备份恢复验证原文表数据为空，策略、审计、用量与早期归档兼容。 [备份边界](operations.md#逻辑备份与恢复演练)
- 20 并发 Mock Provider 基准完成四租户 40 次冷请求、40 次缓存命中和 80 条计量记录。冷请求 P50 为 142.573 ms，P95 为 185.1 ms，P99 为 204.501 ms。
- 八个实际运行镜像均生成 SBOM，Trivy 0.76.1 未报告高危或严重漏洞。公网 RFC Editor 正文探针在同版本仍因 Worker DNS 返回非公网地址而收到 `FETCH_TARGET_BLOCKED`。 [镜像扫描](security.md#运行镜像-sbom-与漏洞扫描) · [公网探针](operations.md#公网正文验收)

### `0.3.0-alpha.15` 复验

- 464 项后端测试、9 项 SDK 测试、Console 类型检查及 Chromium 工作区回归通过。隔离 Compose 的八个服务健康。同源与跨源动态抓取、本地研究日期边界、证据归档和 REST／MCP 单次计量复验通过。
- 受控上游限流探针经过真实 PostgreSQL 和 Redis，验证 REST 502 的 `Retry-After`、MCP 冷却期间沿用的原因，以及追踪中实际尝试 1 次和跳过 0 次的记录。Chromium 同时验证旧追踪的未知次数保持可区分。 [追踪契约](api-contracts.md#管理面)
- 用相同八条文字查询和 3.1 秒请求间隔各测 L1、L2。`0.3.0-alpha.10` 基线返回候选 4／16，`0.3.0-alpha.15` 返回候选 8／16，均为开发文档样本。通用知识两版均为 0／8。实际来源尝试由 9 次变为 16 次，因熔断跳过的来源从 23 次变为 16 次。候选版的 8 次 502 中，6 次带冷却重试提示。两轮从不同熔断状态启动，公网结果属于小样本观测。 [基准方法](operations.md#性能与质量基线)
- 四租户的 20 并发 Mock Provider 基准完成 40 次冷请求、40 次缓存命中和 80 条完成记录。冷请求 P50 为 138.544 ms，P95 为 193.481 ms，P99 为 201.392 ms。
- 八个实际运行镜像均生成 SBOM，Trivy 0.76.1 未报告高危或严重漏洞。RFC Editor 公网正文探针收到 `FETCH_TARGET_BLOCKED`，Worker DNS 返回了非公网地址。完整公网覆盖、正向公网正文和人工 Top-10 有效率待验收。 [镜像扫描](security.md#运行镜像-sbom-与漏洞扫描) · [公网探针](operations.md#公网正文验收)
