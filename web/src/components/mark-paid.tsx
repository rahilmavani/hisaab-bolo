"use client";

import { CheckCheck } from "lucide-react";
import { useState } from "react";
import { api } from "@/lib/api";
import { inr } from "@/lib/format";

/** Money that reached the shop outside Paytm (straight to its UPI ID, or cash) sends no notification,
 * so the merchant confirms it here. */
export function MarkPaid({
  invoiceId,
  number,
  amountPaise,
  onDone,
}: {
  invoiceId: string;
  number: string;
  amountPaise: number;
  onDone: () => Promise<unknown>;
}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function markPaid() {
    setBusy(true);
    setError(null);
    try {
      await api(`/invoices/${invoiceId}/mark-paid`, { method: "POST" });
      setConfirming(false);
      await onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't mark it paid");
    } finally {
      setBusy(false);
    }
  }

  if (!confirming) {
    return (
      <button className="btn btn-quiet" onClick={() => setConfirming(true)}>
        <CheckCheck size={16} aria-hidden /> Mark as paid
      </button>
    );
  }
  return (
    <div className="space-y-2 rounded-xl border border-line p-3 text-sm">
      <p>
        Mark {number} paid ({inr(amountPaise)})? Use this when the money reached you outside Paytm, for example
        straight to your UPI ID.
      </p>
      <div className="flex gap-2">
        <button className="btn btn-primary px-3 py-1.5 text-sm" onClick={markPaid} disabled={busy}>
          {busy ? "Saving…" : "Yes, it's paid"}
        </button>
        <button className="btn btn-quiet px-3 py-1.5 text-sm" onClick={() => setConfirming(false)} disabled={busy}>
          Not yet
        </button>
      </div>
      {error ? (
        <p role="alert" className="text-rose">
          {error}
        </p>
      ) : null}
    </div>
  );
}
