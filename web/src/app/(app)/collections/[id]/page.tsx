"use client";

import clsx from "clsx";
import {
  ArrowLeft,
  Check,
  Copy,
  ExternalLink,
  Phone,
  RefreshCw,
  Send,
  XCircle,
} from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { CallHistoryItem } from "@/components/call-status";
import { invoiceStatusLabel } from "@/components/invoice-status";
import { MarkPaid } from "@/components/mark-paid";
import { ErrorNote, Skeleton } from "@/components/ui";
import { ApiError, api, useApi } from "@/lib/api";
import { dateOnly, inr, istDate, istTime } from "@/lib/format";
import type { InvoiceDetail } from "@/lib/types";

function PaperInvoice({ inv }: { inv: InvoiceDetail }) {
  const paid = inv.status === "paid";
  return (
    <article
      aria-label={`Invoice ${inv.number}`}
      className={clsx(
        "relative overflow-hidden rounded-[20px] border border-line bg-paper p-6 text-paper-ink shadow-[0_8px_24px_rgb(31_27_22/0.06)] sm:p-8",
        paid && "pb-28 sm:pb-28",
      )}
    >
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="text-sm text-paper-muted">{inv.merchant_name}</p>
          <h1 className="text-[1.6rem] leading-tight font-semibold tracking-tight">
            Invoice {inv.number}
          </h1>
        </div>
        <div className="text-right text-sm text-paper-muted">
          <p>Issued {istDate(inv.created_at, true)}</p>
          <p>
            Due{" "}
            {dateOnly(inv.due_date, {
              day: "numeric",
              month: "short",
              year: "numeric",
            })}
          </p>
        </div>
      </header>

      <div className="mt-6 text-[0.95rem]">
        <p className="text-sm text-paper-muted">Bill to</p>
        <p className="font-semibold">{inv.payer_name}</p>
        {inv.email_to ? (
          <p className="text-paper-muted">{inv.email_to}</p>
        ) : null}
        {inv.gst_taxable ? (
          <p className="mt-1 font-mono text-sm">
            GSTIN {inv.payer_gstin ?? "not given"}
          </p>
        ) : null}
      </div>

      <table className="mt-6 w-full text-[0.95rem]">
        <thead>
          <tr className="border-b border-paper-ink/15 text-left text-sm text-paper-muted">
            <th className="py-2 font-normal">Item</th>
            <th className="py-2 text-right font-normal">Qty</th>
            <th className="py-2 text-right font-normal">Amount</th>
          </tr>
        </thead>
        <tbody>
          {inv.line_items.map((it, i) => (
            <tr key={i} className="border-b border-paper-ink/10">
              <td className="py-2.5">{it.name}</td>
              <td className="py-2.5 text-right">{it.qty}</td>
              <td className="num py-2.5 text-right font-mono">
                {inr(it.qty * it.unit_price_paise)}
              </td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <td className="pt-4 text-lg font-semibold" colSpan={2}>
              Total
            </td>
            <td className="num pt-4 text-right font-mono text-lg font-semibold">
              {inr(inv.amount_paise)}
            </td>
          </tr>
        </tfoot>
      </table>
      {inv.gst_taxable ? (
        <p className="mt-3 text-sm text-paper-muted">
          GST invoice (taxable supply).
        </p>
      ) : null}

      {paid ? (
        <div
          role="img"
          aria-label="Paid"
          className="paid-stamp pointer-events-none absolute right-8 bottom-8 rotate-[-10deg] rounded-xl border-[3px] border-emerald-600 px-4 py-1.5 text-3xl font-bold tracking-wider text-emerald-600 uppercase opacity-90 sm:right-10"
        >
          Paid
        </div>
      ) : null}
    </article>
  );
}

export default function InvoicePage() {
  const { id } = useParams<{ id: string }>();
  const {
    data: inv,
    error,
    isLoading,
    mutate,
  } = useApi<InvoiceDetail>(`/invoices/${id}`, {
    // While a call rings, check for its outcome (realtime usually gets there first).
    refreshInterval: (d) => (d?.last_call_status === "queued" ? 4000 : 0),
  });
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<{
    text: string;
    error?: boolean;
    muted?: boolean;
  } | null>(null);
  const [copied, setCopied] = useState(false);
  const [confirmCancel, setConfirmCancel] = useState(false);

  if (error)
    return (
      <div className="mx-auto max-w-5xl space-y-4">
        <BackLink />
        <ErrorNote
          message={
            error instanceof ApiError && [404, 422].includes(error.status)
              ? "This invoice doesn't exist."
              : error.message
          }
        />
      </div>
    );
  if (isLoading || !inv)
    return (
      <div className="mx-auto max-w-5xl space-y-4" aria-busy="true">
        <Skeleton className="h-6 w-32" />
        <Skeleton className="h-[520px] rounded-[22px]" />
      </div>
    );

  const status = invoiceStatusLabel(
    inv.status,
    inv.due_in_days,
    inv.grace_days,
  );
  const open = inv.status === "sent" || inv.status === "overdue";
  const reminderTo = inv.email_to ?? inv.payer_email;
  const calling = inv.last_call_status === "queued";
  // The first email of an invoice is the invoice itself (it failed, or was never sent).
  // Any email (invoice or overdue reminder) carries the same link and QR.
  const unsent =
    open &&
    Boolean(inv.email_to) &&
    !inv.sent_at &&
    !inv.reminders.some((r) => r.channel === "email");
  const failure = inv.paytm_raw?.last_failure;

  async function run(kind: "remind" | "call" | "check" | "cancel") {
    setBusy(kind);
    setNote(null);
    try {
      if (kind === "remind") {
        const r = await api<{ intended_to: string; delivered_to: string }>(
          `/invoices/${id}/remind`,
          { method: "POST" },
        );
        const copy =
          r.delivered_to !== r.intended_to
            ? ` (demo copy to ${r.delivered_to})`
            : "";
        setNote({
          text: `${unsent ? "Invoice" : "Reminder"} emailed to ${r.intended_to}${copy}.`,
        });
      } else if (kind === "call") {
        const r = await api<{ intended_to: string; delivered_to: string }>(
          `/invoices/${id}/call`,
          { method: "POST" },
        );
        const copy =
          r.delivered_to !== r.intended_to
            ? ` (demo call to ${r.delivered_to})`
            : "";
        setNote({
          text: `Calling ${inv?.payer_name} on ${r.intended_to}${copy}. How it went shows up in the history when the call ends.`,
        });
      } else if (kind === "check") {
        const r = await api<{ status: string }>(`/invoices/${id}/check`, {
          method: "POST",
        });
        setNote(
          r.status === "paid"
            ? { text: "Paytm confirms the payment. Marked paid." }
            : r.status === "needs_attention"
              ? {
                  text: "Paytm has a payment for this order that doesn't match the invoice amount. Check it in your Paytm dashboard.",
                  error: true,
                }
              : {
                  text: "Paytm has no payment for this invoice yet.",
                  muted: true,
                },
        );
      } else {
        await api(`/invoices/${id}/cancel`, { method: "POST" });
        setNote({ text: "Invoice cancelled." });
      }
      await mutate();
    } catch (e) {
      setNote({
        text: e instanceof Error ? e.message : "Something went wrong",
        error: true,
      });
    } finally {
      setBusy(null);
    }
  }

  async function copyLink() {
    if (!inv?.link_url) return;
    await navigator.clipboard.writeText(inv.link_url);
    setCopied(true);
    setTimeout(() => setCopied(false), 1800);
  }

  return (
    <div className="mx-auto max-w-5xl space-y-5">
      <BackLink />
      <div className="grid gap-6 @4xl:grid-cols-[minmax(0,1fr)_330px]">
        <PaperInvoice inv={inv} />

        <aside className="space-y-5">
          <section className="panel p-5">
            <p className="text-sm text-muted">Status</p>
            <p
              className={clsx("mt-0.5 text-2xl font-semibold", status.tone)}
              data-testid="invoice-status"
            >
              {inv.status === "paid"
                ? inv.payment?.source === "manual"
                  ? "Paid · marked by you"
                  : "Paid · auto-matched"
                : status.text}
            </p>
            {inv.payment ? (
              <p className="mt-1 text-sm text-muted">
                {inr(inv.payment.amount_paise)} via{" "}
                {inv.payment.payment_mode ?? "Paytm"} on{" "}
                {istDate(inv.payment.paid_at)}, {istTime(inv.payment.paid_at)}
              </p>
            ) : null}

            {unsent ? (
              <p className="mt-2 text-sm text-amber">
                Not emailed yet: the invoice email to {inv.email_to} hasn&apos;t
                gone out.
              </p>
            ) : null}
            {open ? (
              <div className="mt-4 flex flex-col gap-2">
                <button
                  className="btn btn-primary"
                  onClick={() => run("remind")}
                  disabled={busy !== null || !reminderTo}
                >
                  <Send size={16} aria-hidden />{" "}
                  {busy === "remind"
                    ? "Sending…"
                    : unsent
                      ? "Email invoice"
                      : "Send reminder"}
                </button>
                <button
                  className="btn btn-quiet"
                  onClick={() => run("call")}
                  disabled={busy !== null || !inv.payer_phone || calling}
                >
                  <Phone size={16} aria-hidden />{" "}
                  {busy === "call"
                    ? "Starting call…"
                    : calling
                      ? "Calling…"
                      : "Call customer"}
                </button>
                <button
                  className="btn btn-quiet"
                  onClick={() => run("check")}
                  disabled={busy !== null}
                >
                  <RefreshCw size={16} aria-hidden />{" "}
                  {busy === "check" ? "Checking with Paytm…" : "Check payment"}
                </button>
                <MarkPaid
                  invoiceId={inv.id}
                  number={inv.number}
                  amountPaise={inv.amount_paise}
                  onDone={() => mutate()}
                />
                {!reminderTo ? (
                  <p className="text-sm text-muted">
                    Add an email to {inv.payer_name} to send reminders.
                  </p>
                ) : null}
                {!inv.payer_phone ? (
                  <p className="text-sm text-muted">
                    Add a phone number to {inv.payer_name} to call them.
                  </p>
                ) : inv.call_remind ? (
                  <p className="text-sm text-muted">
                    If it stays unpaid after the overdue email, the agent calls{" "}
                    {inv.payer_name} automatically.
                  </p>
                ) : null}
              </div>
            ) : null}
            {inv.status === "cancelled" ? (
              <div className="mt-4 flex flex-col gap-2">
                <p className="text-sm text-muted">
                  If they paid before you cancelled and Paytm&apos;s
                  notification never came, checking records it.
                </p>
                <button
                  className="btn btn-quiet"
                  onClick={() => run("check")}
                  disabled={busy !== null}
                >
                  <RefreshCw size={16} aria-hidden />{" "}
                  {busy === "check" ? "Checking with Paytm…" : "Check payment"}
                </button>
              </div>
            ) : null}
            {note ? (
              <p
                role="status"
                className={clsx(
                  "mt-3 text-sm",
                  note.error
                    ? "text-rose"
                    : note.muted
                      ? "text-muted"
                      : "text-mint",
                )}
              >
                {note.text}
              </p>
            ) : null}
          </section>

          {inv.link_url && !open ? (
            <section className="panel p-5 text-sm text-muted">
              Payment link closed: this invoice is{" "}
              {inv.status === "paid" ? "paid" : "cancelled"}.
              <p className="mt-2 font-mono text-xs break-all text-faint">
                Order {inv.paytm_order_id}
              </p>
            </section>
          ) : null}
          {inv.link_url && open ? (
            <section className="panel p-5">
              <p className="text-sm text-muted">Pay with any UPI app</p>
              {inv.qr_image_url ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={inv.qr_image_url}
                  alt={`UPI QR code for ${inr(inv.amount_paise)}`}
                  width={180}
                  height={180}
                  className="mx-auto my-3 rounded-xl bg-white p-2"
                />
              ) : null}
              <div className="flex items-center gap-2">
                <a
                  href={inv.link_url}
                  target="_blank"
                  rel="noreferrer"
                  className="btn btn-quiet flex-1 text-sm"
                >
                  <ExternalLink size={15} aria-hidden /> Open payment link
                </a>
                <button
                  onClick={copyLink}
                  className="btn btn-quiet text-sm"
                  aria-label="Copy payment link"
                >
                  {copied ? <Check size={15} /> : <Copy size={15} />}
                </button>
              </div>
              <p className="mt-3 font-mono text-xs break-all text-faint">
                Order {inv.paytm_order_id}
              </p>
            </section>
          ) : null}

          <section className="panel p-5">
            <p className="mb-3 text-sm text-muted">History</p>
            <ol className="space-y-3 text-sm">
              <li>
                <span className="text-text">Created</span>{" "}
                <span className="text-muted">
                  {istDate(inv.created_at)}, {istTime(inv.created_at)}
                </span>
              </li>
              {inv.reminders.map((r, i) =>
                r.channel === "call" ? (
                  <CallHistoryItem
                    key={r.id}
                    r={r}
                    emails={inv.reminders.filter(
                      (e) => e.sent_during_call === r.id,
                    )}
                  />
                ) : r.sent_during_call ? null : (
                  <li key={r.id ?? i}>
                    <span className="text-text">
                      {r.kind === "overdue"
                        ? "Overdue reminder emailed"
                        : "Invoice emailed"}
                    </span>{" "}
                    <span className="text-muted">
                      {istDate(r.sent_at)}, {istTime(r.sent_at)}
                      {r.to_email ? ` to ${r.to_email}` : ""}
                    </span>
                  </li>
                ),
              )}
              {failure && inv.status !== "paid" ? (
                <li>
                  <span className="text-amber">Payment attempt failed</span>{" "}
                  <span className="text-muted">
                    {failure.TXNDATE
                      ? dateOnly(failure.TXNDATE.slice(0, 10))
                      : ""}
                    {failure.RESPMSG ? `: ${failure.RESPMSG}` : ""}
                  </span>
                </li>
              ) : null}
              {inv.payment ? (
                <li>
                  <span className="text-mint">
                    {inv.payment.source === "manual"
                      ? "Marked paid by you"
                      : "Paid and matched"}
                  </span>{" "}
                  <span className="text-muted">
                    {istDate(inv.payment.paid_at)},{" "}
                    {istTime(inv.payment.paid_at)}
                  </span>
                  {inv.payment.paytm_txn_id ? (
                    <span className="block font-mono text-xs break-all text-faint">
                      Txn {inv.payment.paytm_txn_id}
                    </span>
                  ) : null}
                </li>
              ) : null}
              {inv.status === "cancelled" ? (
                <li className="text-faint">Cancelled</li>
              ) : null}
            </ol>
            {open && !confirmCancel ? (
              <button
                className="mt-4 inline-flex items-center gap-1.5 text-sm text-muted hover:text-rose"
                onClick={() => setConfirmCancel(true)}
                disabled={busy !== null}
              >
                <XCircle size={14} aria-hidden /> Cancel invoice
              </button>
            ) : null}
            {open && confirmCancel ? (
              <div className="mt-4 space-y-2 rounded-xl border border-rose/30 p-3 text-sm">
                <p>
                  Cancel {inv.number}? Its payment link stops taking payments
                  and it won’t count as outstanding.
                </p>
                <div className="flex gap-2">
                  <button
                    className="btn btn-quiet px-3 py-1.5 text-sm"
                    onClick={() => setConfirmCancel(false)}
                  >
                    Keep it
                  </button>
                  <button
                    className="btn px-3 py-1.5 text-sm text-rose ring-1 ring-rose/40 hover:bg-rose/10"
                    onClick={() => {
                      setConfirmCancel(false);
                      run("cancel");
                    }}
                  >
                    Cancel invoice
                  </button>
                </div>
              </div>
            ) : null}
          </section>
        </aside>
      </div>
    </div>
  );
}

function BackLink() {
  return (
    <Link
      href="/collections"
      className="inline-flex items-center gap-1.5 text-muted hover:text-text"
    >
      <ArrowLeft size={16} aria-hidden /> Collections
    </Link>
  );
}
