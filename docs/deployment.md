# 本地部署

需要支持 Linux 容器的 Docker Compose V2，以及用于生成密钥与执行冒烟脚本的 Python 3.12。在仓库根目录运行以下命令。

```sh
python scripts/init-secrets.py
docker compose config --quiet
docker compose up --build --wait
docker compose ps
```

初始化脚本将随机密钥保存至 Git 忽略的 `.secrets/` 目录。再次运行保留已有密钥。请保管该目录及数据卷的备份。PostgreSQL 仅在初始化空卷时使用密码文件设置密码，替换文件本身不会轮换现有角色的密码。API Key 哈希依赖 pepper，更换 pepper 会使已有 API Key 失效。

| 服务 | 访问地址或网络 | 持久化 |
| --- | --- | --- |
| FastAPI | 宿主机 `http://127.0.0.1:8765` | 依赖 PostgreSQL 与 Redis |
| Next.js Console | 宿主机 `http://127.0.0.1:3000` | 无 |
| SearXNG | 搜索网络 `http://searxng:8080` | 挂载只读设置 |
| PostgreSQL | 内部数据网络 `postgres:5432` | 命名卷 `postgres_data` |
| Redis | 内部数据网络 `redis:6379` | 命名卷 `redis_data`，启用 AOF |

可以从 `.env.example` 创建 `.env`，调整 `MULTISEARCH_API_PORT` 或 `MULTISEARCH_CONSOLE_PORT`。调整端口时同步更新 `MULTISEARCH_PUBLIC_API_BASE_URL` 和 `MULTISEARCH_PUBLIC_CONSOLE_URL`，分别用于文档链接和会话来源校验。`MULTISEARCH_ENABLE_DDGS=false` 可禁用 DDGS。`MULTISEARCH_ENABLE_IMAGE_SEARCH=false` 可关闭图片搜索。`MULTISEARCH_MAX_CONCURRENT_REQUESTS` 可将单 API 进程的检索并发上限设为 1 至 20。

首次启动等待数据库与缓存健康并应用 Alembic 迁移。应用运行以固定版本的镜像与锁定依赖为基础。`/health/live` 检查 API 进程，`/health/ready` 检查 PostgreSQL 和 Redis。公网搜索能力以实际 Provider 调用与 [验收结果](acceptance.md) 为准。

```sh
docker compose logs --tail=100 multisearch-api multisearch-console searxng
docker compose down
```

`docker compose down` 停止服务并保留命名卷。开发部署只通过回环地址发布 API 和 Console。PostgreSQL、Redis 和 SearXNG 的端口保留在 Compose 网络内。

## 创建登录账户

部署操作员在本地终端创建首位平台管理员和开发者。命令在终端隐藏输入密码。完成后通过 `/auth` 登录，输入对应租户、邮件地址和密码。

```sh
docker compose exec multisearch-api python -m multisearch.keys user-add --tenant platform --email admin@example.invalid --role platform_admin
docker compose exec multisearch-api python -m multisearch.keys user-add --tenant demo --email developer@example.invalid --role developer
```

HTTP 字面回环地址使用本地会话 Cookie。HTTPS Console 地址启用 Secure 标志。管理操作经同源 Console 代理转发，由 API 检查会话、角色、Origin 与 CSRF。

## 签发测试凭据

在拥有该部署 Docker 访问权限的本地终端执行操作员 CLI。以下命令签发一小时有效的双模态搜索 Key。

```sh
docker compose exec -T multisearch-api python -m multisearch.keys issue --tenant demo --email developer@example.invalid --scope search:text --scope search:image --ttl-hours 1
```

生成的明文只由 CLI 输出一次。请通过密码管理器或当前会话变量保存。Key 中 `msk_` 与点号之间的 32 位十六进制值是吊销用标识。将下例中的 `KEY_ID` 替换为该标识。

```sh
docker compose exec -T multisearch-api python -m multisearch.keys revoke --id KEY_ID
```

## SearXNG 上游

