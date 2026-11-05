# MultiSearch

[简体中文](#简体中文) · [English](#english)

## 简体中文

MultiSearch 是可独立部署的通用检索平台。当前版本 `0.1.0-alpha.2` 对应 V2.0 规格的阶段 I。

REST 和 MCP 支持网页文字、新闻和图片检索。平台通过 DDGS 与 SearXNG 提供模式选择、预算、回退、融合去重和来源追踪。开发者可管理 API Key、查看用量并在 Playground 检索。管理员可查看请求与审计、配置 Provider 和租户配额。

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

图片许可默认显示未知，支持选择性核验 Wikimedia Commons 的许可声明。网页证据提取、专业数据源、持久任务和深度研究按后续阶段推进。

### 版本与许可证

应用采用语义化版本 2.0.0，以 `VERSION` 和 Git 标签 `v<版本号>` 跟踪交付。Python 元数据使用 PEP 440 的等价格式。发布时同步更新包版本、锁文件和变更记录，运行 `python scripts/check-version.py`，打标签后用 `--tag` 复核。提交说明使用中文。 [版本规范](https://semver.org/lang/zh-CN/) · [变更记录](CHANGELOG.md)

项目代码采用 [Apache-2.0](LICENSE)。各依赖保留其自身许可，见 [第三方声明](NOTICE)。

## English

MultiSearch is a self-hosted general-purpose search platform. Version `0.1.0-alpha.2` corresponds to Phase I of the V2.0 specification.

REST and MCP support web text, news, and image search. DDGS and SearXNG supply candidates with mode selection, budgets, fallback, deduplication, and source tracking. Developers can manage API keys, see usage, and search in the Playground. Administrators can inspect requests and audit events, configure providers, and manage tenant quotas.

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

Image rights default to unknown, with optional verification of Wikimedia Commons license declarations. Web evidence extraction, vertical sources, durable jobs, and deep research follow in later phases.

### Versioning and license

Releases follow Semantic Versioning 2.0.0 and are tracked in `VERSION` and Git tags named `v<version>`. Python metadata uses the equivalent PEP 440 form. Update package versions, lockfiles, and the changelog together. Run `python scripts/check-version.py` before tagging and repeat with `--tag` afterward. Commit descriptions are written in Chinese. [Versioning specification](https://semver.org/) · [Changelog](CHANGELOG.md)

MultiSearch source code uses [Apache-2.0](LICENSE). Dependencies retain their own licenses. See the [third-party notices](NOTICE).
