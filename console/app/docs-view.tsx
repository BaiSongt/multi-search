import { publicApiUrl } from "./lib/api-server";

export function DocsView() {
  const search = publicApiUrl("/api/v1/search");
  const fetchPage = publicApiUrl("/api/v1/fetch");
  const mcp = publicApiUrl("/mcp");
  return (
    <>
      <section className="panel">
        <h2>REST API</h2>
        <p>API Key 用于机器鉴权，文字检索需要 <code>search:text</code>，图片检索需要 <code>search:image</code>。</p>
        <div className="actions">
          <a className="button" href={publicApiUrl("/docs")} target="_blank" rel="noreferrer">OpenAPI 交互文档</a>
          <a className="button" href={publicApiUrl("/openapi.json")} target="_blank" rel="noreferrer">下载接口定义</a>
        </div>
        <details open><summary>curl</summary><pre>{`curl "${search}" \\\n  -H "Authorization: Bearer $MULTISEARCH_KEY" \\\n  -H 'Content-Type: application/json' \\\n  -d '{"query":"NASA","modalities":["text"]}'`}</pre></details>
        <details><summary>Python</summary><pre>{`import os
import httpx

response = httpx.post(
    "${search}",
    headers={"Authorization": f"Bearer {os.environ['MULTISEARCH_KEY']}"},
    json={"query": "NASA", "modalities": ["text"]},
    timeout=15,
)
response.raise_for_status()
print(response.json())`}</pre></details>
        <details><summary>TypeScript</summary><pre>{`const response = await fetch("${search}", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    Authorization: "Bearer " + process.env.MULTISEARCH_KEY,
  },
  body: JSON.stringify({ query: "NASA", modalities: ["text"] }),
});
if (!response.ok) throw new Error("HTTP " + response.status);
console.log(await response.json());`}</pre></details>
      </section>
      <section className="panel">
        <h2>批量网页提取</h2>
        <p>使用包含 <code>fetch</code> 作用域的 API Key 提交网址。响应包含任务 ID 与状态地址。任务仍在排队或运行时，可对任务地址追加 <code>/cancel</code> 并发送 POST。</p>
        <pre>{`curl "${fetchPage}" \\\n  -H "Authorization: Bearer $MULTISEARCH_KEY" \\\n  -H 'Content-Type: application/json' \\\n  -d '{"urls":["https://example.org/"],"max_pages":1}'`}</pre>
        <p className="muted">抓取需要已启用的 Worker。查看能力端点中的 <code>fetch</code> 状态以确认可用性。</p>
      </section>
      <section className="panel">
        <h2>MCP Streamable HTTP</h2>
        <p>用相同 API Key 调用 <code>multi_search</code> 和 <code>image_search</code>。密钥由客户端从安全存储注入。</p>
        <pre>{JSON.stringify({
          mcpServers: {
            multisearch: {
              type: "streamable-http",
              url: mcp,
              headers: { Authorization: "Bearer <API_KEY>" },
            },
          },
        }, null, 2)}</pre>
      </section>
    </>
  );
}
