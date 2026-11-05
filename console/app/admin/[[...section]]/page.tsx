import { WorkspacePage } from "../../workspace";

export default async function Page({ params }: { params: Promise<{ section?: string[] }> }) {
  return <WorkspacePage workspace="admin" route={(await params).section} />;
}
