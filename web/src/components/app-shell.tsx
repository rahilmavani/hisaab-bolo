"use client";

import clsx from "clsx";
import { LayoutDashboard, LogOut, ReceiptIndianRupee, Users } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import type { ReactNode } from "react";
import { useApi } from "@/lib/api";
import { istDayLabel, istTime } from "@/lib/format";
import { supabaseBrowser } from "@/lib/supabase/client";
import type { Me } from "@/lib/types";
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
  return (
    <span className="flex items-center gap-2.5">
      <span
        aria-hidden
        className="grid h-9 w-9 place-items-center rounded-xl text-lg font-bold text-white"
        style={{ background: "linear-gradient(135deg, #8fb4ff, #2f6fed 60%, #1b3f9e)" }}
      >
        ₹
      </span>
      <span className="text-lg font-semibold tracking-tight">Hisaab Bolo</span>
    </span>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { data: me } = useMe();

  async function signOut() {
    await supabaseBrowser().auth.signOut();
    router.replace("/login");
    router.refresh();
  }

  const isActive = (href: string) => (href === "/" ? pathname === "/" : pathname.startsWith(href));

  return (
    <div className="min-h-dvh lg:grid lg:grid-cols-[232px_minmax(0,1fr)]">
      <aside className="sticky top-0 hidden h-dvh flex-col border-r border-line px-5 py-6 lg:flex">
        <Link href="/" aria-label="Hisaab Bolo home">
          <BrandMark />
        </Link>
        <nav aria-label="Main" className="mt-10 flex flex-col gap-1">
          {NAV.map(({ href, label, icon: Icon }) => (
            <Link
              key={href}
              href={href}
              aria-current={isActive(href) ? "page" : undefined}
              className={clsx(
                "flex items-center gap-3 rounded-xl px-3 py-2.5 text-[0.98rem] transition-colors",
                isActive(href) ? "bg-blue/20 text-text" : "text-muted hover:bg-white/5 hover:text-text",
              )}
            >
              <Icon size={18} aria-hidden />
              {label}
            </Link>
          ))}
        </nav>
        <div className="mt-auto space-y-4 text-sm">
          {me?.demo_tools ? <DemoClock me={me} /> : null}
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
      </aside>

      <div className="flex min-w-0 flex-col">
        <header className="flex items-center justify-between gap-3 px-4 pt-4 lg:hidden">
          <BrandMark />
          <button onClick={signOut} aria-label="Sign out" className="rounded-full p-2 text-muted hover:text-text">
            <LogOut size={18} />
          </button>
        </header>
        {/* A size container: pages lay out by the space they get. */}
        <main className="@container min-w-0 flex-1 px-4 pt-5 pb-28 sm:px-6 lg:px-10 lg:pt-8 lg:pb-12">
          {children}
          {me?.demo_tools ? (
            <div className="mt-8 lg:hidden">
              <DemoClock me={me} />
            </div>
          ) : null}
        </main>
      </div>

      <nav
        aria-label="Main"
        className="fixed inset-x-0 bottom-0 z-30 grid grid-cols-3 border-t border-line bg-ink/90 backdrop-blur lg:hidden"
      >
        {NAV.map(({ href, label, icon: Icon }) => (
          <Link
            key={href}
            href={href}
            aria-current={isActive(href) ? "page" : undefined}
            className={clsx("flex flex-col items-center gap-0.5 py-2.5 text-xs", isActive(href) ? "text-sky" : "text-muted")}
          >
            <Icon size={20} aria-hidden />
            {label}
          </Link>
        ))}
      </nav>

      {me ? <LiveUpdates merchantId={me.merchant.id} /> : null}
    </div>
  );
}
