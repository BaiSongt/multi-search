export const dynamic = "force-dynamic";

async function apiReady() {
  const url = process.env.MULTISEARCH_INTERNAL_API_URL ?? "http://127.0.0.1:8765";
  try {
    const response = await fetch(new URL("/health/ready", url), {
      cache: "no-store",
      signal: AbortSignal.timeout(2000),
    });
    return response.ok;
  } catch {
    return false;
  }
}

export default async function Home() {
  const ready = await apiReady();
  const docsUrl = new URL("/docs", process.env.MULTISEARCH_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8765");

  return (
    <main>
      <p className="label">MULTISEARCH / 开发预览</p>
      <h1>通用检索服务</h1>
      <p>文字、新闻与图片搜索使用同一套 API 契约。</p>
      <section aria-labelledby="status">
        <h2 id="status">服务状态</h2>
        <p><span className={ready ? "ready" : "pending"}>{ready ? "就绪" : "待就绪"}</span> API、数据库与缓存</p>
        <a href={docsUrl.href}>查看 OpenAPI 文档</a>
      </section>
    </main>
  );
}
