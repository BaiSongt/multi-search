# 首轮验收

本轮对应 WP-01 至 WP-04，完成后进入人工验收。WP-05 至 WP-08 完成后再验收阶段 I 产品。

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

## Compose 冒烟

按 [部署文档](deployment.md) 启动服务，然后运行以下命令。

```sh
python scripts/smoke.py
```

脚本验证 API 存活与就绪、Console 首页、公开能力发现、搜索权限、图片作用域、请求校验、离线边界以及吊销生效。它在两个固定的本地验收租户中签发短期测试 Key，并在结束时吊销。公开检索探测仅发送通用测试关键词。脚本使用本地回环地址，密钥不会出现在输出中。

公网探测包含 SearXNG 图片分类 JSON 请求与 MultiSearch 文字、图片检索。上游部分失败且仍返回合法候选时报告“部分成功”。某一模态零候选或上游全部失败时报告“未验收”，默认以非零状态退出。`--allow-upstream-failure` 可用于仅判断容器和本地契约是否通过。完整的可缓存公网响应可验证同租户本地请求命中，文字响应还用于验证跨租户缓存隔离。上游部分失败或无结果时，相应缓存场景显示未验收。

如已修改宿主机端口，使用回环地址提供端点。

```sh
python scripts/smoke.py --api-url http://127.0.0.1:18765 --console-url http://127.0.0.1:13000
```

## 本机验收记录

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

## 当前边界

- WP-05 扩充图片归属与许可核查。首轮只输出未知授权状态，服务端不跟随候选 URL。
- WP-06 交付 MCP Streamable HTTP 和实际客户端契约验证。
- WP-07 交付开发者 Key 生命周期 API、用量计量与完整配额。首轮通过本地 CLI 签发和吊销，平台实施作用域、有效期及基础限流。
- WP-08 交付角色工作区、登录会话与 Playground。首轮 Console 只提供状态和文档入口。
- Crawl4AI、专业来源、本地索引和 L4 研究属于后续阶段。离线或私网请求在没有获准的内部来源时显式拒绝。
- 真实引擎可用性受出口网络、限流和搜索区域影响。测试须区分 JSON 接口可达、上游返回候选、契约与来源字段合格。公网延迟和质量基准需在固定环境下单独测定。
