# 安全与依赖验收

## 生产部署检查

- [ ] 保持 PostgreSQL、Redis、SearXNG、Worker 与解析器的服务端口位于受控网络。Compose 默认只将 API 和 Console 发布到宿主机回环地址。对外服务使用 HTTPS 反向代理，设置准确的 `MULTISEARCH_PUBLIC_CONSOLE_URL` 和 `MULTISEARCH_PUBLIC_API_BASE_URL`，验证同源登录、会话 Cookie 的 Secure 属性、CSRF 与反向代理的访问日志脱敏。 [部署说明](deployment.md)
- [ ] 使用仓库的初始化脚本生成独立的数据库、Key 哈希、SearXNG、解析器与指标密钥。限制密钥文件和备份的读取权限，定期演练数据库密码、指标令牌及机器 Key 的轮换。更换 API Key 哈希 pepper 会让已签发 Key 失效，恢复时应从独立的密钥备份中取得对应版本。 [密钥初始化](../scripts/init-secrets.py)
- [ ] 首位管理员由本地运维终端创建。人机接口使用分离的凭据。管理员和开发者通过 Console 会话完成管理，服务间调用使用限定租户、作用域、配额和有效期的 Key。检查 Key 签发、轮换、吊销、会话注销和跨租户访问的审计事件。限制生产宿主机及 Docker 运维权限，因本地 CLI 可执行紧急账户操作。 [账户与 Key](deployment.md#创建登录账户)
- [ ] 对 API 与 Worker 的网络出口实施部署级控制。SafeFetcher 对 URL、DNS 结果及每次重定向执行地址检查，连接锁定至已解析的公开 IP，Crawl4AI 仅从隔离网络接收经验证的 HTML。按业务复核域名策略、robots、访问频率与内容授权。阻止来自不可信网络的代理转发或直达解析器。 [抓取部署](deployment.md#抓取-worker-和-crawl4ai)
- [ ] 以部署实际配置复验租户边界、离线模式、任务恢复与负载。在启用公网 Provider 时，查询词可能发送至该 Provider。在配置模型并显式分配 token 预算时，问题和精选证据可能发送至指定模型服务。按数据分类决定可用来源和模型出口。 [深度研究部署](deployment.md#深度研究与指标)
- [ ] 保护 `/metrics` 与 PostgreSQL 备份。为指标采集器使用独立凭据，限制采集端的网络可达范围。按本组织保留策略处理用量元数据、任务结果、证据和归档，并验证删除和恢复流程。应用内的幂等响应保留窗口为 24 小时，用量元数据当前未配置自动过期。 [运维验证](operations.md)
- [ ] MCP 默认以回环传输和作用域 Key 验证。对互联网开放前，应完成 OAuth 2.1、授权发现、Origin 与 Host 校验以及独立客户端兼容性验收。当前实现提供基于 Key 的 MCP 传输，公网授权设施待部署方接入和验证。 [MCP 接入](mcp.md)
- [ ] 发布目标镜像前，核查运行镜像实际安装的 OS 与应用包、漏洞公告和镜像摘要，并复验隔离、权限及告警。Compose 的 API、Worker、Console 和解析器使用非 root 用户、只读根文件系统和降权设置。保存部署对应的依赖清单、漏洞扫描结果与许可证记录。 [Compose 配置](../compose.yml)

## 机器凭据

使用部署操作员终端签发用途单一的 Key。示例为已有的 `demo` 租户开发者签发一小时有效的文字搜索 Key。

```sh
docker compose exec -T multisearch-api python -m multisearch.keys issue --tenant demo --email developer@example.invalid --scope search:text --ttl-hours 1
```

签发输出包含仅展示一次的明文，应直接保存在秘密存储中。SDK 和本地 MCP 示例从服务端进程的 `MULTISEARCH_KEY` 读取凭据。文字研究同时需要 `research` 和 `search:text`，图片研究需要 `research` 和 `search:image`。抓取使用 `fetch`。轮换时先更新调用方，完成验证后吊销旧 Key。审计与支持请求通过 `X-Request-ID` 和 `X-Trace-ID` 关联。 [SDK 示例](sdk.md) · [MCP 工具](mcp.md)

## 内容与许可证

证据接口保留来源、必要摘录与定位信息。部署方应按数据源许可、robots 和组织保留规则设置来源。使用图片前应复查原始落地页和授权状态，尊重权利人条件。项目采用 Apache-2.0，仓库中的第三方通知列明主要直接依赖和独立服务的上游许可。分发镜像或修改版时，应按各组件的实际许可履行适用义务。 [证据与图片归属](image-attribution.md) · [LICENSE](../LICENSE) · [NOTICE](../NOTICE)

## 依赖清单和扫描记录

以下命令生成 CycloneDX 1.6 JSON。清单涵盖 API、独立解析器和 Console 锁文件的 Python 与 npm 版本，以及 Compose 和 Dockerfile 的固定镜像摘要。元数据标记为构建前清单，包含锁文件的开发项和条件依赖。生产交付还需按实际镜像层生成 SBOM，并验证所有传递依赖的许可证与运行环境漏洞。 [CycloneDX 1.6](https://cyclonedx.org/docs/1.6/json/)

```sh
python scripts/ops/dependency_inventory.py > multisearch-dependencies.cdx.json
```

2026 年 11 月 6 日检查记录（UTC+8）

| 数据源 | 检查范围 | 结果 |
| --- | --- | --- |
| OSV `/v1/querybatch` | 两份 `uv.lock` 中 122 个去重后的 PyPI 包与版本（API 55 个，解析器 98 个） | 0 个返回漏洞公告的版本 |
| npm 官方注册表 `npm audit` | Console 锁文件，含开发项和可选项，审计元数据计 63 个依赖 | 0 个漏洞 |

可在 `console/` 重新运行 npm 审计。OSV 使用两份锁文件中来自注册表的包名和版本，以 `PyPI` 生态提交批量版本查询。公告数据库和依赖版本均可能变化，发布前应重新扫描。 [npm audit](https://docs.npmjs.com/cli/commands/npm-audit) · [OSV 批量接口](https://google.github.io/osv.dev/post-v1-querybatch/)

```sh
npm audit --package-lock-only --ignore-scripts --registry=https://registry.npmjs.org
```

### 运行镜像 SBOM 与漏洞扫描

在目标 Compose 项目启动后，安装经过校验的 Trivy，然后在仓库根目录运行以下命令。按实际启动配置指定 `--profile`，需要时用 `--project` 指定 Compose 项目，用 `--trivy` 指定可执行文件。每次扫描使用新的输出目录。 [Trivy 安装](https://trivy.dev/docs/latest/getting-started/installation/) · [镜像扫描参数](https://trivy.dev/docs/latest/guide/references/configuration/cli/trivy_image/)

```sh
python scripts/ops/image_security.py --profile research --output .secrets/ops/image-security-2026-11-06 --fail-on-high
```

脚本根据运行容器的镜像 ID 读取本地镜像，要求所选 Compose 配置的服务均在运行。它记录 Trivy 和漏洞数据库版本，并以同一个数据库快照生成每个镜像的原始漏洞 JSON、CycloneDX SBOM 和 `summary.json`。发现高危或严重记录时，`--fail-on-high` 保留报告并以状态码 2 退出。状态码 1 表示扫描未完成。报告按镜像组件与漏洞组合计数，供部署方结合上游公告评估可达性。原始报告包含镜像元数据，应存放在受控目录。 [报告格式](https://www.trivy.dev/docs/latest/guide/configuration/reporting/)

2026 年 11 月 6 日对 `0.3.0-alpha.2` 的七个 `linux/amd64` 运行镜像完成扫描。Trivy 0.76.1 使用 2026-11-05 13:09 UTC 更新的漏洞库，并生成 CycloneDX 1.7。复扫中，三个 Python 镜像先前由构建工具 uv 触发的严重记录和 Console 包管理器触发的 10 条高危 npm 记录均已消失。Python 构建阶段使用包含修复的 uv 0.13.4，运行阶段仅复制锁定依赖的虚拟环境。Console 从运行文件系统中移除了 npm、Corepack 和 Yarn。 [uv 安全公告](https://github.com/astral-sh/uv/security/advisories/GHSA-hq85-5cpj-mr62)

| 服务 | 镜像 ID 前 12 位 | SBOM 组件 | 严重 | 高危 | 报告列有修复版本（严重／高危） |
| --- | --- | ---: | ---: | ---: | ---: |
| API | `8d91a65bafb4` | 134 | 0 | 46 | 0／0 |
| Worker | `5c3eadcaa54c` | 134 | 0 | 46 | 0／0 |
| Crawl4AI | `662376f91929` | 187 | 0 | 46 | 0／0 |
| Console | `3fbf90dcafc5` | 110 | 1 | 33 | 0／0 |
| PostgreSQL | `266d3c257594` | 51 | 1 | 25 | 1／25 |
| Redis | `f4ed7d6732f9` | 23 | 0 | 0 | 0／0 |
| SearXNG | `892ed5710603` | 33 | 0 | 0 | 0／0 |

本轮未在识别到的 Python 或 npm 包中发现严重或高危记录。API、Worker、Crawl4AI 和 Console 的剩余高危记录来自 Debian 软件包，当前报告未列修复版本。Console 的 `CVE-2023-45853` 是 Debian 12 `zlib1g` 的版本匹配。Debian 说明该漏洞位于旧版本 `zlib` 源包未构建的 MiniZip 代码，本机 Console 镜像也未安装 `libminizip1`。原始记录保留。 [Debian CVE 记录](https://security-tracker.debian.org/tracker/CVE-2023-45853)

PostgreSQL 的严重和高危记录来自 `gosu` 嵌入的 Go 标准库，表中的修复版本指 Go 版本。对该镜像内的 `gosu` 运行 `govulncheck -mode binary`，未匹配到严重项 `CVE-2025-68121` 的 TLS 易受攻击符号，另检出 `moby/sys/user` 的中危 `CVE-2026-61801`，其利用前提是输入可被攻击者控制的用户或组数据库文件。本 Compose 配置的 PostgreSQL 根文件系统为只读，且不挂载替代的 `/etc/passwd` 或 `/etc/group`。原始 Trivy 高危门禁仍返回 2。 [Go CVE 记录](https://pkg.go.dev/vuln/GO-2026-4337) · [gosu 复核方法](https://github.com/tianon/gosu/security) · [Moby 安全公告](https://github.com/moby/sys/security/advisories/GHSA-mjcv-p78q-w5fw)
