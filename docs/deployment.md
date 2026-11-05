# 本地部署

本指南对应 V2.0 规格的 WP-01 至 WP-04 首轮验收。需要支持 Linux 容器的 Docker Compose V2，以及用于生成密钥与执行冒烟脚本的 Python 3.12。在仓库根目录运行以下命令。

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

可以从 `.env.example` 创建 `.env`，调整 `MULTISEARCH_API_PORT` 或 `MULTISEARCH_CONSOLE_PORT`。调整 API 端口时同步修改 `MULTISEARCH_PUBLIC_API_BASE_URL`，让 Console 的 OpenAPI 链接指向实际地址。`MULTISEARCH_ENABLE_DDGS=false` 可禁用 DDGS。`MULTISEARCH_ENABLE_IMAGE_SEARCH=false` 可关闭图片搜索。`MULTISEARCH_MAX_CONCURRENT_REQUESTS` 可将单 API 进程的检索并发上限设为 1 至 20。

首次启动等待数据库与缓存健康并应用 Alembic 迁移。应用运行以固定版本的镜像与锁定依赖为基础。`/health/live` 检查 API 进程，`/health/ready` 检查 PostgreSQL 和 Redis。公网搜索能力以实际 Provider 调用与 [验收结果](acceptance.md) 为准。

```sh
docker compose logs --tail=100 multisearch-api multisearch-console searxng
docker compose down
```

`docker compose down` 停止服务并保留命名卷。开发部署只通过回环地址发布 API 和 Console。PostgreSQL、Redis 和 SearXNG 的端口保留在 Compose 网络内。

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
