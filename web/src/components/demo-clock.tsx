"use client";

import { Clock3, RotateCcw } from "lucide-react";
import { useState } from "react";
import { useSWRConfig } from "swr";
import { api } from "@/lib/api";
import { istDayLabel, istTime } from "@/lib/format";
import type { Me } from "@/lib/types";

type Jobs = {
  skipped?: string;
  dues_created?: { invoice: string }[] | { error: string };
  due_emails?: { sent?: string[] } | { error: string };
  paid_by_status_poll?: { invoice: string }[] | { error: string };
  overdue?:
    | { overdue?: string[]; reminded?: string[]; retry_later?: string[] }
    | { error: string };
};

function summarise(jobs: Jobs | undefined): string {
  if (!jobs) return "Clock moved.";
  if (jobs.skipped)
    return "Clock moved. The background jobs were already running; they'll pick this up.";
  const parts: string[] = [];
  const dues = Array.isArray(jobs.dues_created) ? jobs.dues_created.length : 0;
  if (dues) parts.push(`${dues} due${dues === 1 ? "" : "s"} created`);
  const resent =
    jobs.due_emails && "sent" in jobs.due_emails
      ? (jobs.due_emails.sent ?? [])
      : [];
  if (resent.length) parts.push(`invoice emailed for ${resent.join(", ")}`);
  const polled = Array.isArray(jobs.paid_by_status_poll) ? jobs.paid_by_status_poll : [];
  if (polled.length) parts.push(`payment found for ${polled.map((p) => p.invoice).join(", ")}`);
  const od =
    jobs.overdue && !("error" in jobs.overdue) ? jobs.overdue : undefined;
  const overdue = od?.overdue?.length ?? 0;
  if (overdue) parts.push(`${overdue} went overdue`);
  if (od?.reminded?.length)
    parts.push(`reminder sent for ${od.reminded.join(", ")}`);
  if (od?.retry_later?.length)
    parts.push(`reminder for ${od.retry_later.join(", ")} will be retried`);
  return parts.length ? parts.join(" · ") : "Jobs ran; nothing new was due.";
}

/** Demo-only time jump: moves the shared app clock and runs the background jobs at the new time. */
export function DemoClock({ me }: { me: Me }) {
  const { mutate } = useSWRConfig();
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  async function move(body: object) {
    setBusy(true);
    setNote("Running jobs…");
    try {
      const r = await api<{ now: string; jobs?: Jobs; reopened?: string[] }>(
        "/admin/clock",
        {
          method: "POST",
          body: JSON.stringify(body),
        },
      );
      const reopened = r.reopened?.length
        ? `${r.reopened.join(", ")} no longer overdue. `
        : "";
      setNote(reopened + summarise(r.jobs));
      await mutate(() => true);
    } catch (e) {
      setNote(e instanceof Error ? e.message : "Couldn't move the clock");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section
      aria-label="Demo clock"
      className="rounded-xl border border-dashed border-line-strong p-3 text-sm"
    >
      <p className="flex items-center gap-2 text-muted">
        <Clock3 size={16} aria-hidden /> Demo clock{" "}
        {me.clock_overridden ? "(moved)" : ""}
      </p>
      <p className="mt-0.5 font-medium text-text" data-testid="demo-clock-now">
        {istDayLabel(me.now)}, {istTime(me.now)}
      </p>
      <div className="mt-2 flex flex-wrap gap-1.5">
        <button
          className="btn btn-quiet px-2.5 py-1 text-xs"
          disabled={busy}
          onClick={() => move({ days: 1 })}
        >
          +1 day
        </button>
        <button
          className="btn btn-quiet px-2.5 py-1 text-xs"
          disabled={busy}
          onClick={() => move({ days: 7 })}
        >
          +7 days
        </button>
        {me.clock_overridden ? (
          <button
            className="btn btn-quiet px-2.5 py-1 text-xs"
            disabled={busy}
            onClick={() => move({ reset: true })}
            aria-label="Reset demo clock"
          >
            <RotateCcw size={12} aria-hidden /> Reset
          </button>
        ) : null}
      </div>
      {note ? (
        <p role="status" className="mt-2 text-xs text-muted">
          {note}
        </p>
      ) : null}
    </section>
  );
}
