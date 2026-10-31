"use client";

import { useState, type FormEvent } from "react";
import { api } from "@/lib/api";
import { dateOnly, inr } from "@/lib/format";
import type { PayerProfile } from "@/lib/types";
import { Dialog } from "./dialog";

type Cadence = "weekly" | "monthly";

type Result = {
  schedule: { next_due_date: string; tracking_only: boolean; updated: boolean };
  dues_created: {
    invoice: string;
    due_date: string;
    emailed: boolean;
    email_error: string | null;
  }[];
  already_invoiced: { invoice: string; due_date: string } | null;
  due_error: string | null;
  emailed_now: string[];
  payer: PayerProfile;
};

/** A sensible amount per due: the suggestion when there is one, else usual payment × payments per period. */
function defaultAmount(payer: PayerProfile, cadence: Cadence): string {
  const s = payer.recurring_suggestion;
  if (s && s.cadence === cadence) return String(s.suggested_amount_paise / 100);
  const usual = payer.median_amount_paise ?? 0;
  if (!usual) return "";
  const gap = payer.median_gap_days ?? 0;
  const perPeriod =
    gap > 0
      ? Math.max(1, Math.round((cadence === "weekly" ? 7 : 30) / gap))
      : 1;
  return String(Math.round((usual * perPeriod) / 100));
}

/** Overdue means due date + grace days have fully passed, so the reminder goes out the day after. */
function graceNote(grace: number): string {
  if (grace === 0)
    return "If it isn't paid by the due date, a reminder goes out the next day.";
  return `If it's still unpaid ${grace} day${grace === 1 ? "" : "s"} after the due date, a reminder goes out the following day.`;
}

