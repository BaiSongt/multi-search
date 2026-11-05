# MCP Streamable HTTP

默认端点为 `http://127.0.0.1:8765/mcp`。客户端需要配置 `Authorization: Bearer <API_KEY>` 请求头。服务在每次传输请求验证 Key，使用与 REST 相同的作用域、租户配额、缓存、检索预算与图片结果模型。

| 工具 | 参数 | 权限 |
| --- | --- | --- |
| `multi_search` | 与 REST SearchRequest 一致，默认文字模态 | 按请求模态检查 `search:text`、`search:image` |
| `image_search` | 与 SearchRequest 一致，省略 `modalities`，固定为图片 | `search:image` |

工具的结构化结果与 REST SearchResponse 一致。工具执行失败时返回 `isError` 与结构化错误对象。客户端应明确展示 `partial`、`warnings` 与图片 `rights_status`。搜索候选不代表获得作品授权。

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

默认只接受回环 Host 和 Origin。公开部署的 OAuth 2.1 与授权发现属于生产部署验收。 [官方 MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.3.0)
