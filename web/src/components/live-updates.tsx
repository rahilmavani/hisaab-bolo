"use client";

import { CheckCircle2, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useSWRConfig } from "swr";
import { inr } from "@/lib/format";
import { supabaseBrowser } from "@/lib/supabase/client";

type Toast = { id: string; title: string; body: string };

const LIVE_KEYS = ["/invoices", "/dashboard", "/payers", "/transactions"];

/** Supabase Realtime (RLS-scoped) → refresh data and announce paid invoices. */
export function LiveUpdates({ merchantId }: { merchantId: string }) {
  const { mutate } = useSWRConfig();
  const [toasts, setToasts] = useState<Toast[]>([]);
  // With RLS on, Realtime's `old` row only carries the primary key, so "just paid" can't be read off
  // the invoice UPDATE alone (a merge or a late failure webhook also updates paid invoices). A toast
  // needs both halves of one payment: the invoice turning up paid and a new payment row for it.
  const announced = useRef(new Set<string>());
  const paidInvoices = useRef(
    new Map<string, { number: string; amount_paise: number }>(),
  );
  const freshPayments = useRef(new Set<string>());

  useEffect(() => {
    const supabase = supabaseBrowser();
    // Coalesce bursts (a bulk insert or re-seed delivers thousands of events): the first event
    // schedules one refresh; everything arriving before it fires rides along.
    // Matcher only (no data argument): revalidate in place without clearing what's on screen.
    let timer: ReturnType<typeof setTimeout> | null = null;
    const announce = (id: string) => {
      const inv = paidInvoices.current.get(id);
      if (!inv || !freshPayments.current.has(id) || announced.current.has(id))
        return;
      announced.current.add(id);
      const toast = {
        id,
        title: `${inv.number} paid`,
        body: `${inr(inv.amount_paise)} received and matched`,
      };
      setToasts((t) => [...t.filter((x) => x.id !== id), toast]);
      setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 8000);
    };
    const refresh = () => {
      if (timer) return;
      timer = setTimeout(() => {
        timer = null;
        mutate(
          (key) =>
            typeof key === "string" && LIVE_KEYS.some((k) => key.startsWith(k)),
        );
      }, 600);
    };
    const channel = supabase
      .channel(`merchant:${merchantId}`)
      .on(
        "postgres_changes",
        {
          event: "UPDATE",
          schema: "public",
          table: "invoices",
          filter: `merchant_id=eq.${merchantId}`,
        },
        (payload) => {
          const inv = payload.new as {
            id: string;
            number: string;
            amount_paise: number;
            status: string;
          };
          refresh();
          if (inv.status === "paid") {
            paidInvoices.current.set(inv.id, {
              number: inv.number,
              amount_paise: inv.amount_paise,
            });
            announce(inv.id);
          }
        },
      )
      .on(
        "postgres_changes",
        {
          event: "INSERT",
          schema: "public",
          table: "invoices",
          filter: `merchant_id=eq.${merchantId}`,
        },
        refresh,
      )
      .on(
        "postgres_changes",
        {
          event: "INSERT",
          schema: "public",
          table: "transactions",
          filter: `merchant_id=eq.${merchantId}`,
        },
        (payload) => {
          refresh();
          const invoiceId = (payload.new as { invoice_id: string | null })
            .invoice_id;
          if (invoiceId) {
            freshPayments.current.add(invoiceId);
            announce(invoiceId);
          }
        },
      )
      .on(
        "postgres_changes",
        {
          event: "INSERT",
          schema: "public",
          table: "reminders",
          filter: `merchant_id=eq.${merchantId}`,
        },
        refresh,
      )
      .subscribe();
    return () => {
      if (timer) clearTimeout(timer);
      supabase.removeChannel(channel);
    };
  }, [merchantId, mutate]);

  return (
    <div
      aria-live="polite"
      className="pointer-events-none fixed top-4 left-1/2 z-50 flex -translate-x-1/2 flex-col gap-2 lg:left-[calc(50%+116px)] xl:left-[calc(50%-84px)]"
    >
      {toasts.map((t) => (
        <div
          key={t.id}
          role="status"
          className="pointer-events-auto flex items-start gap-3 rounded-2xl border border-mint/30 bg-ink-2/95 px-4 py-3 shadow-xl backdrop-blur"
        >
          <CheckCircle2 size={20} className="mt-0.5 text-mint" aria-hidden />
          <div>
            <p className="font-semibold">{t.title}</p>
            <p className="text-sm text-muted">{t.body}</p>
          </div>
          <button
            onClick={() => setToasts((x) => x.filter((y) => y.id !== t.id))}
            aria-label="Dismiss"
            className="text-muted hover:text-text"
          >
            <X size={16} />
          </button>
        </div>
      ))}
    </div>
  );
}