export function RecurringDialog({
  payer,
  open,
  onClose,
  onSaved,
  today,
}: {
  payer: PayerProfile;
  open: boolean;
  onClose: () => void;
  onSaved: (p: PayerProfile, note: string) => void;
  today: string;
}) {
  const existing = payer.recurring_schedule;
  const [cadence, setCadence] = useState<Cadence>(
    existing?.cadence ?? payer.recurring_suggestion?.cadence ?? "monthly",
  );
  const [amount, setAmount] = useState(
    existing
      ? String(existing.amount_paise / 100)
      : defaultAmount(payer, cadence),
  );
  const [amountTouched, setAmountTouched] = useState(Boolean(existing));
  const [first, setFirst] = useState(
    existing && existing.next_due_date >= today
      ? existing.next_due_date
      : today,
  );
  const [grace, setGrace] = useState(String(existing?.grace_days ?? 3));
  const [email, setEmail] = useState(
    existing ? (existing.email ?? "") : (payer.email ?? ""),
  );
  const [callRemind, setCallRemind] = useState(existing?.call_remind ?? false);
  const [phone, setPhone] = useState(payer.phone ?? "");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function pickCadence(c: Cadence) {
    setCadence(c);
    if (!amountTouched) setAmount(defaultAmount(payer, c));
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    const rupees = Number(amount);
    const graceDays = Number(grace);
    if (!rupees || rupees < 1)
      return setError("Enter the amount they pay each time (at least ₹1).");
    if (!Number.isInteger(graceDays) || graceDays < 0 || graceDays > 60)
      return setError("Grace should be 0 to 60 days.");
    if (callRemind && !phone.trim())
      return setError("Add their phone number so the agent can call them.");
    setBusy(true);
    setError(null);
    try {
      const r = await api<Result>(`/payers/${payer.id}/recurring`, {
        method: "POST",
        body: JSON.stringify({
          cadence,
          amount_paise: Math.round(rupees * 100),
          anchor_date: first,
          grace_days: graceDays,
          email: email.trim() || null,
          call_remind: callRemind,
          phone: callRemind ? phone.trim() : null,
        }),
      });
      const due = r.dues_created[0];
      const note = r.due_error
        ? `Schedule saved, but the first invoice couldn't be created (${r.due_error}). It will be tried again automatically.`
        : r.schedule.tracking_only
          ? `Tracking ${cadence} dues. Edit the schedule and add an email to send invoices and reminders.`
          : due
            ? `${due.invoice} for ${dateOnly(due.due_date)} created${due.emailed ? " and emailed" : due.email_error ? "; the email didn't go out and will be retried" : ""}.`
            : r.already_invoiced
              ? `${r.already_invoiced.invoice} for ${dateOnly(r.already_invoiced.due_date)} is already invoiced${r.emailed_now.includes(r.already_invoiced.invoice) ? `; emailed it to ${email.trim()}` : ""}.`
              : r.schedule.updated
                ? "Schedule updated."
                : `First invoice goes out 3 days before ${dateOnly(r.schedule.next_due_date)}.`;
      onSaved(r.payer, note);
      onClose();
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message.replace(
              /^grace_days: .*/,
              "Grace should be 0 to 60 days.",
            )
          : "Couldn't save",
      );
    } finally {
      setBusy(false);
    }
  }

  const usual = payer.median_amount_paise ?? 0;
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={
        existing
          ? `Edit ${payer.display_name}'s schedule`
          : `Mark ${payer.display_name} as recurring`
      }
    >
      <form onSubmit={submit} className="space-y-4">
        <div
          role="radiogroup"
          aria-label="How often"
          className="grid grid-cols-2 gap-2"
        >
          {(["weekly", "monthly"] as const).map((c) => (
            <button
              key={c}
              type="button"
              role="radio"
              aria-checked={cadence === c}
              onClick={() => pickCadence(c)}
              className={`rounded-2xl border px-4 py-3 text-left ${cadence === c ? "border-blue bg-blue/15" : "border-line hover:border-line-strong"}`}
            >
              <span className="block font-semibold">
                {c === "weekly" ? "Weekly" : "Monthly"}
              </span>
              <span className="text-sm text-muted">
                {c === "weekly"
                  ? "Same weekday each week"
                  : "Same date each month"}
              </span>
            </button>
          ))}
        </div>
        <div className="grid gap-4 sm:grid-cols-3">
          <label className="block space-y-1.5 sm:col-span-1">
            <span className="text-sm text-muted">Amount (₹)</span>
            <input
              className="field"
              inputMode="decimal"
              value={amount}
              onChange={(e) => {
                setAmount(e.target.value);
                setAmountTouched(true);
              }}
              required
            />
          </label>
          <label className="block space-y-1.5">
            <span className="text-sm text-muted">
              {existing ? "Next due" : "First due"}
            </span>
            <input
              className="field"
              type="date"
              min={today}
              value={first}
              onChange={(e) => setFirst(e.target.value)}
              required
            />
          </label>
          <label className="block space-y-1.5">
            <span className="text-sm text-muted">Grace (days)</span>
            <input
              className="field"
              inputMode="numeric"
              value={grace}
              onChange={(e) => setGrace(e.target.value)}
            />
          </label>
        </div>
        <label className="block space-y-1.5">
          <span className="text-sm text-muted">
            Email for invoices and reminders (optional)
          </span>
          <input
            className="field"
            type="email"
            placeholder="Leave empty to only track dues"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>
        <p className="text-sm text-muted">
          {email.trim()
            ? `Each due gets an invoice with a Paytm link and QR, emailed to ${email.trim()} 3 days ahead. ${graceNote(Number(grace) || 0)}`
            : "Without an email, Hisaab tracks each due and flags missed payments; nothing is sent."}
          {usual ? ` They usually pay ${inr(usual)} at a time.` : ""}
        </p>
        <label className="flex items-start gap-3">
          <input
            type="checkbox"
            className="mt-1 h-4 w-4 accent-blue"
            checked={callRemind}
            onChange={(e) => setCallRemind(e.target.checked)}
          />
          <span className="text-sm">
            <span className="block text-text">Call them if a due stays unpaid</span>
            <span className="text-muted">
              {email.trim()
                ? "Our voice agent rings them 2 days after the overdue email, and emails the payment link once they pick up."
                : "Our voice agent rings them 2 days after a due is missed."}
            </span>
          </span>
        </label>
        {callRemind ? (
          <label className="block space-y-1.5">
            <span className="text-sm text-muted">Phone to call</span>
            <input
              className="field"
              type="tel"
              placeholder="98765 43210"
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
            />
          </label>
        ) : null}
        {error ? (
          <p role="alert" className="text-sm text-rose">
            {error}
          </p>
        ) : null}
        <div className="flex justify-end gap-2">
          <button type="button" className="btn btn-quiet" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="btn btn-primary" disabled={busy}>
            {busy ? "Saving…" : existing ? "Save schedule" : "Mark recurring"}
          </button>
        </div>
      </form>
    </Dialog>
  );
}
