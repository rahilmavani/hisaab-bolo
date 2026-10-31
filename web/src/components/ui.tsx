import clsx from "clsx";
import type { ReactNode } from "react";
import { initials } from "@/lib/format";

export function Avatar({ name, size = 44 }: { name: string; size?: number }) {
  return (
    <span
      aria-hidden
      className="inline-flex shrink-0 items-center justify-center rounded-full bg-raised font-medium text-muted shadow-[inset_0_0_0_1px_var(--color-line)]"
      style={{ width: size, height: size, fontSize: Math.round(size * 0.36) }}
    >
      {initials(name)}
    </span>
  );
}

// Each customer category gets a small dot; the label itself stays quiet.
const BADGE_DOTS: Record<string, string> = {
  Regular: "bg-blue",
  Recurring: "bg-mint",
  Lapsed: "bg-amber",
  New: "bg-faint",
};

export function Badge({ label }: { label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-md border border-line px-2 py-0.5 text-sm leading-5 text-muted">
      <span aria-hidden className={clsx("h-1.5 w-1.5 rounded-full", BADGE_DOTS[label] ?? "bg-faint")} />
      {label}
    </span>
  );
}

export function StatTile({
  label,
  value,
  sub,
  delta,
  total = false,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  delta?: { ratio: number; vs: string } | null;
  /** Draw the ledger's double rule under the figure: for the one total that matters on the screen. */
  total?: boolean;
}) {
  const up = delta && delta.ratio >= 0;
  return (
    <div className="min-w-0">
      <p className="text-sm text-muted">{label}</p>
      <p className="num mt-2 text-3xl leading-9 font-medium text-text">
        <span className={total ? "ledger-total" : undefined}>{value}</span>
      </p>
      {delta ? (
        <p className="mt-2 text-sm text-faint">
          <span className={clsx("num", up ? "text-mint" : "text-amber")}>
            {up ? "+" : "−"}
            {Math.abs(Math.round(delta.ratio * 100))}%
          </span>{" "}
          vs {delta.vs}
        </p>
      ) : sub ? (
        <p className="mt-2 text-sm text-muted">{sub}</p>
      ) : null}
    </div>
  );
}

export function SectionTitle({ title, action, sub }: { title: string; sub?: ReactNode; action?: ReactNode }) {
  return (
    <div className="mb-4 flex items-end justify-between gap-3">
      <div className="min-w-0">
        <h2 className="text-lg font-medium text-text">{title}</h2>
        {sub ? <p className="mt-1 text-sm text-muted">{sub}</p> : null}
      </div>
      {action}
    </div>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={clsx("animate-pulse rounded-xl bg-raised", className)} />;
}

export function ErrorNote({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div role="alert" className="panel flex items-center justify-between gap-3 border-rose/30 p-4 text-sm">
      <span className="text-rose">{message}</span>
      {onRetry ? (
        <button className="btn btn-quiet" onClick={onRetry}>
          Try again
        </button>
      ) : null}
    </div>
  );
}
