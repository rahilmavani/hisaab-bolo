"use client";

import { CheckCircle2, ShieldCheck } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { api } from "@/lib/api";
import { dateOnly, inr, istDate, istTime } from "@/lib/format";
import type { AskComponent, PendingAction } from "@/lib/types";
import { TrendChart } from "../charts";
import { errorText } from "./errors";
import { Avatar, Badge } from "../ui";

function StatCards({
  data,
}: {
  data: Extract<AskComponent, { type: "stat_cards" }>["data"];
}) {
  return (
    <div className="grid grid-cols-2 gap-2">
      {data.items.map((s) => (
        <div
          key={s.label}
          className="rounded-xl border border-line bg-white/[0.03] px-3 py-2"
        >
          <p className="text-xs text-muted">{s.label}</p>
          <p className="num text-lg font-semibold">
            {s.value_paise != null
              ? inr(s.value_paise)
              : s.value != null
                ? s.value.toLocaleString("en-IN")
                : (s.value_text ?? "—")}
          </p>
          {s.sub ? (
            <p className="text-xs text-faint">{dateOnly(s.sub)}</p>
          ) : null}
        </div>
      ))}
    </div>
  );
}

function PayerCard({
  data,
}: {
  data: Extract<AskComponent, { type: "payer_card" }>["data"];
}) {
  return (
    <Link
      href={`/customers/${data.payer_id}`}
      className="block rounded-2xl border border-line bg-white/[0.03] p-3 hover:border-line-strong"
    >
      <div className="flex items-center gap-3">
        <Avatar name={data.name} size={38} />
        <div className="min-w-0 flex-1">
          <p className="truncate font-semibold">
            {data.name}
            {data.vpa ? (
              <span className="font-normal text-muted"> · {data.vpa}</span>
            ) : null}
          </p>
          <p className="truncate text-sm text-muted">{data.habit}</p>
        </div>
        {data.badges[0] ? <Badge label={data.badges[0]} /> : null}
      </div>
      <dl className="mt-3 grid grid-cols-3 gap-2 text-sm">
        <div>
          <dt className="text-xs text-muted">
            {dateOnly(data.window.from)} – {dateOnly(data.window.to)}
          </dt>
          <dd className="num font-semibold">
            {data.total_paise != null ? inr(data.total_paise) : "—"}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted">Payments</dt>
          <dd className="num font-semibold">{data.payments}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted">Last paid</dt>
          <dd className="font-semibold">{data.last_payment_was ?? "—"}</dd>
        </div>
      </dl>
    </Link>
  );
}

function DataTable({
  data,
}: {
  data: Extract<AskComponent, { type: "table" }>["data"];
}) {
  const [all, setAll] = useState(false);
  const rows = all ? data.rows : data.rows.slice(0, 6);
  return (
    <div className="rounded-2xl border border-line bg-white/[0.02] p-3">
      <p className="mb-2 text-sm text-muted">
        {data.title}
        {data.total_paise != null ? ` · ${inr(data.total_paise)}` : ""}
      </p>
      <ul className="divide-y divide-line text-sm">
        {rows.map((r, i) =>
          data.kind === "payments" ? (
            <li
              key={String(r.id ?? i)}
              className="flex items-center justify-between gap-2 py-1.5"
            >
              <span className="min-w-0">
                <span className="block truncate">
                  {(r.customer as string) ?? "Walk-in"}
                </span>
                <span className="block truncate text-xs text-faint">
                  {istDate(r.paid_at as string)}, {istTime(r.paid_at as string)}
                  {r.description ? ` · ${r.description}` : ""}
                </span>
              </span>
              <span className="num shrink-0 font-mono">
                {inr(r.amount_paise as number)}
              </span>
            </li>
          ) : (
            <li key={String(r.id ?? i)}>
              <Link
                href={`/collections/${r.id}`}
                className="flex items-center justify-between gap-2 py-1.5 hover:text-sky"
              >
                <span className="min-w-0">
                  <span className="block truncate">{r.customer as string}</span>
                  <span className="block text-xs text-faint">
                    {r.number as string} ·{" "}
                    {r.status === "overdue" ? (
                      <span className="text-amber">
                        overdue {r.days_overdue as number}d
                      </span>
                    ) : r.status === "paid" ? (
                      <span className="text-mint">paid</span>
                    ) : (
                      `due ${dateOnly(r.due_date as string)}`
                    )}
                  </span>
                </span>
                <span className="num shrink-0 font-mono">
                  {inr(r.amount_paise as number)}
                </span>
              </Link>
            </li>
          ),
        )}
      </ul>
      {data.rows.length > 6 ? (
        <button
          className="mt-2 text-sm text-sky hover:underline"
          onClick={() => setAll(!all)}
        >
          {all
            ? "Show fewer"
            : `Show all ${data.rows.length}${data.total_rows > data.rows.length ? ` of ${data.total_rows}` : ""}`}
        </button>
      ) : null}
    </div>
  );
}

