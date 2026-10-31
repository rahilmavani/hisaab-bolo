import clsx from "clsx";
import { TrendingDown, TrendingUp } from "lucide-react";
import type { ReactNode } from "react";
import { initials } from "@/lib/format";

export function Avatar({ name, size = 44 }: { name: string; size?: number }) {
  return (
    <span
      aria-hidden
      className="inline-flex shrink-0 items-center justify-center rounded-full font-semibold text-white"
      style={{
        width: size,
        height: size,
        fontSize: size * 0.38,
        background: "radial-gradient(circle at 30% 25%, #8fb4ff 0%, #2f6fed 55%, #1b3f9e 100%)",
      }}
    >
      {initials(name)}
    </span>
  );
}

const BADGE_STYLES: Record<string, string> = {
  Regular: "bg-blue/25 text-sky",
  Recurring: "bg-mint/15 text-mint",
  Lapsed: "bg-amber/15 text-amber",
  New: "bg-white/8 text-text",
};

export function Badge({ label }: { label: string }) {
  return (
    <span className={clsx("rounded-full px-3 py-1 text-sm font-medium", BADGE_STYLES[label] ?? "bg-white/8 text-muted")}>
      {label}
    </span>
  );
}

export function StatTile({
  label,
  value,
  sub,
  delta,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  delta?: { ratio: number; vs: string } | null;
}) {
  const up = delta && delta.ratio >= 0;
  return (
    <div className="min-w-0">
      <p className="text-[0.95rem] text-muted">{label}</p>
      <p className="num mt-0.5 text-[2rem] leading-tight font-semibold tracking-tight text-text sm:text-[2.2rem]">
        {value}
      </p>
      {delta ? (
        <p className={clsx("mt-0.5 flex items-center gap-1 text-[0.95rem]", up ? "text-mint" : "text-amber")}>
          {up ? <TrendingUp size={15} aria-hidden /> : <TrendingDown size={15} aria-hidden />}
          <span className="num">
            {up ? "+" : "−"}
            {Math.abs(Math.round(delta.ratio * 100))}%
          </span>
          <span className="text-muted">vs {delta.vs}</span>
        </p>
      ) : sub ? (
        <p className="mt-0.5 text-[0.95rem] text-sky">{sub}</p>
      ) : null}
    </div>
  );
}

export function SectionTitle({ title, action, sub }: { title: string; sub?: ReactNode; action?: ReactNode }) {
  return (
    <div className="mb-4 flex items-end justify-between gap-3">
      <div className="min-w-0">
        <h2 className="text-lg font-semibold text-text">{title}</h2>
        {sub ? <p className="text-sm text-muted">{sub}</p> : null}
      </div>
      {action}
    </div>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={clsx("animate-pulse rounded-xl bg-white/5", className)} />;
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
