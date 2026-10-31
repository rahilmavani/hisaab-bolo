"use client";

import { CheckCircle2, FlaskConical, XCircle } from "lucide-react";
import { useParams } from "next/navigation";
import { Suspense, useState } from "react";
import useSWR from "swr";
import { API_BASE } from "@/lib/api";
import { dateOnly, inr, istDate, istTime } from "@/lib/format";
import type { LineItem } from "@/lib/types";

type Order = {
  order_id: string;
  status: "PENDING" | "TXN_SUCCESS" | "TXN_FAILURE";
  invoice_status: string | null;
  amount_paise: number;
  merchant_name: string | null;
  invoice_number: string | null;
  payer_name: string | null;
  due_date: string | null;
  line_items: LineItem[];
  txn_id: string | null;
  paid_at: string | null;
};

type Outcome = "success" | "failure" | "success_no_webhook";

function MockPay() {
  const { orderId } = useParams<{ orderId: string }>();
  const { data: order, error: loadError, mutate } = useSWR<Order>(`mock-pay:${orderId}`, async () => {
    const r = await fetch(`${API_BASE}/mock-pay/${encodeURIComponent(orderId)}`);
    if (!r.ok) {
      throw new Error(r.status === 404 ? "This payment link doesn't exist or has expired." : `Couldn't load the payment (${r.status}).`);
    }
    return r.json();
  });
  const [payError, setPayError] = useState<string | null>(null);
  const [busy, setBusy] = useState<Outcome | null>(null);
  const [result, setResult] = useState<{ status: string; txn_id: string; webhook_sent: boolean } | null>(null);
  const error = payError ?? (loadError ? (loadError as Error).message : null);

  async function pay(outcome: Outcome) {
    setBusy(outcome);
    setPayError(null);
    try {
      const r = await fetch(`${API_BASE}/mock-pay/${encodeURIComponent(orderId)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ outcome }),
      });
      const body = await r.json();
      if (!r.ok) throw new Error(body.detail ?? `Payment failed (${r.status})`);
      setResult(body);
      await mutate();
    } catch (e) {
      setPayError(e instanceof Error ? e.message : "Payment failed");
    } finally {
      setBusy(null);
    }
  }

  return (
    <main className="min-h-dvh bg-[#eef2fa] px-4 py-8 text-paper-ink sm:py-14" style={{ colorScheme: "light" }}>
      <div className="mx-auto w-full max-w-md space-y-4">
        <p className="flex items-center justify-center gap-2 rounded-full bg-amber-100 px-4 py-2 text-sm text-amber-900">
          <FlaskConical size={16} aria-hidden /> Test payment gateway. No real money moves.
        </p>

        <section className="rounded-[22px] bg-white p-6 shadow-[0_20px_60px_-30px_rgba(15,26,51,0.35)] sm:p-7">
          {error && !order ? (
            <p role="alert" className="text-rose-700">
              {error}
            </p>
          ) : !order ? (
            <p className="text-paper-muted" aria-busy="true">
              Loading payment…
            </p>
          ) : (
            <>
              <p className="text-sm text-paper-muted">Paying {order.merchant_name ?? "merchant"}</p>
              <h1 className="mt-1 text-[2.4rem] leading-none font-semibold tracking-tight">{inr(order.amount_paise)}</h1>
              <p className="mt-2 text-paper-muted">
                {order.invoice_number ? `Invoice ${order.invoice_number}` : "Payment"}
                {order.payer_name ? ` for ${order.payer_name}` : ""}
                {order.due_date ? ` · due ${dateOnly(order.due_date)}` : ""}
              </p>

              {order.line_items.length ? (
                <ul className="mt-5 divide-y divide-paper-ink/10 border-y border-paper-ink/10 text-[0.95rem]">
                  {order.line_items.map((it, i) => (
                    <li key={i} className="flex justify-between gap-3 py-2">
                      <span>
                        {it.name}
                        {it.qty !== 1 ? ` × ${it.qty}` : ""}
                      </span>
                      <span className="font-mono">{inr(it.qty * it.unit_price_paise)}</span>
                    </li>
                  ))}
                </ul>
              ) : null}

              {order.invoice_status === "cancelled" && order.status !== "TXN_SUCCESS" ? (
                <p role="status" className="mt-6 rounded-2xl bg-paper-ink/5 p-4">
                  This invoice was cancelled by {order.merchant_name ?? "the merchant"}. There’s nothing to pay.
                </p>
              ) : order.status === "TXN_SUCCESS" ? (
                <div role="status" className="mt-6 rounded-2xl bg-emerald-50 p-4 text-emerald-900">
                  <p className="flex items-center gap-2 font-semibold">
                    <CheckCircle2 size={20} aria-hidden /> Payment successful
                  </p>
                  {order.paid_at ? (
                    <p className="mt-1 text-sm">
                      {istDate(order.paid_at)}, {istTime(order.paid_at)}
                    </p>
                  ) : null}
                  {order.txn_id ? <p className="mt-1 font-mono text-xs break-all">Txn {order.txn_id}</p> : null}
                  {result && !result.webhook_sent ? (
                    <p className="mt-2 text-sm">
                      The merchant wasn’t notified (webhook dropped on purpose). Their “Check payment” button or the
                      5-minute check will pick it up.
                    </p>
                  ) : (
                    <p className="mt-2 text-sm">You can close this page.</p>
                  )}
                </div>
              ) : (
                <div className="mt-6 space-y-2.5">
                  {order.status === "TXN_FAILURE" ? (
                    <p role="status" className="flex items-center gap-2 text-sm text-rose-700">
                      <XCircle size={16} aria-hidden /> The last attempt failed. You can try again.
                    </p>
                  ) : null}
                  <button
                    onClick={() => pay("success")}
                    disabled={busy !== null}
                    className="w-full rounded-full bg-[#2f6fed] px-5 py-3.5 text-lg font-semibold text-white hover:bg-[#4580f7] disabled:opacity-50"
                  >
                    {busy === "success" ? "Paying…" : `Pay ${inr(order.amount_paise)}`}
                  </button>
                  <button
                    onClick={() => pay("failure")}
                    disabled={busy !== null}
                    className="w-full rounded-full border border-paper-ink/20 px-5 py-3 font-medium hover:bg-paper-ink/5 disabled:opacity-50"
                  >
                    {busy === "failure" ? "Declining…" : "Simulate a failed payment"}
                  </button>
                  <button
                    onClick={() => pay("success_no_webhook")}
                    disabled={busy !== null}
                    className="w-full px-5 py-2 text-sm text-paper-muted underline-offset-2 hover:underline disabled:opacity-50"
                  >
                    Pay, but drop the merchant notification
                  </button>
                  {error ? (
                    <p role="alert" className="text-sm text-rose-700">
                      {error}
                    </p>
                  ) : null}
                </div>
              )}
            </>
          )}
        </section>
        <p className="text-center font-mono text-xs break-all text-paper-muted">Order {orderId}</p>
      </div>
    </main>
  );
}

export default function MockPayPage() {
  return (
    <Suspense>
      <MockPay />
    </Suspense>
  );
}
