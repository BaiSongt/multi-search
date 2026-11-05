# MultiSearch

[简体中文](#简体中文) · [English](#english)

## 简体中文

MultiSearch 是可独立部署的通用检索平台。当前版本 `0.1.0-alpha.1` 交付 V2.0 规格的首轮 WP-01 至 WP-04。

REST API 支持 Web 文字与图片、新闻文字，以及租户内缓存查询。平台通过 DDGS 和 SearXNG 检索，提供模式选择、预算、回退、融合去重和来源追踪。API Key 由本地操作员签发，按租户、作用域和有效期限制访问。

### 本地启动

需要 Docker Compose V2 和 Python 3.12。运行以下命令生成本地密钥并启动服务。

```sh
python scripts/init-secrets.py
docker compose up --build --wait
```

- API 存活与就绪分别见 `http://127.0.0.1:8765/health/live` 和 `/health/ready`。
- OpenAPI 文档位于 `http://127.0.0.1:8765/docs`。
- Console 开发预览位于 `http://127.0.0.1:3000`。
- 可复制 `.env.example` 为 `.env` 以调整本地端口。

### 检索与验收

参阅 [部署和密钥签发](docs/deployment.md)、[REST 契约与请求示例](docs/api-contracts.md)和[图片归属字段](docs/image-attribution.md)。启动后运行 `python scripts/smoke.py` 检查 Compose、鉴权和真实文字与图片结果。脱网测试和本轮验收边界见 [验收文档](docs/acceptance.md)。

WP-05 至 WP-08 是阶段 I 的后续交付，涵盖图片归属与许可核验、MCP、完整密钥与用量管理，以及 [管理员和开发者工作区](docs/ui-design.md)。当前图片许可标记为 `unknown`。

### 版本与许可证

应用采用语义化版本 2.0.0，以 `VERSION` 和 Git 标签 `v<版本号>` 跟踪交付。Python 元数据使用 PEP 440 的等价格式。发布时同步更新包版本、锁文件和变更记录，运行 `python scripts/check-version.py`，打标签后用 `--tag` 复核。提交说明使用中文。 [版本规范](https://semver.org/lang/zh-CN/) · [变更记录](CHANGELOG.md)

项目代码采用 [Apache-2.0](LICENSE)。各依赖保留其自身许可，见 [第三方声明](NOTICE)。

## English

MultiSearch is a self-hosted general-purpose search platform. Version `0.1.0-alpha.1` covers WP-01 through WP-04 of the V2.0 specification.

The REST API supports web text, web images, news text, and tenant-scoped cache lookups. DDGS and SearXNG supply candidates with mode selection, budgets, fallback, deduplication, and source tracking. Local operators issue API keys that are restricted by tenant, scope, and expiration.

### Start locally

With Docker Compose V2 and Python 3.12 installed, generate local secrets and start the services.

```sh
python scripts/init-secrets.py
docker compose up --build --wait
```

- API liveness and readiness are available at `http://127.0.0.1:8765/health/live` and `/health/ready`.
- OpenAPI documentation is at `http://127.0.0.1:8765/docs`.
- The Console preview is at `http://127.0.0.1:3000`.
- Copy `.env.example` to `.env` to adjust the local ports.

### Search and verification

See the [deployment and key issuance guide](docs/deployment.md), [REST contract and request example](docs/api-contracts.md), and [image attribution fields](docs/image-attribution.md). Run `python scripts/smoke.py` after startup to check Compose, authentication, and live text and image results. The [acceptance guide](docs/acceptance.md) covers offline tests and milestone boundaries.

WP-05 through WP-08 cover deeper image attribution and license verification, MCP, full key and usage management, and the [administrator and developer workspaces](docs/ui-design.md). Image rights are currently reported as `unknown`.

### Versioning and license

Releases follow Semantic Versioning 2.0.0 and are tracked in `VERSION` and Git tags named `v<version>`. Python metadata uses the equivalent PEP 440 form. Update package versions, lockfiles, and the changelog together. Run `python scripts/check-version.py` before tagging and repeat with `--tag` afterward. Commit descriptions are written in Chinese. [Versioning specification](https://semver.org/) · [Changelog](CHANGELOG.md)

MultiSearch source code uses [Apache-2.0](LICENSE). Dependencies retain their own licenses. See the [third-party notices](NOTICE).
