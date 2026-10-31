"use client";

import { Check, Pencil, Sparkles, X } from "lucide-react";
import { useState, type FormEvent } from "react";
import { api } from "@/lib/api";
import { inr, istDate, istTime } from "@/lib/format";
import type { Txn } from "@/lib/types";

function DescriptionCell({ txn, onSaved }: { txn: Txn; onSaved: (t: Txn) => void }) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(txn.description ?? "");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function save(e: FormEvent) {
    e.preventDefault();
    const description = value.trim();
    if (!description) {
      setError("Write a few words, or press Esc to cancel.");
      return;
    }
    setBusy(true);
    try {
      const updated = await api<Txn>(`/transactions/${txn.id}`, {
        method: "PATCH",
        body: JSON.stringify({ description }),
      });
      onSaved(updated);
      setEditing(false);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't save");
    } finally {
      setBusy(false);
    }
  }

  if (editing) {
    return (
      <form onSubmit={save} className="flex items-center gap-1.5">
        <input
          autoFocus
          aria-label="Payment description"
          value={value}
          maxLength={120}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              setEditing(false);
              setValue(txn.description ?? "");
              setError(null);
            }
          }}
          className="field py-1.5 text-sm"
        />
        <button type="submit" disabled={busy} aria-label="Save description" className="rounded-full p-1.5 text-mint hover:bg-raised">
          <Check size={16} />
        </button>
        <button
          type="button"
          aria-label="Cancel editing"
          onClick={() => {
            setEditing(false);
            setValue(txn.description ?? "");
            setError(null);
          }}
          className="rounded-full p-1.5 text-muted hover:bg-raised"
        >
          <X size={16} />
        </button>
        {error ? <span className="text-xs text-rose">{error}</span> : null}
      </form>
    );
  }

  return (
    <div className="group flex items-center gap-2">
      <span className={txn.description ? "text-text" : "text-faint"}>{txn.description ?? "No description yet"}</span>
      {txn.description_by === "ai" ? (
        <Sparkles size={13} className="shrink-0 text-blue" aria-label="Written by Hisaab AI" />
      ) : null}
      <button
        onClick={() => setEditing(true)}
        aria-label={`Edit description for ${inr(txn.amount_paise)} on ${istDate(txn.paid_at)}`}
        className="rounded-full p-1 text-faint opacity-60 group-hover:opacity-100 hover:text-text focus-visible:opacity-100"
      >
        <Pencil size={13} />
      </button>
    </div>
  );
}

const PAGE = 15;

export function PaymentsTable({ txns, onChange }: { txns: Txn[]; onChange: (t: Txn) => void }) {
  const [showAll, setShowAll] = useState(false);
  if (!txns.length) return <p className="text-muted">No payments from this customer yet.</p>;
  const shown = showAll ? txns : txns.slice(0, PAGE);
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[560px] text-left text-[0.95rem]">
        <thead className="text-sm text-muted">
          <tr className="border-b border-line">
            <th className="py-2 font-normal">When</th>
            <th className="py-2 pr-6 text-right font-normal">Amount</th>
            <th className="py-2 font-normal">What for</th>
            <th className="py-2 font-normal">Via</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((t) => (
            <tr key={t.id} className="border-b border-line last:border-0">
              <td className="py-2.5 whitespace-nowrap">
                {istDate(t.paid_at)} <span className="text-muted">{istTime(t.paid_at)}</span>
              </td>
              <td className="num py-2.5 pr-6 text-right font-mono whitespace-nowrap">{inr(t.amount_paise)}</td>
              <td className="py-2.5">
                <DescriptionCell txn={t} onSaved={onChange} />
              </td>
              <td className="py-2.5 text-sm whitespace-nowrap text-muted">
                {t.invoice_number ? `Invoice ${t.invoice_number}` : (t.payment_mode ?? "UPI")}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {txns.length > PAGE ? (
        <button className="btn btn-quiet mt-4" onClick={() => setShowAll(!showAll)}>
          {showAll ? "Show the latest 15" : `Show all ${txns.length} payments`}
        </button>
      ) : null}
    </div>
  );
}
