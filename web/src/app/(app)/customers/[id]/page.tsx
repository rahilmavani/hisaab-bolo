"use client";

import { ArrowLeft, FilePlus2, Merge, PencilLine } from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useMe } from "@/components/app-shell";
import { MonthlyBars } from "@/components/charts";
import { InvoiceStatusText } from "@/components/invoice-status";
import { NewInvoiceDialog } from "@/components/new-invoice-dialog";
import { EditPayerDialog, MergeDialog } from "@/components/payer-dialogs";
import { PaymentsTable } from "@/components/payments-table";
import { RecurringCard } from "@/components/recurring-card";
import { Avatar, Badge, ErrorNote, SectionTitle, Skeleton, StatTile } from "@/components/ui";
import { ApiError, useApi } from "@/lib/api";
import { dateOnly, inr, istDate, istISODate } from "@/lib/format";
import type { PayerProfile, Txn } from "@/lib/types";

const KIND_LABEL = { vpa: "UPI ID", phone: "Phone", email: "Email", paytm_cust_id: "Paytm customer ID" } as const;

export default function PayerPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { data: p, error, isLoading, mutate } = useApi<PayerProfile>(`/payers/${id}`);
  const [editOpen, setEditOpen] = useState(false);
  const [mergeOpen, setMergeOpen] = useState(false);
  const [invoiceOpen, setInvoiceOpen] = useState(false);
  const { data: me } = useMe();

  useEffect(() => {
    if (p?.merged_into) router.replace(`/customers/${p.merged_into}`);
  }, [p?.merged_into, router]);

  if (error) {
    return (
      <div className="mx-auto max-w-5xl space-y-4">
        <BackLink />
        <ErrorNote
          message={error instanceof ApiError && [404, 422].includes(error.status) ? "This customer doesn't exist in your ledger." : error.message}
          onRetry={error instanceof ApiError && [404, 422].includes(error.status) ? undefined : () => mutate()}
        />
      </div>
    );
  }
  if (isLoading || !p) {
    return (
      <div className="mx-auto max-w-5xl space-y-6" aria-busy="true">
        <Skeleton className="h-8 w-32" />
        <Skeleton className="h-48 rounded-[28px]" />
        <Skeleton className="h-80" />
      </div>
    );
  }

  const updateTxn = (t: Txn) =>
    mutate({ ...p, transactions: p.transactions.map((x) => (x.id === t.id ? { ...x, ...t } : x)) }, { revalidate: false });
  const lastPaidDays = p.days_since_last === null ? null : Math.floor(p.days_since_last);

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <BackLink />

      <section className="glass p-5 sm:p-7" aria-labelledby="payer-name">
        <div className="flex flex-wrap items-start gap-5">
          <Avatar name={p.display_name} size={64} />
          <div className="min-w-[15rem] flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h1 id="payer-name" className="text-[1.8rem] leading-tight font-semibold tracking-tight">
                {p.display_name}
              </h1>
              {p.badges.map((b) => (
                <Badge key={b} label={b} />
              ))}
            </div>
            <p className="mt-1 text-[1.08rem] text-sky">{p.habit}</p>
            <p className="mt-1 flex flex-wrap gap-x-1.5 text-sm break-all text-muted">
              <span>{[p.vpa, p.phone, p.email].filter(Boolean).join(" · ") || "No contact details yet"}</span>
              {p.gstin ? <span className="font-mono whitespace-nowrap">· GSTIN {p.gstin}</span> : null}
            </p>
          </div>
          {/* Beside the name when there's room; on its own line (buttons wrapping) when there isn't. */}
          <div className="flex max-w-full min-w-0 flex-wrap gap-2">
            <button className="btn btn-primary" onClick={() => setInvoiceOpen(true)} disabled={!me}>
              <FilePlus2 size={16} aria-hidden /> New invoice
            </button>
            <button className="btn btn-quiet" onClick={() => setEditOpen(true)}>
              <PencilLine size={16} aria-hidden /> Edit details
            </button>
            <button className="btn btn-quiet" onClick={() => setMergeOpen(true)}>
              <Merge size={16} aria-hidden /> Merge duplicate
            </button>
          </div>
        </div>

        {p.lost ? (
          <p role="status" className="mt-5 rounded-2xl border border-amber/25 bg-amber/[0.07] px-4 py-3 text-[0.97rem]">
            <span className="text-amber">Gone quiet.</span> Last paid {lastPaidDays} days ago; they usually pay every{" "}
            {Math.round(p.median_gap_days ?? 0)} day{Math.round(p.median_gap_days ?? 0) === 1 ? "" : "s"}.
          </p>
        ) : null}

        <div className="mt-6 grid grid-cols-2 gap-5 border-t border-line pt-5 md:grid-cols-4">
          <StatTile label="Total paid" value={inr(p.total_paise)} />
          <StatTile label="Payments" value={p.txn_count.toLocaleString("en-IN")} />
          <StatTile label="Usual amount" value={p.median_amount_paise ? inr(p.median_amount_paise) : "—"} />
          <StatTile
            label="Last paid"
            value={p.last_paid_at ? istDate(p.last_paid_at) : "—"}
            sub={p.typical_time ? `Usually around ${p.typical_time}` : undefined}
          />
        </div>
      </section>

      <div className="grid grid-cols-1 gap-6 @4xl:grid-cols-[minmax(0,1fr)_320px]">
        <section className="panel min-w-0 p-5 sm:p-6">
          <SectionTitle title="Payments" sub={`${p.txn_count} in total, newest first`} />
          <PaymentsTable txns={p.transactions} onChange={updateTxn} />
        </section>

        <div className="min-w-0 space-y-6">
          {me ? (
            <RecurringCard
              payer={p}
              today={istISODate(me.now)}
              onChange={(np) => (np ? mutate(np, { revalidate: false }) : mutate())}
            />
          ) : null}

          <section className="panel p-5 sm:p-6">
            <SectionTitle title="Last 6 months" />
            <MonthlyBars months={p.monthly} />
          </section>

          <section className="panel p-5 sm:p-6">
            <SectionTitle title="How we recognise them" sub="Payments from any of these land in this ledger" />
            <ul className="space-y-2 text-[0.95rem]">
              {p.identifiers.map((i) => (
                <li key={`${i.kind}-${i.value}`} className="flex justify-between gap-3">
                  <span className="text-muted">{KIND_LABEL[i.kind]}</span>
                  <span className="truncate font-mono text-sm">{i.value}</span>
                </li>
              ))}
              {!p.identifiers.length ? <li className="text-muted">None yet.</li> : null}
            </ul>
          </section>

          <section className="panel p-5 sm:p-6">
            <SectionTitle title="Invoices" />
            {p.invoices.length ? (
              <ul className="divide-y divide-line">
                {p.invoices.map((inv) => (
                  <li key={inv.id} className="flex items-center justify-between gap-3 py-2.5 text-[0.95rem]">
                    <span>
                      <Link href={`/collections/${inv.id}`} className="font-mono text-sm hover:text-sky">
                        {inv.number}
                      </Link>
                      <span className="block text-sm text-muted">Due {dateOnly(inv.due_date)}</span>
                    </span>
                    <span className="text-right">
                      <span className="num block font-mono">{inr(inv.amount_paise)}</span>
                      <InvoiceStatusText
                        className="text-sm"
                        status={inv.status}
                        dueInDays={inv.due_in_days}
                        graceDays={inv.grace_days}
                      />
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-muted">No invoices for this customer.</p>
            )}
          </section>

          {p.notes ? (
            <section className="panel p-5 sm:p-6">
              <SectionTitle title="Notes" />
              <p className="whitespace-pre-line text-muted">{p.notes}</p>
            </section>
          ) : null}
        </div>
      </div>

      <EditPayerDialog
        key={`edit-${p.id}-${editOpen}`}
        payer={p}
        open={editOpen}
        onClose={() => setEditOpen(false)}
        onSaved={(np) => mutate(np, { revalidate: false })}
      />
      {me ? (
        <NewInvoiceDialog
          key={`invoice-${p.id}-${invoiceOpen}`}
          open={invoiceOpen}
          onClose={() => setInvoiceOpen(false)}
          today={istISODate(me.now)}
          presetPayer={p}
        />
      ) : null}
      <MergeDialog
        key={`merge-${p.id}-${mergeOpen}`}
        payer={p}
        open={mergeOpen}
        onClose={() => setMergeOpen(false)}
        onMerged={(np) => mutate(np, { revalidate: false })}
      />
    </div>
  );
}

function BackLink() {
  return (
    <Link href="/customers" className="inline-flex items-center gap-1.5 text-muted hover:text-text">
      <ArrowLeft size={16} aria-hidden /> Customers
    </Link>
  );
}
