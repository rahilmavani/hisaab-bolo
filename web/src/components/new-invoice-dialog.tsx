"use client";

import { Plus, Trash2 } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { api, useApi } from "@/lib/api";
import { inr } from "@/lib/format";
import type { Invoice, Payer } from "@/lib/types";
import { Dialog } from "./dialog";
import { PayerSearchBox, useDebounced } from "./payer-search";
import { Avatar } from "./ui";

type Row = { name: string; qty: string; price: string };

function addDays(iso: string, days: number): string {
  const d = new Date(`${iso}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

export function NewInvoiceDialog({
  open,
  onClose,
  today,
  presetPayer,
}: {
  open: boolean;
  onClose: () => void;
  /** App-clock date (YYYY-MM-DD), so due dates follow the demo clock. */
  today: string;
  presetPayer?: Pick<Payer, "id" | "display_name" | "email" | "gstin"> | null;
}) {
  const router = useRouter();
  const [payer, setPayer] = useState(presetPayer ?? null);
  const [q, setQ] = useState("");
  const query = useDebounced(q.trim());
  const { data: found } = useApi<{ payers: Payer[] }>(open && !payer ? `/payers?q=${encodeURIComponent(query)}&limit=6` : null, {
    keepPreviousData: true,
  });
  const [rows, setRows] = useState<Row[]>([{ name: "", qty: "1", price: "" }]);
  const [due, setDue] = useState(addDays(today, 7));
  const [gst, setGst] = useState(Boolean(presetPayer?.gstin));
  const [gstin, setGstin] = useState(presetPayer?.gstin ?? "");
  const [email, setEmail] = useState(presetPayer?.email ?? "");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const items = rows.map((r) => ({
    name: r.name.trim(),
    qty: Math.max(0, Math.floor(Number(r.qty) || 0)),
    unit_price_paise: Math.round((Number(r.price) || 0) * 100),
  }));
  const total = items.reduce((s, i) => s + i.qty * i.unit_price_paise, 0);

  function pick(p: Payer) {
    setPayer(p);
    setEmail(p.email ?? "");
    setGstin(p.gstin ?? "");
    setGst(Boolean(p.gstin));
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!payer) return setError("Pick who this invoice is for.");
    if (items.some((i) => !i.name || i.qty <= 0 || i.unit_price_paise <= 0))
      return setError("Every line needs a description, a quantity and a price.");
    if (total < 100) return setError("The total must be at least ₹1.");
    setBusy(true);
    setError(null);
    try {
      const inv = await api<Invoice>("/invoices", {
        method: "POST",
        body: JSON.stringify({
          payer_id: payer.id,
          due_date: due,
          line_items: items,
          gst_taxable: gst,
          payer_gstin: gst && gstin.trim() ? gstin.trim().toUpperCase() : null,
          email: email.trim() || null,
          send_email: Boolean(email.trim()),
        }),
      });
      onClose();
      router.push(`/collections/${inv.id}`);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Couldn't create the invoice";
      setError(
        msg.includes("payer_gstin")
          ? "GSTIN should be 15 characters, like 27AAPFS1234K1ZW."
          : msg.startsWith("email:")
            ? "That email address doesn't look right."
            : msg,
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onClose={onClose} title="New invoice" wide>
      <form onSubmit={submit} className="space-y-5">
        <fieldset className="space-y-2">
          <legend className="mb-1.5 text-sm text-muted">Customer</legend>
          {payer ? (
            <div className="flex items-center justify-between gap-3 rounded-2xl border border-line px-4 py-3">
              <span className="flex items-center gap-3">
                <Avatar name={payer.display_name} size={34} />
                <span className="font-medium">{payer.display_name}</span>
              </span>
              {presetPayer ? null : (
                <button type="button" className="text-sm text-sky hover:underline" onClick={() => setPayer(null)}>
                  Change
                </button>
              )}
            </div>
          ) : (
            <>
              <PayerSearchBox value={q} onChange={setQ} autoFocus />
              <ul className="max-h-52 space-y-1 overflow-y-auto">
                {found?.payers.map((p) => (
                  <li key={p.id}>
                    <button
                      type="button"
                      onClick={() => pick(p)}
                      className="flex w-full items-center gap-3 rounded-xl px-3 py-2 text-left hover:bg-white/5"
                    >
                      <Avatar name={p.display_name} size={30} />
                      <span className="min-w-0">
                        <span className="block truncate">{p.display_name}</span>
                        <span className="block truncate text-sm text-muted">{p.email ?? p.vpa}</span>
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            </>
          )}
        </fieldset>

        <fieldset className="space-y-2">
          <legend className="mb-1.5 text-sm text-muted">What it’s for</legend>
          {rows.map((r, i) => (
            <div key={i} className="grid grid-cols-[minmax(0,1fr)_4.5rem_7rem_2rem] items-center gap-2">
              <input
                aria-label={`Item ${i + 1} description`}
                className="field"
                placeholder="e.g. Canteen staples"
                value={r.name}
                maxLength={80}
                onChange={(e) => setRows(rows.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))}
              />
              <input
                aria-label={`Item ${i + 1} quantity`}
                className="field"
                inputMode="numeric"
                value={r.qty}
                onChange={(e) => setRows(rows.map((x, j) => (j === i ? { ...x, qty: e.target.value } : x)))}
              />
              <input
                aria-label={`Item ${i + 1} price in rupees`}
                className="field"
                inputMode="decimal"
                placeholder="₹"
                value={r.price}
                onChange={(e) => setRows(rows.map((x, j) => (j === i ? { ...x, price: e.target.value } : x)))}
              />
              <button
                type="button"
                aria-label={`Remove item ${i + 1}`}
                disabled={rows.length === 1}
                onClick={() => setRows(rows.filter((_, j) => j !== i))}
                className="rounded-full p-1.5 text-muted hover:text-rose disabled:opacity-30"
              >
                <Trash2 size={16} />
              </button>
            </div>
          ))}
          <button
            type="button"
            className="inline-flex items-center gap-1.5 text-sm text-sky hover:underline"
            onClick={() => setRows([...rows, { name: "", qty: "1", price: "" }])}
          >
            <Plus size={15} /> Add a line
          </button>
        </fieldset>

        <div className="grid gap-4 sm:grid-cols-2">
          <label className="block space-y-1.5">
            <span className="text-sm text-muted">Due date</span>
            <input className="field" type="date" min={today} value={due} onChange={(e) => setDue(e.target.value)} required />
          </label>
          <label className="block space-y-1.5">
            <span className="text-sm text-muted">Send to (email)</span>
            <input
              className="field"
              type="email"
              placeholder="Leave empty to just get a link"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </label>
        </div>

        <div className="space-y-2">
          <label className="flex items-center gap-2.5">
            <input type="checkbox" checked={gst} onChange={(e) => setGst(e.target.checked)} className="h-4 w-4 accent-blue" />
            <span>GST invoice (B2B)</span>
          </label>
          {gst ? (
            <input
              aria-label="Customer GSTIN"
              className="field font-mono"
              placeholder="Customer GSTIN, e.g. 27AAPFS1234K1ZW"
              maxLength={15}
              value={gstin}
              onChange={(e) => setGstin(e.target.value.toUpperCase())}
            />
          ) : null}
        </div>

        {error ? (
          <p role="alert" className="text-sm text-rose">
            {error}
          </p>
        ) : null}
        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line pt-4">
          <p className="text-lg">
            Total <span className="num ml-1 font-mono font-semibold">{inr(total)}</span>
          </p>
          <div className="flex gap-2">
            <button type="button" className="btn btn-quiet" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className="btn btn-primary" disabled={busy}>
              {busy ? "Creating…" : email.trim() ? "Create and email" : "Create invoice"}
            </button>
          </div>
        </div>
      </form>
    </Dialog>
  );
}
