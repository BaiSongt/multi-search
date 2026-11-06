# REST 客户端

仓库提供两个可复用的源码客户端。Python 客户端使用 Python 3.12 标准库。TypeScript 客户端使用原生 Fetch，可在 Node.js 22.18 及以上直接运行。两者均通过 API Key 调用机器接口，可在未配置 Key 时查询部署能力。请将 Key 注入服务端进程的环境或从密钥管理器读取。 [部署与签发](deployment.md#签发测试凭据) · [REST 契约](api-contracts.md)

## Python 3.12

以下示例从仓库根目录运行，使用调用方环境中的 `MULTISEARCH_KEY`。

```python
import os
from uuid import uuid4

from sdk.python.multisearch_client import MultiSearchClient

client = MultiSearchClient("http://127.0.0.1:8765", os.environ["MULTISEARCH_KEY"])
capabilities = client.capabilities().body
response = client.search(
    {"query": "公开技术文档", "mode": "quick", "max_results": 5},
    request_id=str(uuid4()),
    idempotency_key=str(uuid4()),
)
if "result_status" in response.body:
    print(response.body["result_status"], response.body.get("provider_reason_codes", []))

if capabilities["research"] == "available":
    submitted = client.research(
        {"query": "如何复核一项公开技术结论", "max_search_rounds": 2},
        idempotency_key=str(uuid4()),
    )
    print(client.get_job(submitted.body["job_id"]).body["status"])
```

## TypeScript

将示例放在仓库根目录的 `example.ts` 后运行 `node example.ts`。在 TypeScript 项目中也可按项目配置编译并导入源码。

```ts
import { randomUUID } from "node:crypto";
import { MultiSearchClient } from "./sdk/typescript/multisearch.ts";

const key = process.env.MULTISEARCH_KEY;
if (!key) throw new Error("MULTISEARCH_KEY is required");

const client = new MultiSearchClient("http://127.0.0.1:8765", { apiKey: key });
const capabilities = (await client.capabilities()).body;
const response = await client.search(
  { query: "公开技术文档", mode: "quick", max_results: 5 },
  { requestId: randomUUID(), idempotencyKey: randomUUID() },
);
if ("result_status" in response.body) {
  console.log(response.body.result_status, response.body.provider_reason_codes);
}

if (capabilities.research === "available") {
  const submitted = await client.research(
    { query: "如何复核一项公开技术结论", max_search_rounds: 2 },
    { idempotencyKey: randomUUID() },
  );
  console.log((await client.getJob(submitted.body.job_id)).body.status);
}
```

## 请求和结果

| 操作 | Python | TypeScript | HTTP 成功状态与正文 |
| --- | --- | --- | --- |
| 发现能力 | `capabilities()` | `capabilities()` | `200`，部署能力 |
| 搜索 | `search(request)` | `search(request)` | `200`，同步结果；深度搜索返回 `202` 和任务标识 |
| 抓取 | `fetch(request)` | `fetch(request)` | `202`，任务标识 |
| 深度研究 | `research(request)` | `research(request)` | `202`，任务标识 |
| 查询任务 | `get_job(job_id)` | `getJob(jobId)` | `200`，任务状态 |
| 取消任务 | `cancel_job(job_id)` | `cancelJob(jobId)` | `200`，任务状态 |

抓取请求可传 `{"url": "https://example.org/"}` 或 `{"urls": ["https://example.org/"]}`，需要 `fetch` 作用域。研究请求需要 `research` 与相应模态的搜索作用域。请在呈现同步搜索结果时检查 `result_status` 和 `provider_reason_codes`，并保留警告、证据引用和图片的权利状态。错误对象的对应字段为 Python 的 `provider_reason_codes` 或 TypeScript 的 `providerReasonCodes`。

提交请求可传独立的请求标识和幂等键，也可使用请求正文中的 `request_id` 与 `idempotency_key`。首次提交前保存幂等键。若请求超时且服务端是否受理尚未确认，重试时复用原请求内容和幂等键。可记录返回的请求与追踪标识以关联服务端日志。错误对象提供 HTTP 状态、错误码、是否可重试及服务端建议的重试间隔。客户端将重试间隔归一为毫秒，重试由应用控制。 [幂等与错误契约](api-contracts.md)

客户端接受可信的 HTTPS 服务地址，也支持本地字面回环地址的 HTTP。请求固定发往配置的 API 路径，所有重定向会终止调用。任务链接仅供展示，查询和取消始终根据任务 UUID 构造固定路径。客户端限制请求体为 32 KiB、响应体默认 4 MiB，可将响应体上限调整至 16 MiB。网络超时默认 30 秒。TypeScript 以 AbortSignal 控制请求和响应读取，Python 标准库使用套接字超时并在读取响应时检查截止时间，DNS 解析时间由系统控制。客户端超时或中止后，已提交的后台任务仍可能继续运行。

```sh
python -m unittest discover -s sdk/python -v
node --test sdk/typescript/multisearch.test.mjs
```
