import Link from "next/link";
import { ThemeToggle } from "../../ui";
import { AcceptInvitation } from "./accept-invitation";

export default function InvitePage() {
  return (
    <>
      <header className="topbar">
        <Link href="/" className="brand"><span className="brand-mark" aria-hidden>MS</span> MultiSearch</Link>
        <ThemeToggle />
      </header>
      <main id="main" className="entry">
        <p className="eyebrow">账户 / 邀请</p>
        <h1>接受团队邀请</h1>
        <AcceptInvitation />
      </main>
    </>
  );
}
