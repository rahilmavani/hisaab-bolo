"use client";

import clsx from "clsx";
import { LayoutDashboard, LogOut, Mic, ReceiptIndianRupee, Users } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState, type ReactNode } from "react";
import { useApi } from "@/lib/api";
import { istDayLabel, istTime, setAppNow } from "@/lib/format";
import { supabaseBrowser } from "@/lib/supabase/client";
import type { Me } from "@/lib/types";
import { AskPanel } from "./ask/ask-panel";
import { DemoClock } from "./demo-clock";
import { LiveUpdates } from "./live-updates";

const NAV = [
  { href: "/", label: "Today", icon: LayoutDashboard },
  { href: "/customers", label: "Customers", icon: Users },
  { href: "/collections", label: "Collections", icon: ReceiptIndianRupee },
];

export function useMe() {
  return useApi<Me>("/me", { refreshInterval: 60_000 });
}

function BrandMark() {
  return <span className="text-lg leading-none font-semibold tracking-tight">Hisaab Bolo</span>;
}

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { data: me } = useMe();
  const [askOpen, setAskOpen] = useState(false);
  // Relative dates ("today", "yesterday") follow the app clock, which the demo clock can move.
  if (me) setAppNow(me.now);

  async function signOut() {
    await supabaseBrowser().auth.signOut();
    router.replace("/login");
    router.refresh();
  }

  const isActive = (href: string) => (href === "/" ? pathname === "/" : pathname.startsWith(href));

  return (
    <div className="min-h-dvh lg:grid lg:grid-cols-[240px_minmax(0,1fr)]">
      <aside className="sticky top-0 hidden h-dvh flex-col border-r border-line bg-side px-4 py-6 lg:flex">
        <Link href="/" aria-label="Hisaab Bolo home" className="flex h-8 items-center rounded-lg px-2">
          <BrandMark />
        </Link>
        <nav aria-label="Main" className="mt-8 flex flex-col gap-0.5">
          {NAV.map(({ href, label, icon: Icon }) => (
            <Link
              key={href}
              href={href}
              aria-current={isActive(href) ? "page" : undefined}
              className={clsx(
                "flex h-9 items-center gap-2.5 rounded-lg px-2.5 text-sm transition-colors",
                isActive(href)
                  ? "bg-card text-text shadow-[inset_0_0_0_1px_var(--color-line)]"
                  : "text-muted hover:bg-raised hover:text-text",
              )}
            >
              <Icon size={18} aria-hidden />
              {label}
            </Link>
          ))}
        </nav>
        <div className="mt-auto space-y-4 text-sm">
          {me?.demo_tools ? <DemoClock me={me} /> : null}
          <div className="space-y-2 px-2">
            {me ? (
              <div>
                <p className="font-medium text-text">{me.merchant.name}</p>
                {!me.demo_tools ? (
                  <p className="text-muted">
                    {istDayLabel(me.now)}, {istTime(me.now)}
                  </p>
                ) : null}
              </div>
            ) : null}
            <button onClick={signOut} className="flex items-center gap-2 text-muted hover:text-text">
              <LogOut size={16} aria-hidden /> Sign out
            </button>
          </div>
        </div>
      </aside>

      <div className="flex min-w-0 flex-col">
        <header className="flex items-center justify-between gap-3 px-4 pt-4 lg:hidden">
          <BrandMark />
          <button onClick={signOut} aria-label="Sign out" className="rounded-lg p-2 text-muted hover:bg-raised hover:text-text">
            <LogOut size={18} />
          </button>
        </header>
        <div className="min-w-0 flex-1 xl:grid xl:grid-cols-[minmax(0,1fr)_400px]">
          {/* A size container: pages lay out by the space they get (the Ask panel docks beside them on xl). */}
          <main className="@container min-w-0 px-4 pt-5 pb-28 sm:px-6 lg:px-10 lg:pt-8 lg:pb-12">
            {children}
            {me?.demo_tools ? (
              <div className="mt-8 lg:hidden">
                <DemoClock me={me} />
              </div>
            ) : null}
          </main>
          <AskPanel open={askOpen} onClose={() => setAskOpen(false)} />
        </div>
      </div>

      {/* Between the phone tab bar and the docked panel (lg–xl), a floating button opens Ask Hisaab. */}
      <button
        onClick={() => setAskOpen(true)}
        className="fixed right-6 bottom-6 z-30 hidden h-12 items-center gap-2 rounded-xl bg-blue px-5 font-medium text-white shadow-[0_8px_24px_rgb(31_27_22/0.14)] transition-colors hover:bg-blue-strong lg:flex xl:hidden"
      >
        <Mic size={18} aria-hidden /> Ask Hisaab
      </button>

      <nav
        aria-label="Main"
        className="fixed inset-x-0 bottom-0 z-30 grid grid-cols-4 border-t border-line bg-page/95 backdrop-blur lg:hidden"
      >
        {NAV.map(({ href, label, icon: Icon }) => (
          <Link
            key={href}
            href={href}
            aria-current={isActive(href) ? "page" : undefined}
            className={clsx("flex flex-col items-center gap-0.5 py-2.5 text-xs", isActive(href) ? "text-blue" : "text-muted")}
          >
            <Icon size={20} aria-hidden />
            {label}
          </Link>
        ))}
        <button onClick={() => setAskOpen(true)} className="flex flex-col items-center gap-0.5 py-2.5 text-xs text-blue">
          <Mic size={20} aria-hidden />
          Ask
        </button>
      </nav>

      {me ? <LiveUpdates merchantId={me.merchant.id} /> : null}
    </div>
  );
}
