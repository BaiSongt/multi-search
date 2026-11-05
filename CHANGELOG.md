# 变更记录 / Changelog

## 0.2.0-alpha.1 · 2026-11-05

- WP-09 增加租户邀请、成员角色、路由版本发布与回滚、按租户和来源的缓存失效。
- WP-10 增加 L3 静态正文、可选隔离 Crawl4AI 和图片来源核查，限制抓取目标、DNS、跳转、响应体积和时间。
- WP-11 保存可定位证据和图片归属，展示来源核查状态及 Commons 报告的原图 SHA-1。
- WP-12 增加可选学术、代码和本地索引来源，以及 OCR 适配器契约。
- WP-13 增加异步抓取、持久队列、Worker 租约恢复与取消。
- WP-14 扩充 Console 的团队、任务、设置、证据详情和 CSV 导出。

## 0.1.0-alpha.2 · 2026-11-05

- WP-05 核验 Wikimedia Commons 文件页、原图匹配与许可声明，严格策略筛除未核验候选。
- WP-06 提供官方 SDK Streamable HTTP MCP，文字与图片工具共享 REST 鉴权、路由和计量。
- WP-07 增加用户会话、CSRF、API Key 生命周期、幂等请求、租户与密钥额度、用量及审计。
- WP-08 提供管理员与开发者工作区、Playground、图片来源详情及接口文档。

## 0.1.0-alpha.1 · 2026-11-05

- WP-01 建立 FastAPI、Next.js、PostgreSQL、Redis 与 SearXNG 的 Compose 工程骨架。
- WP-02 定义文字、图片与错误契约，加入 Alembic 迁移、租户成员、API Key 作用域和操作员审计。
- WP-03 接入 DDGS 与 SearXNG，规范化 Web、新闻、图片结果和图片来源信息。
- WP-04 实现检索路由、降级、融合、租户隔离缓存和限流，并提供 REST 与验收脚本。
