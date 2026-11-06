# MCP Streamable HTTP

默认端点为 `http://127.0.0.1:8765/mcp`。客户端需要配置 `Authorization: Bearer <API_KEY>` 请求头。服务在每次传输请求验证 Key，使用与 REST 相同的作用域、租户配额、缓存、检索预算与图片结果模型。

| 工具 | 参数 | 权限 |
| --- | --- | --- |
| `multi_search` | 与 REST SearchRequest 一致，默认文字模态 | 按请求模态检查 `search:text`、`search:image`，深度模式另需 `research` |
| `image_search` | 与 SearchRequest 一致，省略 `modalities`，固定为图片 | `search:image`，深度模式另需 `research` |
| `web_fetch` | 与 REST FetchRequest 一致，使用 `url` 或 `urls` 提交异步正文提取 | `fetch` |
| `start_deep_research` | 与 REST SearchRequest 一致，`mode` 默认并固定为 `deep` | `research` 与请求模态的搜索作用域 |
| `get_search_job` | 使用 UUID 格式的 `job_id` 查询任务 | 与任务类型匹配的 `fetch` 或 `research` |
| `cancel_search_job` | 使用 UUID 格式的 `job_id` 取消任务 | 与任务类型匹配的 `fetch` 或 `research` |
| `get_result_page` | 使用 `result_ref.result_id` 和从 0 开始的整数 `page` | 生成引用的租户、API Key 及作用域 |

同步搜索返回 REST `SearchResponse`。`web_fetch`、`start_deep_research` 和设置 `mode=deep` 的搜索返回 `JobAccepted`，其中包含 `job_id`、预算与 REST 状态地址。任务查询与取消返回 `JobView`。配置任务服务后可发现四个任务工具，提交任务需要相应 Worker 就绪。每个任务的查询和取消都执行与 REST 相同的租户与作用域校验。

工具执行失败时返回 `isError` 与结构化错误对象。同步搜索与搜索预览提供 `result_status`，区分 `has_results`、`no_results`、`partial_results` 与 `incomplete_no_results`。有已知的未恢复来源错误时，在搜索响应、搜索预览和工具错误对象中查看 `provider_reason_codes`（例如 `upstream_rate_limited`）。客户端也应展示 `warnings` 与图片 `rights_status`。搜索候选不代表获得作品授权。 [结果与错误](api-contracts.md#结果与错误)

## 长结果取回

当同步搜索或任务查询的结果超过 16 KiB 工具响应预算时，`structuredContent` 与 `TextContent` 都返回相同的 `delivery: "deferred"` 对象。它包含状态、请求／任务标识、最多三项候选预览和 `result_ref`。通过 `fetch_tool` 指定的工具逐页取回后可恢复原始 `SearchResponse` 或 `JobView` JSON。长图片响应的完整原图、预览、来源页及许可状态随分页交付。取消任务的长响应会将 `JobView.result` 置为 `null`，调用方可按原 `job_id` 查询任务。

每页 `data_base64` 可独立解码。依 `page` 顺序拼接字节后再按 UTF-8 解析，`next_page=null` 表示末页。每页最多 4,500 个原始字节，单个完整结果最多 4 MiB。引用最长有效 300 秒，同一有效结果被重新返回时沿用剩余时限。过期、未知及其他身份的引用均返回 `MCP_RESULT_NOT_FOUND`。分页限额为每 Key 每分钟 240 次、每租户每分钟 480 次，读取沿用现有认证且不重复计量搜索。超限时可读取 `retry_after_ms`。

临时交付使用独立的 Redis 键空间，与检索缓存分开。`cache_policy=bypass` 跳过检索缓存，长结果仍会进行短时交付。默认 Compose 的 Redis 启用 AOF，应按运行数据的访问和备份策略管理。 [部署安全](security.md)

```python
import base64
import json


async def full_tool_payload(client, response):
    if response.is_error:
        raise RuntimeError(response.structured_content["code"])
    data = response.structured_content
    if data.get("delivery") != "deferred":
        return data
    ref = data["result_ref"]
    chunks = []
    for page in range(ref["page_count"]):
        result = await client.call_tool(ref["fetch_tool"], {"result_id": ref["result_id"], "page": page})
        if result.is_error:
            raise RuntimeError(result.structured_content["code"])
        part = result.structured_content
        if part["result_id"] != ref["result_id"] or part["page"] != page:
            raise ValueError("MCP 返回了不匹配的结果页")
        chunks.append(base64.b64decode(part["data_base64"], validate=True))
    payload = b"".join(chunks)
    if len(payload) != ref["payload_bytes"]:
        raise ValueError("MCP 结果长度不匹配")
    return json.loads(payload)
```

使用锁定的官方 Python SDK 与本地部署验证连接。先在终端设置 `MULTISEARCH_KEY`，然后在后端 Python 环境运行以下代码。

```python
import asyncio
import os

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client


async def main():
    headers = {"Authorization": "Bearer " + os.environ["MULTISEARCH_KEY"]}
    async with httpx2.AsyncClient(headers=headers, trust_env=False) as http:
        transport = streamable_http_client("http://127.0.0.1:8765/mcp", http_client=http)
        async with Client(transport) as client:
            result = await client.call_tool("image_search", {"query": "Kingfisher", "max_results": 5})
            print(result.structured_content)


asyncio.run(main())
```

已连接的客户端可提交研究任务，再按任务标识查询。以下示例使用具有 `research` 和 `search:text` 作用域的 Key。

```python
queued = await client.call_tool(
    "start_deep_research",
    {"query": "如何复核一项公开技术结论", "max_search_rounds": 2, "max_pages": 5},
)
if queued.is_error:
    raise RuntimeError(queued.structured_content["code"])

job_ref = {"job_id": queued.structured_content["job_id"]}
current = await full_tool_payload(client, await client.call_tool("get_search_job", job_ref))
print(current["status"])
```

默认只接受回环 Host 和 Origin。公开部署的 OAuth 2.1 与授权发现属于生产部署验收。 [官方 MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.3.0)
