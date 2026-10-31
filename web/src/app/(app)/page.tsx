"use client";

import clsx from "clsx";
import { TrendingDown } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { PeakHoursHeatmap, TrendChart, type Grain } from "@/components/charts";
import { InvoiceStatusText } from "@/components/invoice-status";
import { PayerSearch } from "@/components/payer-search";
import {
  Avatar,
  ErrorNote,
  SectionTitle,
  Skeleton,
  StatTile,
} from "@/components/ui";
import { useApi } from "@/lib/api";
import { dateOnly, hourLabel, inr, istDate, istTime, pct } from "@/lib/format";
import type { DashboardSummary, TrendPoint } from "@/lib/types";

const RANGES: { key: string; label: string; days: number; grain: Grain }[] = [
  { key: "7d", label: "7 days", days: 7, grain: "day" },
  { key: "30d", label: "30 days", days: 30, grain: "day" },
  { key: "90d", label: "90 days", days: 91, grain: "week" },
];

function TrendBlock({
  fallback,
  isToday,
}: {
  fallback: TrendPoint[];
  isToday: boolean;
}) {
  const [range, setRange] = useState(RANGES[0]);
  const { data } = useApi<{ points: TrendPoint[] }>(
    range.key === "7d"
      ? null
      : `/dashboard/trend?days=${range.days}&grain=${range.grain}`,
    { keepPreviousData: true },
  );
  const points = range.key === "7d" ? fallback : (data?.points ?? fallback);
  return (
    <div>
      <div className="mb-2 flex items-center justify-between gap-3">
        <h2 className="text-sm text-muted">
          Collections, last {range.label}
          {range.grain === "week" ? " by week" : ""}
        </h2>
        <div
          role="group"
          aria-label="Chart range"
          className="flex rounded-full border border-line p-0.5 text-sm"
        >
          {RANGES.map((r) => (
            <button
              key={r.key}
              onClick={() => setRange(r)}
              aria-pressed={r.key === range.key}
              className={clsx(
                "rounded-full px-3 py-1 transition-colors",
                r.key === range.key
                  ? "bg-blue text-white"
                  : "text-muted hover:text-text",
              )}
            >
              {r.label.replace(" days", "D")}
            </button>
          ))}
        </div>
      </div>
      <TrendChart points={points} grain={range.grain} partialLast={isToday} />
    </div>
  );
}

function SlowDayBanner({
  slow,
}: {
  slow: NonNullable<DashboardSummary["slow_day"]>;
}) {
  return (
    <div
      role="status"
      className="flex items-start gap-3 rounded-2xl border border-amber/25 bg-amber/[0.07] px-4 py-3.5"
    >
      <TrendingDown
        size={20}
        className="mt-0.5 shrink-0 text-amber"
        aria-hidden
      />
      <p className="text-[0.98rem] text-text">
        Slow day: takings are {slow.drop_pct}% below a usual {slow.weekday} so
        far.{" "}
        <span className="text-muted">
          {inr(slow.today_paise)} by {slow.as_of}, against an average of{" "}
          {inr(slow.baseline_paise)} by this time on the last four{" "}
          {slow.weekday}s.
        </span>
      </p>
    </div>
  );
}

