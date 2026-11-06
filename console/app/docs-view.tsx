import Link from "next/link";
import { publicApiUrl } from "./lib/api-server";
import { Copy } from "./ui";

function Example({ title, value, open }: { title: string; value: string; open?: boolean }) {
  return (
    <details open={open}>
      <summary>{title}</summary>
      <pre>{value}</pre>
      <Copy value={value} label={`复制 ${title} 示例`} />
    </details>
  );
}

function curlExample(endpoint: string, body: object) {
  return [
    `curl "${endpoint}"`,
    '  -H "Authorization: Bearer $MULTISEARCH_KEY"',
    "  -H 'Content-Type: application/json'",
    `  -d '${JSON.stringify(body)}'`,
  ].join(" \\" + "\n");
}

export function DocsView({ canManageKeys }: { canManageKeys: boolean }) {
  const base = publicApiUrl("/api/v1");
  const search = publicApiUrl("/api/v1/search");
  const fetchPage = publicApiUrl("/api/v1/fetch");
  const mcp = publicApiUrl("/mcp");
  const authHeader = "Authorization: Bearer <API_KEY>";
  return (
    <>
      <section className="panel">
        <h2>搜索 API 快速接入</h2>
        <p>使用 API Key 从脚本或服务发起 JSON 请求。调用时无需登录控制台。</p>
        <div className="actions">
          {canManageKeys && <Link className="button primary" href="/console/keys">创建 API Key</Link>}
          <a className="button" href={publicApiUrl("/docs")} target="_blank" rel="noreferrer">OpenAPI 交互文档</a>
          <a className="button" href={publicApiUrl("/openapi.json")} target="_blank" rel="noreferrer">下载接口定义</a>
        </div>
        <dl>
          <dt>Base URL</dt>
          <dd><code>{base}</code> <Copy value={base} label="复制 Base URL" /></dd>
          <dt>搜索端点</dt>
          <dd><code>POST {search}</code> <Copy value={search} label="复制搜索端点" /></dd>
          <dt>鉴权请求头</dt>
          <dd><code>{authHeader}</code> <Copy value={authHeader} label="复制鉴权请求头" /></dd>
        </dl>
        <p>
          {canManageKeys ? "在 API 密钥页选择「新建密钥」并保存只显示一次的明文。"
            : "请向租户管理员获取有相应权限的 API Key。"}
          {" "}文字搜索需要 <code>search:text</code>，图片搜索需要 <code>search:image</code>。
          将密钥存入运行调用代码的环境变量 <code>MULTISEARCH_KEY</code>，即可运行以下示例。
        </p>
        <Example title="curl" open value={curlExample(search, { query: "NASA", max_results: 5 })} />
        <Example title="PowerShell" value={`$request = @{
    Method = "Post"
    Uri = "${search}"
    Headers = @{ Authorization = "Bearer $env:MULTISEARCH_KEY" }
    ContentType = "application/json"
    Body = @{ query = "NASA"; max_results = 5 } | ConvertTo-Json
}
$response = Invoke-RestMethod @request
$response.results | Select-Object title, url, snippet`} />
        <Example title="Python" value={`import os
import httpx

response = httpx.post(
    "${search}",
    headers={"Authorization": f"Bearer {os.environ['MULTISEARCH_KEY']}"},
    json={"query": "NASA", "max_results": 5},
    timeout=15,
)
response.raise_for_status()
print(response.json()["results"])`} />
        <Example title="Node.js" value={`const response = await fetch("${search}", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    Authorization: "Bearer " + process.env.MULTISEARCH_KEY,
  },
  body: JSON.stringify({ query: "NASA", max_results: 5 }),
});
if (!response.ok) throw new Error("HTTP " + response.status);
console.log((await response.json()).results);`} />
        <p className="muted">
          文字结果位于 <code>results</code>，每条包含 <code>title</code>、<code>url</code> 和 <code>snippet</code>。
          <code>401</code> 表示密钥缺失或无效，<code>403</code> 表示权限不足，<code>429</code> 表示达到限流或额度上限。
        </p>
      </section>
      <section className="panel">
        <h2>批量网页提取</h2>
        <p>使用包含 <code>fetch</code> 作用域的 API Key 提交网址。响应包含任务 ID 与状态地址。任务仍在排队或运行时，可对任务地址追加 <code>/cancel</code> 并发送 POST。</p>
        <pre>{curlExample(fetchPage, { urls: ["https://example.org/"], max_pages: 1 })}</pre>
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
