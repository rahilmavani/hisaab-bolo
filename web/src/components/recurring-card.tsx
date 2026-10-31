"use client";

import { CalendarClock, Repeat } from "lucide-react";
import { useState } from "react";
import { api } from "@/lib/api";
import { dateOnly, inr } from "@/lib/format";
import type { PayerProfile } from "@/lib/types";
import { RecurringDialog } from "./recurring-dialog";
import { SectionTitle } from "./ui";

/** Recurring status on a payer profile: the schedule, or a suggestion when their pattern is steady. */
export function RecurringCard({
  payer,
  today,
  onChange,
}: {
  payer: PayerProfile;
  today: string;
  onChange: (p: PayerProfile | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const s = payer.recurring_schedule;
  const suggestion = payer.recurring_suggestion;

  async function stop() {
    setBusy(true);
    try {
      await api(`/payers/${payer.id}/recurring`, { method: "DELETE" });
      setNote("Recurring stopped. Open invoices stay payable; no more automatic emails go out.");
      onChange(null);
    } catch (e) {
      setNote(e instanceof Error ? e.message : "Couldn't stop it");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel p-5 sm:p-6">
      <SectionTitle title="Recurring" />
      {s ? (
        <div className="space-y-2 text-[0.95rem]">
          <p className="flex items-center gap-2">
            <Repeat size={16} className="text-mint" aria-hidden />
            <span>
              {inr(s.amount_paise)} {s.cadence}
            </span>
          </p>
          <p className="flex items-center gap-2 text-muted">
            <CalendarClock size={16} aria-hidden /> Next due {dateOnly(s.next_due_date)}, {s.grace_days} day
            {s.grace_days === 1 ? "" : "s"} grace
          </p>
          <p className="text-sm text-muted">
            {s.email && s.auto_remind
              ? `Invoices and overdue reminders go to ${s.email}.`
              : s.email
                ? `Invoices go to ${s.email}; no automatic reminders.`
                : "Tracking only: dues and missed payments, no emails. Edit to add an email."}
          </p>
          <div className="mt-1 flex flex-wrap gap-2">
            <button className="btn btn-quiet px-3 py-1.5 text-sm" onClick={() => setOpen(true)} disabled={busy}>
              Edit schedule
            </button>
            <button className="btn btn-quiet px-3 py-1.5 text-sm" onClick={stop} disabled={busy}>
              Stop recurring
            </button>
          </div>
        </div>
      ) : (
        <div className="space-y-3">
          {suggestion ? (
            <p className="rounded-2xl border border-sky/25 bg-blue/10 px-3 py-2.5 text-sm">
              {payer.display_name} pays a steady {inr(suggestion.amount_paise)}
              {suggestion.every_days === 1 ? " every day" : suggestion.every_days === 7 ? " every week" : " every month"}.
              {" "}
              Track it as {inr(suggestion.suggested_amount_paise)} {suggestion.cadence}?
            </p>
          ) : (
            <p className="text-sm text-muted">Bill this customer on a schedule and remind them automatically.</p>
          )}
          <button className="btn btn-primary px-4 py-2 text-sm" onClick={() => setOpen(true)}>
            <Repeat size={15} aria-hidden /> Mark recurring
          </button>
        </div>
      )}
      {note ? (
        <p role="status" className="mt-3 text-sm text-mint">
          {note}
        </p>
      ) : null}
      <RecurringDialog
        key={String(open)}
        payer={payer}
        open={open}
        today={today}
        onClose={() => setOpen(false)}
        onSaved={(p, n) => {
          setNote(n);
          onChange(p);
        }}
      />
    </section>
  );
}
