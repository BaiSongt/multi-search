# MultiSearch

[简体中文](#简体中文) · [English](#english)

## 简体中文

MultiSearch 是可独立部署的通用检索平台。当前预发布版本为 `0.3.0-alpha.12`。

REST 和 MCP 支持网页文字、新闻和图片检索。平台通过 DDGS 与 SearXNG 提供模式选择、预算、回退、融合去重和来源追踪。开发者可管理 API Key、查看用量并在 Playground 检索。管理员可查看请求与审计、配置 Provider 和租户配额。

API Key 可限制来源 IP。用户可在账户设置查看并撤销本人当前租户的其他会话。 [地址校验部署](docs/deployment.md#api-key-的-ip-白名单)

### 本地启动

需要 Docker Compose V2 和 Python 3.12。运行以下命令生成本地密钥并启动服务。

```sh
python scripts/init-secrets.py
docker compose up --build --wait
```

- API 存活与就绪分别见 `http://127.0.0.1:8765/health/live` 和 `/health/ready`。
- OpenAPI 文档位于 `http://127.0.0.1:8765/docs`。
- Console 位于 `http://127.0.0.1:3000`，首次登录前由本地部署操作员创建账户。
- MCP Streamable HTTP 位于 `http://127.0.0.1:8765/mcp`。
- 可复制 `.env.example` 为 `.env` 以调整本地端口。

### 检索与验收

启动后运行 `python scripts/smoke.py` 检查 Compose、鉴权和公网双模态结果。运行 `python scripts/smoke_management.py` 验证会话、密钥、计量和权限。 [部署](docs/deployment.md) · [REST 契约](docs/api-contracts.md) · [MCP](docs/mcp.md) · [图片归属](docs/image-attribution.md) · [验收](docs/acceptance.md)

平台支持可定位的网页和 PDF 证据、图片来源检查、可选专业与本地来源，以及可恢复的抓取和深度研究任务。`fetch` 配置启动抓取 Worker 与隔离 Crawl4AI。`research` 配置可启用多轮研究，默认使用确定性流程。 [部署](docs/deployment.md) · [运维](docs/operations.md) · [SDK](docs/sdk.md) · [安全验收](docs/security.md)

可选 `dynamic` 配置使用断网 Chromium 渲染获准 HTML。部署方可按页面授权同源或跨源的 JavaScript 和 JSON GET 资源，REST 和 MCP 抓取任务显式选择动态模式。 [动态抓取部署](docs/deployment.md#可选动态-html-渲染)

### 版本与许可证

应用采用语义化版本 2.0.0，以 `VERSION` 和 Git 标签 `v<版本号>` 跟踪交付。Python 元数据使用 PEP 440 的等价格式。发布时同步更新包版本、锁文件和变更记录，运行 `python scripts/check-version.py`，打标签后用 `--tag` 复核。提交说明使用中文。 [版本规范](https://semver.org/lang/zh-CN/) · [变更记录](CHANGELOG.md)

项目代码采用 [Apache-2.0](LICENSE)。各依赖保留其自身许可，见 [第三方声明](NOTICE)。

## English

MultiSearch is a self-hosted general-purpose search platform. The current prerelease is `0.3.0-alpha.12`.

REST and MCP support web text, news, and image search. DDGS and SearXNG supply candidates with mode selection, budgets, fallback, deduplication, and source tracking. Developers can manage API keys, see usage, and search in the Playground. Administrators can inspect requests and audit events, configure providers, and manage tenant quotas.

API keys can restrict client IP addresses. Account settings let users view and revoke their other sessions in the current tenant. [Client address configuration](docs/deployment.md#api-key-的-ip-白名单)

### Start locally

With Docker Compose V2 and Python 3.12 installed, generate local secrets and start the services.

```sh
python scripts/init-secrets.py
docker compose up --build --wait
```

- API liveness and readiness are available at `http://127.0.0.1:8765/health/live` and `/health/ready`.
- OpenAPI documentation is at `http://127.0.0.1:8765/docs`.
- The Console is at `http://127.0.0.1:3000`. A local deployment operator provisions the first accounts.
- MCP Streamable HTTP is at `http://127.0.0.1:8765/mcp`.
- Copy `.env.example` to `.env` to adjust the local ports.

### Search and verification

Run `python scripts/smoke.py` after startup to check Compose, authentication, and live text and image results. Run `python scripts/smoke_management.py` to verify sessions, API keys, metering, and permissions. [Deployment](docs/deployment.md) · [REST contract](docs/api-contracts.md) · [MCP](docs/mcp.md) · [Image attribution](docs/image-attribution.md) · [Acceptance](docs/acceptance.md)

MultiSearch supports locatable web and PDF evidence, image source checks, optional vertical and local sources, and recoverable fetch and research jobs. The Compose `fetch` profile starts the worker and isolated Crawl4AI parser. The `research` profile can enable multi-round research with deterministic execution by default. [Deployment](docs/deployment.md) · [Operations](docs/operations.md) · [SDKs](docs/sdk.md) · [Security verification](docs/security.md)

The optional `dynamic` profile renders authorized HTML in a network-isolated Chromium instance. Operators can approve exact same-origin and cross-origin JavaScript and JSON GET resources for each page. REST and MCP fetch jobs opt in explicitly. [Dynamic fetch deployment](docs/deployment.md#可选动态-html-渲染)

### Versioning and license

Releases follow Semantic Versioning 2.0.0 and are tracked in `VERSION` and Git tags named `v<version>`. Python metadata uses the equivalent PEP 440 form. Update package versions, lockfiles, and the changelog together. Run `python scripts/check-version.py` before tagging and repeat with `--tag` afterward. Commit descriptions are written in Chinese. [Versioning specification](https://semver.org/) · [Changelog](CHANGELOG.md)

MultiSearch source code uses [Apache-2.0](LICENSE). Dependencies retain their own licenses. See the [third-party notices](NOTICE).