配置文件启用 JSON 格式，保留 Web、新闻和图片类别所需的候选引擎，关闭 SearXNG 图片代理。每个引擎的实际覆盖与可用性受网络环境及上游限制。冒烟脚本会从 API 容器测试 `categories=images&format=json`，分别记录候选数与上游未响应数。[SearXNG Search API](https://docs.searxng.org/dev/search_api.html)

## 部署策略基线

Compose 将 `config.example.yaml` 以只读方式挂载到 API 和 Worker。自定义时将其复制为 `config.local.yaml`，编辑后在 `.env` 中设置 `MULTISEARCH_POLICY_PATH=./config.local.yaml`，再重新创建两个服务。独立启动 Python 服务时可用 `MULTISEARCH_POLICY_FILE` 指定同一文件。文件支持模式预算、自动快速模式阈值、快速回退、缓存时限、来源优先级／限流及图片过滤默认值。配置应与发布记录一起保存。 [配置示例](../config.example.yaml)

管理台发布的路由版本和已保存的来源策略优先。路由回滚到版本 0 使用当前部署基线。调用方可逐项覆盖图片默认值，已受理的研究任务保存其有效图片选项。策略文件只接受受支持的字段，凭据继续通过专用 Secret 配置。

## 可选专业来源

可在 `.env` 中设置 `MULTISEARCH_VERTICAL_SOURCES=crossref,arxiv` 选择来源，或设置 `MULTISEARCH_ENABLE_ACADEMIC=true` 启用 OpenAlex、Crossref 和 arXiv。学术请求使用 `source_types=["academic"]`。OpenAlex 匿名查询使用上游较低的额度，可配置 `MULTISEARCH_OPENALEX_KEY`。如需使用 Crossref 的礼貌访问池，可提供 `MULTISEARCH_CROSSREF_EMAIL`。

学术查询可以使用完整 DOI、DOI 链接或 `doi:` 前缀，也可以使用 arXiv ID、arXiv 摘要／PDF 链接或 `arXiv:` 前缀。带 `vN` 的 arXiv ID 要求上游返回同一版本。日期与域名筛选仍适用。 [Crossref 接口](https://api.crossref.org/swagger-ui/index.html) · [OpenAlex 接口](https://help.openalex.org/api/get-single-entities/) · [arXiv API 手册](https://info.arxiv.org/help/api/user-manual.html)

GitHub 代码检索通过 `MULTISEARCH_VERTICAL_SOURCES=github` 和 `MULTISEARCH_GITHUB_TOKEN` 启用，请求使用 `source_types=["code"]`。结果只包含公开仓库，默认分支与提交 SHA 分开理解。部署平台可通过对应的 `_FILE` 变量注入密钥文件。未启用的来源由能力端点显式标记为不可用。

## 本地索引和离线查询

设置 `MULTISEARCH_ENABLE_LOCAL_INDEX=true` 并重建 API。操作员将批准的纯文本资料导入指定租户。每行至少包含 `title` 与 `text`，可选 `id`、`source_url`、`language` 和 `published_at`。相同租户和 ID 的导入更新原记录。每批最多 500 条，每租户最多 2000 条。

```sh
docker compose exec -T multisearch-api python -m multisearch.local_index import --tenant demo < approved-documents.jsonl
```

PowerShell 使用 `Get-Content -Raw -Encoding UTF8 approved-documents.jsonl | docker compose exec -T multisearch-api python -m multisearch.local_index import --tenant demo`。检索设置 `source_types=["local"]` 和 `network_scope="offline"`。本地来源必须单独选择，关键词与正文留在当前租户。

## 抓取 Worker 和 Crawl4AI

首次升级先运行 `python scripts/init-secrets.py`，为隔离解析器补充专用令牌。已有密钥文件会保留。

```sh
docker compose --profile fetch up -d --build --wait
docker compose --profile fetch ps
```

Worker 通过 PostgreSQL 持久化队列和租约恢复，Redis 提供就绪心跳。Crawl4AI 只在独立内部网络接受 Worker 传入的已验证 HTML，使用令牌鉴权。公开 URL 由 SafeFetcher 逐跳检查并固定目标 IP，遵守 robots 和摘录限制。解析器不接收 URL，且无需浏览器或公网访问。API 的同步 L3 默认使用静态提取。如需让同步 L3 使用隔离解析器，在启用 `fetch` 配置时设置 `CRAWL4AI_BASE_URL=http://crawl4ai:11235`。

### 可选动态 HTML 渲染

动态模式用于公开 HTML 内嵌脚本生成正文的场景。部署方先确认站点允许抓取和保留摘录，再在 `.env` 将 `MULTISEARCH_DYNAMIC_FETCH_DOMAINS` 设为逗号分隔的精确域名。每个允许的重定向域名也需单独列入。更新所有 Worker 后运行密钥初始化命令生成专用渲染凭据，再启用覆盖文件。

```sh
python scripts/init-secrets.py
docker compose -f compose.yml -f compose.dynamic.yml --profile dynamic up -d --build --wait
```

Worker 通过 Unix socket 向断网 Chromium 容器传递已授权的 HTML。浏览器仅加载该响应，阻断其他资源请求。来源 CSP、原始和渲染后的 robots 摘录限制继续生效。动态镜像面向 Linux x86-64，构建使用 Docker BuildKit。宿主 Linux 需允许非特权用户命名空间，渲染器在启动时确认 Chromium 的命名空间和 Seccomp-BPF 沙箱。渲染器健康检查失败时，动态能力保持不可用。静态 L3 与动态 `fetch` 分别通过各自入口使用。 [抓取接口](api-contracts.md#证据与任务)

PDF 需要匹配 MIME 和文件签名，输入上限为 1 MiB。每份检查前 20 页，单页规范化文本至多 16,000 字符，总计至多 64,000 字符。解析进程的墙钟时限为 3 秒，Linux 容器还限制 CPU 时间和地址空间。当前镜像提取 PDF 文本层，将需要 OCR 的图像型 PDF 标记为待处理。部署方可通过 OCR 契约接入获准的引擎。

## 深度研究与指标

设置 `MULTISEARCH_ENABLE_DEEP_RESEARCH=true`，启动研究配置。默认研究采用确定性分解、检索和证据汇总，无需模型凭据。为机器客户端签发 `research` 和相应的文字或图片搜索作用域。

```sh
python scripts/init-secrets.py
docker compose --profile research up -d --build --wait
```

需要模型综合时，部署操作员配置 `MULTISEARCH_RESEARCH_MODEL_BACKEND=openai` 或 `ollama`，以及 `MULTISEARCH_RESEARCH_MODEL_BASE_URL`、`MULTISEARCH_RESEARCH_MODEL_NAME` 和所需凭据。请求需显式设置正数 `max_llm_tokens`。OpenAI 兼容服务需要支持 Responses 和输入 token 计数接口。离线本地研究不调用模型。

如需费用估算，同时设置 `MULTISEARCH_RESEARCH_COST_CURRENCY`、`MULTISEARCH_RESEARCH_INPUT_PER_MILLION_TOKENS` 和 `MULTISEARCH_RESEARCH_OUTPUT_PER_MILLION_TOKENS`。费率由部署操作员按实际模型和计价币种提供。

Compose 从单独的指标密钥文件保护 `/metrics`。指标、脱敏追踪、备份及演练命令见运维文档。 [运维验证](operations.md)