function InvoiceCard({ data }: { data: Record<string, unknown> }) {
  if (data.preview) {
    return (
      <div className="rounded-2xl bg-paper p-3 text-paper-ink">
        <p className="text-xs text-paper-muted">Invoice preview</p>
        <p className="font-semibold">
          {String(data.customer)} · {String(data.amount)}
        </p>
        {data.due_date ? (
          <p className="text-sm text-paper-muted">
            Due {dateOnly(String(data.due_date))}
          </p>
        ) : null}
      </div>
    );
  }
  const inv = data.invoice as Record<string, string | number>;
  return (
    <Link
      href={`/collections/${inv.invoice_id}`}
      className="block rounded-2xl bg-paper p-3 text-paper-ink"
    >
      <p className="text-xs text-paper-muted">{inv.number}</p>
      <p className="font-semibold">
        {inv.customer} · {inr(Math.round(Number(inv.amount_rupees) * 100))}
      </p>
      <p className="text-sm text-paper-muted">
        {inv.status} · due {dateOnly(String(inv.due_date))}
      </p>
    </Link>
  );
}

const ACTION_VERB: Record<PendingAction["tool"], string> = {
  create_invoice: "Create invoice",
  send_reminder: "Send reminder",
  set_recurring: "Mark recurring",
};

function ConfirmAction({ action }: { action: PendingAction }) {
  const [state, setState] = useState<
    "idle" | "busy" | "done" | "dismissed" | "error"
  >("idle");
  const [message, setMessage] = useState<string | null>(null);
  const [link, setLink] = useState<string | null>(null);
  const s = action.summary;

  async function confirm() {
    setState("busy");
    try {
      const r = await api<{ message: string; invoice?: { id: string } }>(
        `/actions/${action.token}/confirm`,
        {
          method: "POST",
        },
      );
      setMessage(r.message);
      setLink(r.invoice ? `/collections/${r.invoice.id}` : null);
      setState("done");
    } catch (e) {
      setMessage(errorText(e, "Couldn't do that"));
      setState("error");
    }
  }

  if (state === "dismissed")
    return <p className="text-sm text-faint">Cancelled. Nothing was sent.</p>;
  return (
    <div className="rounded-2xl border border-sky/30 bg-blue/10 p-3">
      <p className="flex items-center gap-1.5 text-xs text-sky">
        <ShieldCheck size={14} aria-hidden /> Needs your OK
      </p>
      <p className="mt-1 font-semibold">{String(s.title)}</p>
      <ul className="mt-1 space-y-0.5 text-sm text-muted">
        {s.amount ? (
          <li>
            Amount: {String(s.amount)}
            {s.cadence ? `, ${String(s.cadence)}` : ""}
          </li>
        ) : null}
        {s.due_date ? <li>Due {dateOnly(String(s.due_date))}</li> : null}
        {s.invoice ? <li>Invoice {String(s.invoice)}</li> : null}
        {s.delivery ? (
          <li>{String(s.delivery)}</li>
        ) : s.email ? (
          <li>Email to {String(s.email)}</li>
        ) : null}
      </ul>
      {state === "done" ? (
        <p
          role="status"
          className="mt-2 flex items-center gap-1.5 text-sm text-mint"
        >
          <CheckCircle2 size={15} aria-hidden /> {message}
          {link ? (
            <Link href={link} className="ml-1 text-sky underline">
              Open
            </Link>
          ) : null}
        </p>
      ) : (
        <div className="mt-3 flex gap-2">
          <button
            className="btn btn-primary px-4 py-1.5 text-sm"
            onClick={confirm}
            disabled={state === "busy"}
          >
            {state === "busy"
              ? "Working…"
              : `Confirm: ${ACTION_VERB[action.tool]}`}
          </button>
          <button
            className="btn btn-quiet px-4 py-1.5 text-sm"
            onClick={() => setState("dismissed")}
            disabled={state === "busy"}
          >
            Cancel
          </button>
        </div>
      )}
      {state === "error" ? (
        <p role="alert" className="mt-2 text-sm text-rose">
          {message}
        </p>
      ) : null}
    </div>
  );
}

export function AnswerComponents({
  components,
  onPickPayer,
}: {
  components: AskComponent[];
  onPickPayer: (payerId: string, label: string) => void;
}) {
  return (
    <div className="space-y-2">
      {components.map((c, i) => {
        const key = `${c.type}-${c.result_id ?? i}`;
        switch (c.type) {
          case "stat_cards":
            return <StatCards key={key} data={c.data} />;
          case "line_chart":
            return (
              <div
                key={key}
                className="rounded-2xl border border-line bg-white/[0.02] p-2"
              >
                <TrendChart
                  points={c.data.points}
                  grain={c.data.grain}
                  height={150}
                />
              </div>
            );
          case "table":
            return <DataTable key={key} data={c.data} />;
          case "payer_card":
            return <PayerCard key={key} data={c.data} />;
          case "invoice_card":
            return <InvoiceCard key={key} data={c.data} />;
          case "confirm_action":
            return <ConfirmAction key={key} action={c.data} />;
          case "chips":
            return (
              <div
                key={key}
                className="flex flex-wrap gap-2"
                role="group"
                aria-label="Which customer?"
              >
                {c.data.choices.map((ch) => (
                  <button
                    key={ch.payer_id}
                    onClick={() => onPickPayer(ch.payer_id, ch.label)}
                    className="rounded-full border border-line-strong px-3 py-1.5 text-left text-sm hover:border-sky"
                  >
                    {ch.label}
                    {ch.sub ? (
                      <span className="ml-1 text-xs text-faint">{ch.sub}</span>
                    ) : null}
                  </button>
                ))}
              </div>
            );
          default:
            return null;
        }
      })}
    </div>
  );
}
