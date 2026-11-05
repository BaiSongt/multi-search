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

## 可选专业来源

可在 `.env` 中设置 `MULTISEARCH_VERTICAL_SOURCES=crossref,arxiv` 选择来源，或设置 `MULTISEARCH_ENABLE_ACADEMIC=true` 启用 OpenAlex、Crossref 和 arXiv。学术请求使用 `source_types=["academic"]`。OpenAlex 匿名查询使用上游较低的额度，可配置 `MULTISEARCH_OPENALEX_KEY`。如需使用 Crossref 的礼貌访问池，可提供 `MULTISEARCH_CROSSREF_EMAIL`。

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
