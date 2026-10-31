"use client";

import clsx from "clsx";
import { MailCheck, Phone } from "lucide-react";
import Link from "next/link";
import { useApi } from "@/lib/api";
import { dateOnly, inr } from "@/lib/format";
import { SectionTitle } from "./ui";

type CallPromise = {
  invoice_id: string;
  number: string;
  amount_paise: number;
  payer_id: string;
  payer_name: string;
  promised_date: string | null;
  promise_state: "broken" | "today" | "upcoming" | "none";
  called_at: string;
  call_link_sent_at: string | null;
  call_summary: string | null;
};

const STATE: Record<CallPromise["promise_state"], { label: (d: string | null) => string; tone: string }> = {
  broken: { label: (d) => `Promised ${d ? dateOnly(d) : ""}, still unpaid`, tone: "text-amber" },
  today: { label: () => "Promised today", tone: "text-sky" },
  upcoming: { label: (d) => `Promised by ${d ? dateOnly(d) : ""}`, tone: "text-muted" },
  none: { label: () => "Said they'll pay", tone: "text-muted" },
};

/** What customers promised on reminder calls. Hidden until a call ends in a promise. */
export function PromisedPayments() {
  const { data } = useApi<{ promises: CallPromise[] }>("/dashboard/promises");
  const rows = data?.promises ?? [];
  if (!rows.length) return null;
  const broken = rows.filter((r) => r.promise_state === "broken").length;
  return (
    <section className="panel min-w-0 p-5 sm:p-6" aria-labelledby="promises-title">
      <SectionTitle
        title="Promised on calls"
        sub={`${rows.length} customer${rows.length === 1 ? "" : "s"} promised to pay${broken ? ` · ${broken} missed the date` : ""}`}
      />
      <h2 id="promises-title" className="sr-only">
        Promised on calls
      </h2>
      <ul className="space-y-2.5">
        {rows.map((r) => {
          const st = STATE[r.promise_state];
          return (
            <li key={r.invoice_id}>
              <Link
                href={`/collections/${r.invoice_id}`}
                className={clsx(
                  "flex items-start gap-3 rounded-2xl border px-4 py-3",
                  r.promise_state === "broken"
                    ? "border-amber/25 bg-amber/[0.05] hover:border-amber/45"
                    : "border-line hover:border-line-strong",
                )}
              >
                <Phone size={16} className="mt-1 shrink-0 text-sky" aria-hidden />
                <div className="min-w-0 flex-1">
                  <p className="flex flex-wrap items-baseline justify-between gap-x-3">
                    <span className="font-semibold">{r.payer_name}</span>
                    <span className="num font-mono">{inr(r.amount_paise)}</span>
                  </p>
                  <p className="text-sm">
                    <span className={st.tone}>{st.label(r.promised_date)}</span>
                    <span className="font-mono text-xs text-faint"> · {r.number}</span>
                  </p>
                  {r.call_link_sent_at ? (
                    <p className="mt-0.5 flex items-center gap-1 text-xs text-mint">
                      <MailCheck size={12} aria-hidden /> Link sent during the call
                    </p>
                  ) : null}
                </div>
              </Link>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
