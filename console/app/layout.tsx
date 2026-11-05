import type { Metadata } from "next";
import { cookies } from "next/headers";
import type { ReactNode } from "react";
import "./style.css";

export const metadata: Metadata = {
  title: "MultiSearch",
  description: "MultiSearch 检索服务与管理工作区",
};

export default async function Layout({ children }: { children: ReactNode }) {
  const theme = (await cookies()).get("multisearch_theme")?.value;
  return (
    <html lang="zh-CN" data-theme={theme === "dark" || theme === "light" ? theme : undefined}>
      <body>
        <a href="#main" className="skip">跳到主要内容</a>
        {children}
      </body>
    </html>
  );
}
