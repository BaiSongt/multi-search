import Link from "next/link";
import { apiUrl, publicApiUrl } from "./lib/api-server";
import { ThemeToggle } from "./ui";

async function apiReady() {
  try {
    const response = await fetch(apiUrl("/health/ready"), {
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
  return (
    <>
      <header className="topbar">
        <span className="brand"><span className="brand-mark" aria-hidden>MS</span> MultiSearch</span>
        <ThemeToggle />
      </header>
      <main id="main" className="entry">
        <p className="eyebrow">MultiSearch / 工作区</p>
        <h1>通用检索服务</h1>
        <p className="muted">搜索文字、新闻与图片，管理 API 密钥和调用量。</p>
        <div className="status-line">
          <span className={ready ? "badge positive" : "badge warning"}>{ready ? "服务就绪" : "待就绪"}</span>
          <span className="muted">API、数据库与缓存</span>
        </div>
        <div className="actions">
          <Link className="button primary" href="/auth">进入工作区</Link>
          <a className="button" href={publicApiUrl("/docs")}>OpenAPI 文档</a>
        </div>
      </main>
    </>
  );
}
