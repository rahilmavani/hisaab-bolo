"use client";

import { useState, type FormEvent } from "react";
import { api, useApi } from "@/lib/api";
import { inr } from "@/lib/format";
import type { Payer, PayerProfile } from "@/lib/types";
import { Dialog } from "./dialog";
import { PayerSearchBox, useDebounced } from "./payer-search";
import { Avatar } from "./ui";

export function EditPayerDialog({
  payer,
  open,
  onClose,
  onSaved,
}: {
  payer: PayerProfile;
  open: boolean;
  onClose: () => void;
  onSaved: (p: PayerProfile) => void;
}) {
  const [form, setForm] = useState({
    display_name: payer.display_name,
    email: payer.email ?? "",
    phone: payer.phone ?? "",
    gstin: payer.gstin ?? "",
    notes: payer.notes ?? "",
  });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const body: Record<string, string | null> = { display_name: form.display_name.trim(), notes: form.notes };
    for (const key of ["email", "phone", "gstin"] as const) {
      const v = form[key].trim();
      if (v || payer[key]) body[key] = v ? (key === "gstin" ? v.toUpperCase() : v) : null;
    }
    try {
      onSaved(await api<PayerProfile>(`/payers/${payer.id}`, { method: "PATCH", body: JSON.stringify(body) }));
      onClose();
    } catch (err) {
      setError(err instanceof Error ? friendlyValidation(err.message) : "Couldn't save");
    } finally {
      setBusy(false);
    }
  }

  const field = (key: keyof typeof form, label: string, props: React.InputHTMLAttributes<HTMLInputElement> = {}) => (
    <label className="block space-y-1.5">
      <span className="text-sm text-muted">{label}</span>
      <input className="field" value={form[key]} onChange={(e) => setForm({ ...form, [key]: e.target.value })} {...props} />
    </label>
  );

  return (
    <Dialog open={open} onClose={onClose} title="Edit customer">
      <form onSubmit={submit} className="space-y-4">
        {field("display_name", "Name", { required: true, maxLength: 120 })}
        {field("email", "Email", { type: "email", placeholder: "For invoices and reminders" })}
        <div className="grid gap-4 sm:grid-cols-2">
          {field("phone", "Phone", { inputMode: "tel", placeholder: "10-digit mobile" })}
          {field("gstin", "GSTIN", { placeholder: "For B2B invoices", maxLength: 15 })}
        </div>
        <label className="block space-y-1.5">
          <span className="text-sm text-muted">Notes</span>
          <textarea
            className="field min-h-20"
            value={form.notes}
            maxLength={2000}
            onChange={(e) => setForm({ ...form, notes: e.target.value })}
          />
        </label>
        {error ? (
          <p role="alert" className="text-sm text-rose">
            {error}
          </p>
        ) : null}
        <div className="flex justify-end gap-2 pt-1">
          <button type="button" className="btn btn-quiet" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="btn btn-primary" disabled={busy}>
            {busy ? "Saving…" : "Save changes"}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

function friendlyValidation(message: string): string {
  if (message.includes("gstin")) return "GSTIN should be 15 characters, like 27AAPFS1234K1Z5.";
  if (message.includes("phone")) return "Phone should be digits only, 7 to 15 of them.";
  if (message.includes("email")) return "That email address doesn't look right.";
  if (message.includes("display_name")) return "The name can't be empty.";
  return message;
}

export function MergeDialog({
  payer,
  open,
  onClose,
  onMerged,
}: {
  payer: PayerProfile;
  open: boolean;
  onClose: () => void;
  onMerged: (p: PayerProfile) => void;
}) {
  const [q, setQ] = useState("");
  const [picked, setPicked] = useState<Payer | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const query = useDebounced(q.trim());
  const { data } = useApi<{ payers: Payer[] }>(open ? `/payers?q=${encodeURIComponent(query)}&limit=8` : null, {
    keepPreviousData: true,
  });
  const options = (data?.payers ?? []).filter((p) => p.id !== payer.id);

  async function merge() {
    if (!picked) return;
    setBusy(true);
    setError(null);
    try {
      onMerged(
        await api<PayerProfile>(`/payers/${payer.id}/merge`, {
          method: "POST",
          body: JSON.stringify({ other_payer_id: picked.id }),
        }),
      );
      setPicked(null);
      setQ("");
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't merge");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onClose={onClose} title={`Merge into ${payer.display_name}`} wide>
      {picked ? (
        <div className="space-y-4">
          <p>
            <strong>{picked.display_name}</strong> will be folded into <strong>{payer.display_name}</strong>. Their{" "}
            {picked.txn_count} payment{picked.txn_count === 1 ? "" : "s"} ({inr(picked.total_paise)}), invoices
            {[picked.vpa && ", UPI ID", picked.phone && ", phone", picked.email && ", email"].filter(Boolean).join("")} move
            here, and {picked.display_name} disappears from your customer list.
          </p>
          {error ? (
            <p role="alert" className="text-sm text-rose">
              {error}
            </p>
          ) : null}
          <div className="flex justify-end gap-2">
            <button className="btn btn-quiet" onClick={() => setPicked(null)}>
              Back
            </button>
            <button className="btn btn-primary" onClick={merge} disabled={busy}>
              {busy ? "Merging…" : `Merge ${picked.display_name}`}
            </button>
          </div>
        </div>
      ) : (
        <div className="space-y-3">
          <p className="text-muted">
            Same person paying from two UPI IDs? Pick the duplicate and their history joins this ledger.
          </p>
          <PayerSearchBox value={q} onChange={setQ} autoFocus />
          <ul className="max-h-72 space-y-1.5 overflow-y-auto">
            {options.map((p) => (
              <li key={p.id}>
                <button
                  onClick={() => setPicked(p)}
                  className="flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left hover:bg-raised"
                >
                  <Avatar name={p.display_name} size={34} />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-medium">{p.display_name}</span>
                    <span className="block truncate text-sm text-muted">{p.vpa ?? p.habit}</span>
                  </span>
                </button>
              </li>
            ))}
            {query && !options.length ? <li className="px-3 text-sm text-muted">No other customer matches.</li> : null}
          </ul>
        </div>
      )}
    </Dialog>
  );
}
