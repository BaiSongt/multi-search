# MultiSearch

[简体中文](#简体中文) · [English](#english)

## 简体中文

MultiSearch 是可独立部署的通用检索平台。当前版本 `0.1.0-alpha.1` 对应 V2.0 规格的首轮开发验收。

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

### 版本与许可证

应用采用 [语义化版本 2.0.0](https://semver.org/lang/zh-CN/)，以 `VERSION` 和 Git 标签 `v<版本号>` 跟踪交付。Python 元数据使用 PEP 440 的等价格式。每次发布同步更新 Console 的包版本、锁文件和 [变更记录](CHANGELOG.md)。提交说明使用中文。

项目代码采用 [Apache-2.0](LICENSE)。各依赖保留其自身许可，见 [第三方声明](NOTICE)。

## English

MultiSearch is a self-hosted general-purpose search platform. Version `0.1.0-alpha.1` is the first development milestone under the V2.0 specification.

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

### Versioning and license

Releases follow [Semantic Versioning 2.0.0](https://semver.org/) and are tracked in `VERSION` and Git tags named `v<version>`. Python metadata uses the equivalent PEP 440 form. Update the Console package version, lockfiles and [changelog](CHANGELOG.md) together for each release. Commit descriptions are written in Chinese.

MultiSearch source code uses [Apache-2.0](LICENSE). Dependencies retain their own licenses. See the [third-party notices](NOTICE).
