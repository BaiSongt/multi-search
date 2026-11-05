import { WorkspacePage } from "../../workspace";

export default async function Page({ params }: { params: Promise<{ section?: string[] }> }) {
  return <WorkspacePage workspace="console" route={(await params).section} />;
}
