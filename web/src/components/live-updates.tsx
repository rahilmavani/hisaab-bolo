"use client";

import { CheckCircle2, MailCheck, Phone, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useSWRConfig } from "swr";
import { callOutcomeText, callStatusLabel } from "@/components/call-status";
import { api } from "@/lib/api";
import { inr } from "@/lib/format";
import type { InvoiceDetail, Reminder } from "@/lib/types";
import { supabaseBrowser } from "@/lib/supabase/client";

type Toast = { id: string; title: string; body: string; icon?: "paid" | "call" | "link" };

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
  // Call updates: announce each call's end once, and a mid-call payment link once.
  const announcedCalls = useRef(new Set<string>());

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
    const push = (toast: Toast) => {
      setToasts((t) => [...t.filter((x) => x.id !== toast.id), toast]);
      setTimeout(() => setToasts((t) => t.filter((x) => x.id !== toast.id)), 9000);
    };
    const onCall = async (r: Reminder & { invoice_id: string }) => {
      const ended = r.call_status && r.call_status !== "queued";
      const key = ended ? `end:${r.id}` : r.call_link_sent_at ? `link:${r.id}` : null;
      if (!key || announcedCalls.current.has(key)) return;
      announcedCalls.current.add(key);
      if (ended) announcedCalls.current.add(`link:${r.id}`); // the end toast mentions the link
      let who = "the customer";
      let number = "";
      try {
        const inv = await api<InvoiceDetail>(`/invoices/${r.invoice_id}`);
        who = inv.payer_name;
        number = inv.number;
      } catch {
        /* toast without the name */
      }
      if (ended) {
        const outcome = callOutcomeText(r.call_outcome, r.call_promised_date);
        const parts = [outcome ?? callStatusLabel(r.call_status, r.call_duration_s).text];
        if (r.call_link_sent_at) parts.push("payment link emailed during the call");
        push({ id: key, icon: "call", title: `Call with ${who} ended`, body: parts.join(" · ") });
      } else {
        push({
          id: key,
          icon: "link",
          title: `${who} asked for the payment link`,
          body: `The agent emailed the Paytm link${number ? ` for ${number}` : ""} during the call`,
        });
      }
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
          // INSERT: a reminder went out; UPDATE: a reminder call ended and its outcome arrived.
          event: "*",
          schema: "public",
          table: "reminders",
          filter: `merchant_id=eq.${merchantId}`,
        },
        (payload) => {
          refresh();
          const r = payload.new as Reminder & { invoice_id: string };
          if (payload.eventType === "UPDATE" && r?.channel === "call") void onCall(r);
        },
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
          className="pointer-events-auto flex items-start gap-3 rounded-2xl border border-mint/30 bg-card/95 px-4 py-3 shadow-xl backdrop-blur"
        >
          {t.icon === "call" ? (
            <Phone size={20} className="mt-0.5 text-blue" aria-hidden />
          ) : t.icon === "link" ? (
            <MailCheck size={20} className="mt-0.5 text-mint" aria-hidden />
          ) : (
            <CheckCircle2 size={20} className="mt-0.5 text-mint" aria-hidden />
          )}
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
