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

同步搜索返回 REST `SearchResponse`。`web_fetch`、`start_deep_research` 和设置 `mode=deep` 的搜索返回 `JobAccepted`，其中包含 `job_id`、预算与 REST 状态地址。任务查询与取消返回 `JobView`。配置任务服务后可发现四个任务工具，提交任务需要相应 Worker 就绪。每个任务的查询和取消都执行与 REST 相同的租户与作用域校验。

工具执行失败时返回 `isError` 与结构化错误对象。客户端应明确展示 `partial`、`warnings` 与图片 `rights_status`。搜索候选不代表获得作品授权。

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
current = await client.call_tool("get_search_job", job_ref)
if current.is_error:
    raise RuntimeError(current.structured_content["code"])
print(current.structured_content["status"])
```

默认只接受回环 Host 和 Origin。公开部署的 OAuth 2.1 与授权发现属于生产部署验收。 [官方 MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.3.0)