export default function DashboardPage() {
  const { data, error, isLoading, mutate } = useApi<DashboardSummary>(
    "/dashboard/summary",
    {
      refreshInterval: 60_000,
    },
  );

  if (error)
    return (
      <ErrorNote
        message={`Couldn't load today's numbers: ${error.message}`}
        onRetry={() => mutate()}
      />
    );
  if (isLoading || !data) return <DashboardSkeleton />;

  const k = data.kpis;
  const compareLabel = `last ${dateOnly(k.compare_day, { weekday: "short" })}`;

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-[1.9rem] leading-tight font-semibold tracking-tight sm:text-[2.2rem]">
            Today’s hisaab
          </h1>
          <p className="text-muted">
            {dateOnly(data.date, {
              weekday: "long",
              day: "numeric",
              month: "long",
            })}
            {data.is_today ? `, as of ${istTime(data.now)}` : ""}
          </p>
        </div>
      </header>

      {data.slow_day?.is_slow ? <SlowDayBanner slow={data.slow_day} /> : null}

      <section
        aria-label="Today at a glance"
        className="glass space-y-6 p-5 sm:p-7"
      >
        <div className="grid grid-cols-1 gap-5 sm:grid-cols-3">
          <StatTile
            label="Collected today"
            value={inr(k.collected_paise)}
            delta={
              k.change_vs_last_week === null
                ? null
                : { ratio: k.change_vs_last_week, vs: compareLabel }
            }
          />
          <StatTile
            label="Payments"
            value={k.payments.toLocaleString("en-IN")}
            sub={`${k.new_payers} new payer${k.new_payers === 1 ? "" : "s"}`}
          />
          <StatTile
            label="Repeat customers"
            value={pct(k.repeat_share)}
            sub="of today’s volume"
          />
        </div>
        <TrendBlock fallback={data.trend} isToday={data.is_today} />
        <PayerSearch limit={5} />
      </section>

      <div className="grid grid-cols-1 gap-6 @4xl:grid-cols-2">
        <section
          className="panel min-w-0 p-5 sm:p-6"
          aria-labelledby="pending-title"
        >
          <SectionTitle
            title="Pending payments"
            sub={
              data.outstanding.invoices.length
                ? `${inr(data.outstanding.total_paise)} outstanding · ${data.outstanding.customers} customer${data.outstanding.customers === 1 ? "" : "s"}`
                : "Nothing outstanding"
            }
          />
          <h2 id="pending-title" className="sr-only">
            Pending payments
          </h2>
          {data.outstanding.invoices.length ||
          data.outstanding.recently_paid.length ? (
            <table className="w-full text-left text-[0.97rem]">
              <thead className="text-sm text-muted">
                <tr className="border-b border-line">
                  <th className="py-2 font-normal">Customer</th>
                  <th className="py-2 text-right font-normal">Amount</th>
                  <th className="py-2 pl-4 font-normal">Status</th>
                </tr>
              </thead>
              <tbody>
                {data.outstanding.invoices.map((inv) => (
                  <tr
                    key={inv.id}
                    className="border-b border-line last:border-0"
                  >
                    <td className="py-3">
                      <Link
                        href={`/customers/${inv.payer_id}`}
                        className="hover:text-sky"
                      >
                        {inv.payer_name}
                      </Link>
                      <span className="block font-mono text-xs text-faint">
                        {inv.number}
                      </span>
                    </td>
                    <td className="num py-3 text-right font-mono">
                      {inr(inv.amount_paise)}
                    </td>
                    <td className="py-3 pl-4">
                      <InvoiceStatusText
                        status={inv.status}
                        dueInDays={-inv.days_overdue}
                        graceDays={inv.grace_days}
                      />
                    </td>
                  </tr>
                ))}
                {data.outstanding.recently_paid.map((inv) => (
                  <tr
                    key={inv.id}
                    className="border-b border-line last:border-0"
                  >
                    <td className="py-3">
                      <Link
                        href={`/collections/${inv.id}`}
                        className="hover:text-sky"
                      >
                        {inv.payer_name}
                      </Link>
                      <span className="block font-mono text-xs text-faint">
                        {inv.number}
                      </span>
                    </td>
                    <td className="num py-3 text-right font-mono">
                      {inr(inv.amount_paise)}
                    </td>
                    <td className="py-3 pl-4">
                      <InvoiceStatusText status="paid" dueInDays={0} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="text-muted">
              Every invoice is paid. New invoices you send will show up here
              until they’re paid.
            </p>
          )}
        </section>

        <section className="panel min-w-0 p-5 sm:p-6">
          <SectionTitle
            title="Gone quiet"
            sub="Regulars who have missed far more than their usual gap"
          />
          {data.lost_regulars.length ? (
            <ul className="space-y-3">
              {data.lost_regulars.map((p) => (
                <li key={p.id}>
                  <Link
                    href={`/customers/${p.id}`}
                    className="flex items-center gap-3 rounded-2xl border border-amber/20 bg-amber/[0.05] px-4 py-3 hover:border-amber/40"
                  >
                    <Avatar name={p.display_name} size={38} />
                    <div className="min-w-0 flex-1">
                      <p className="font-semibold">
                        {p.display_name}{" "}
                        <span className="font-normal text-amber">
                          · last paid {Math.floor(p.days_since_last ?? 0)} days
                          ago
                        </span>
                      </p>
                      <p className="truncate text-sm text-muted">
                        Usually {p.habit}
                      </p>
                    </div>
                  </Link>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-muted">
              All your regulars have paid on their usual rhythm.
            </p>
          )}
        </section>

        <section className="panel min-w-0 p-5 sm:p-6">
          <SectionTitle
            title="Peak hours"
            sub={
              data.peak_hours.length
                ? `Busiest: ${data.peak_hours.map((p) => hourLabel(p.hour)).join(", ")}`
                : "Not enough payments yet"
            }
          />
          <PeakHoursHeatmap cells={data.heatmap} />
        </section>

        <section className="panel min-w-0 p-5 sm:p-6">
          <SectionTitle
            title="Latest payments"
            sub={data.is_today ? "Today" : dateOnly(data.date)}
          />
          {data.recent.length ? (
            <ul className="divide-y divide-line">
              {data.recent.map((t) => (
                <li
                  key={t.id}
                  className="flex items-center justify-between gap-3 py-2.5"
                >
                  <div className="min-w-0">
                    <p className="truncate">
                      {t.payer_id ? (
                        <Link
                          href={`/customers/${t.payer_id}`}
                          className="hover:text-sky"
                        >
                          {t.payer_name}
                        </Link>
                      ) : (
                        <span className="text-muted">Walk-in</span>
                      )}
                    </p>
                    <p className="truncate text-sm text-muted">
                      {t.description ?? "Payment"}
                    </p>
                  </div>
                  <div className="shrink-0 text-right">
                    <p className="num font-mono">{inr(t.amount_paise)}</p>
                    <p className="text-xs text-faint">{istTime(t.paid_at)}</p>
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-muted">
              No payments yet{" "}
              {data.is_today ? "today" : `on ${istDate(data.date)}`}.
            </p>
          )}
        </section>
      </div>
    </div>
  );
}

function DashboardSkeleton() {
  return (
    <div
      className="mx-auto max-w-6xl space-y-6"
      aria-busy="true"
      aria-label="Loading"
    >
      <Skeleton className="h-14 w-72" />
      <Skeleton className="h-[460px] rounded-[28px]" />
      <div className="grid gap-6 @4xl:grid-cols-2">
        <Skeleton className="h-64" />
        <Skeleton className="h-64" />
      </div>
    </div>
  );
}
