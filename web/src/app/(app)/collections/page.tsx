"use client";

import { Phone, Plus, RefreshCw, Send } from "lucide-react";
import Link from "next/link";
import { Fragment, useState } from "react";
import { useMe } from "@/components/app-shell";
import { callOutcomeText, callStatusLabel } from "@/components/call-status";
import { InvoiceStatusText, isOverdue } from "@/components/invoice-status";
import { NewInvoiceDialog } from "@/components/new-invoice-dialog";
import { ErrorNote, SectionTitle, Skeleton } from "@/components/ui";
import { api, useApi } from "@/lib/api";
import { inr, istISODate, relDate } from "@/lib/format";
import type { Invoice, InvoiceList } from "@/lib/types";

type RowState = {
  busy?: "remind" | "call" | "check";
  note?: string;
  error?: boolean;
  muted?: boolean;
};

export default function CollectionsPage() {
  const { data: me } = useMe();
  const { data, error, isLoading, mutate } = useApi<InvoiceList>(
    "/invoices?status=all",
    { refreshInterval: 30_000 },
  );
  const [creating, setCreating] = useState(false);
  const [rowState, setRowState] = useState<Record<string, RowState>>({});
  const [bulk, setBulk] = useState<string | null>(null);

  const open = (data?.invoices ?? []).filter(
    (i) => i.status === "sent" || i.status === "overdue",
  );
  const paid = (data?.invoices ?? [])
    .filter((i) => i.status === "paid")
    .sort((a, b) => (b.paid_at ?? "").localeCompare(a.paid_at ?? ""));
  const remindable = open.filter(
    (i) =>
      isOverdue(i.status, i.due_in_days, i.grace_days) &&
      (i.email_to || i.payer_email),
  );

  async function act(inv: Invoice, kind: "remind" | "call" | "check") {
    setRowState((s) => ({ ...s, [inv.id]: { busy: kind } }));
    try {
      if (kind === "remind") {
        const r = await api<{ intended_to: string }>(
          `/invoices/${inv.id}/remind`,
          { method: "POST" },
        );
        setRowState((s) => ({
          ...s,
          [inv.id]: { note: `Reminder sent to ${r.intended_to}` },
        }));
      } else if (kind === "call") {
        const r = await api<{ intended_to: string }>(
          `/invoices/${inv.id}/call`,
          { method: "POST" },
        );
        setRowState((s) => ({
          ...s,
          [inv.id]: { note: `Calling ${r.intended_to}…` },
        }));
      } else {
        const r = await api<{ status: string }>(`/invoices/${inv.id}/check`, {
          method: "POST",
        });
        setRowState((s) => ({
          ...s,
          [inv.id]:
            r.status === "paid"
              ? { note: "Payment found and matched" }
              : r.status === "needs_attention"
                ? {
                    note: "Paytm has a payment that doesn't match this invoice",
                    error: true,
                  }
                : { note: "No payment yet", muted: true },
        }));
      }
      mutate();
    } catch (e) {
      setRowState((s) => ({
        ...s,
        [inv.id]: {
          note: e instanceof Error ? e.message : "Failed",
          error: true,
        },
      }));
    }
  }

  async function remindAll() {
    setBulk("Sending…");
    let sent = 0;
    for (const inv of remindable) {
      try {
        await api(`/invoices/${inv.id}/remind`, { method: "POST" });
        sent++;
      } catch {
        /* reported per row below on retry */
      }
    }
    setBulk(`${sent} reminder${sent === 1 ? "" : "s"} sent`);
    mutate();
  }

  if (error)
    return (
      <ErrorNote
        message={`Couldn't load invoices: ${error.message}`}
        onRetry={() => mutate()}
      />
    );

  // Row actions: a column of their own when there's room; a full-width row under the invoice on phones.
  const rowActions = (inv: (typeof open)[number], st: RowState) => (
    <>
      <div className="flex flex-wrap gap-2 @xl:justify-end">
        <button
          className="btn btn-quiet px-3 py-1.5 text-sm"
          onClick={() => act(inv, "remind")}
          disabled={Boolean(st.busy) || !(inv.email_to || inv.payer_email)}
          title={
            inv.email_to || inv.payer_email
              ? undefined
              : "Add an email to this customer first"
          }
        >
          <Send size={14} aria-hidden />{" "}
          {st.busy === "remind" ? "Sending…" : "Send reminder"}
        </button>
        <button
          className="btn btn-quiet px-3 py-1.5 text-sm"
          onClick={() => act(inv, "call")}
          disabled={
            Boolean(st.busy) ||
            !inv.payer_phone ||
            inv.last_call_status === "queued"
          }
          title={inv.payer_phone ? undefined : "Add a phone number to this customer first"}
        >
          <Phone size={14} aria-hidden />{" "}
          {st.busy === "call"
            ? "Starting…"
            : inv.last_call_status === "queued"
              ? "Calling…"
              : "Call"}
        </button>
        <button
          className="btn btn-quiet px-3 py-1.5 text-sm"
          onClick={() => act(inv, "check")}
          disabled={Boolean(st.busy)}
        >
          <RefreshCw size={14} aria-hidden />{" "}
          {st.busy === "check" ? "Checking…" : "Check payment"}
        </button>
      </div>
      {st.note ? (
        <p
          role="status"
          className={`mt-1 text-xs ${st.error ? "text-rose" : st.muted ? "text-muted" : "text-mint"}`}
        >
          {st.note}
        </p>
      ) : null}
    </>
  );

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-3xl leading-tight font-semibold tracking-tight">
            Collections
          </h1>
          <p className="text-muted">
            Invoices with a Paytm link and QR. They mark themselves paid when
            the money lands.
          </p>
        </div>
        <button
          className="btn btn-primary"
          onClick={() => setCreating(true)}
          disabled={!me}
        >
          <Plus size={17} aria-hidden /> New invoice
        </button>
      </header>

      <section className="panel p-5 sm:p-7">
        <SectionTitle
          title="Pending payments"
          sub={
            isLoading || !data
              ? "Loading…"
              : open.length
                ? `${inr(data.outstanding_paise)} outstanding from ${data.outstanding_customers} customer${data.outstanding_customers === 1 ? "" : "s"}`
                : "Nothing outstanding"
          }
        />
        {isLoading || !data ? (
          <Skeleton className="h-40" />
        ) : open.length ? (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm @xl:min-w-[560px]">
              <thead className="text-sm text-muted">
                <tr className="border-b border-line">
                  <th className="py-2 font-normal">Customer</th>
                  <th className="py-2 pr-6 text-right font-normal">Amount</th>
                  <th className="py-2 font-normal">Status</th>
                  <th className="hidden py-2 font-normal @xl:table-cell">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {open.map((inv) => {
                  const st = rowState[inv.id] ?? {};
                  return (
                    <Fragment key={inv.id}>
                      <tr className="align-top @xl:border-b @xl:border-line @xl:last:border-0">
                        <td className="py-3">
                          <Link
                            href={`/collections/${inv.id}`}
                            className="font-medium hover:text-blue"
                          >
                            {inv.payer_name}
                          </Link>
                          <span className="block text-xs text-faint">
                            {inv.number}
                          </span>
                        </td>
                        <td className="num py-3 pr-6 text-right">
                          {inr(inv.amount_paise)}
                        </td>
                        <td className="py-3">
                          <InvoiceStatusText
                            status={inv.status}
                            dueInDays={inv.due_in_days}
                            graceDays={inv.grace_days}
                          />
                          <span className="block text-xs text-faint">
                            {inv.last_reminded_at
                              ? `Emailed ${relDate(inv.last_reminded_at)}`
                              : "Not emailed"}
                          </span>
                          {inv.last_called_at ? (
                            <span className="block text-xs text-faint">
                              Called {relDate(inv.last_called_at)}:{" "}
                              {(
                                callOutcomeText(inv.last_call_outcome, inv.last_call_promised_date) ??
                                callStatusLabel(inv.last_call_status).text
                              ).toLowerCase()}
                            </span>
                          ) : null}
                        </td>
                        <td className="hidden py-3 text-right @xl:table-cell">
                          {rowActions(inv, st)}
                        </td>
                      </tr>
                      <tr className="border-b border-line @xl:hidden">
                        <td colSpan={3} className="pb-3">
                          {rowActions(inv, st)}
                        </td>
                      </tr>
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="flex flex-wrap items-center gap-3">
            <p className="text-muted">No unpaid invoices.</p>
            <button className="btn btn-quiet px-3 py-1.5 text-sm" onClick={() => setCreating(true)} disabled={!me}>
              <Plus size={14} aria-hidden /> New invoice
            </button>
          </div>
        )}
        {remindable.length ? (
          <div className="mt-5 flex flex-wrap items-center gap-3">
            <button
              className="btn btn-primary"
              onClick={remindAll}
              disabled={bulk === "Sending…"}
            >
              <Send size={16} aria-hidden /> Send {remindable.length} overdue
              reminder{remindable.length === 1 ? "" : "s"}
            </button>
            <span className="text-sm text-muted" role="status">
              {bulk}
            </span>
          </div>
        ) : null}
      </section>

      <section className="panel p-5 sm:p-6">
        <SectionTitle
          title="Paid"
          sub="Matched automatically to the Paytm payment"
        />
        {paid.length ? (
          <ul className="divide-y divide-line">
            {paid.map((inv) => (
              <li key={inv.id}>
                <Link
                  href={`/collections/${inv.id}`}
                  className="flex items-center justify-between gap-3 py-3 hover:text-blue"
                >
                  <span>
                    {inv.payer_name}
                    <span className="block text-xs text-faint">
                      {inv.number}
                    </span>
                  </span>
                  <span className="text-right">
                    <span className="num block">
                      {inr(inv.amount_paise)}
                    </span>
                    <span className="text-sm text-mint">
                      Paid {inv.paid_at ? relDate(inv.paid_at) : ""}
                    </span>
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-muted">No paid invoices yet.</p>
        )}
      </section>

      {me ? (
        <NewInvoiceDialog
          key={String(creating)}
          open={creating}
          onClose={() => setCreating(false)}
          today={istISODate(me.now)}
        />
      ) : null}
    </div>
  );
}
