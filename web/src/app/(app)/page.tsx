"use client";

import clsx from "clsx";
import { TrendingDown } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import {
  COMPARE,
  PeakHoursHeatmap,
  TrendChart,
  type Grain,
} from "@/components/charts";
import { InvoiceStatusText } from "@/components/invoice-status";
import { PayerSearch } from "@/components/payer-search";
import { PromisedPayments } from "@/components/promised-payments";
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

const RANGES: {
  key: string;
  label: string;
  days: number;
  grain: Grain;
  now: string;
  before: string;
}[] = [
  {
    key: "7d",
    label: "7 days",
    days: 7,
    grain: "day",
    now: "Last 7 days",
    before: "7 days before",
  },
  {
    key: "30d",
    label: "30 days",
    days: 30,
    grain: "day",
    now: "Last 30 days",
    before: "30 days before",
  },
  {
    key: "90d",
    label: "90 days",
    days: 91,
    grain: "week",
    now: "Last 13 weeks",
    before: "13 weeks before",
  },
];

/** The calendar date `days` before an ISO date, as YYYY-MM-DD. */
function daysBefore(isoDate: string, days: number): string {
  const d = new Date(`${isoDate}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() - days);
  return d.toISOString().slice(0, 10);
}

function TrendBlock({
  fallback,
  date,
  isToday,
}: {
  fallback: TrendPoint[];
  date: string;
  isToday: boolean;
}) {
  const [range, setRange] = useState(RANGES[0]);
  const { data } = useApi<{ points: TrendPoint[] }>(
    range.key === "7d"
      ? null
      : `/dashboard/trend?days=${range.days}&grain=${range.grain}`,
    { keepPreviousData: true },
  );
  // The same span just before, for the quiet comparison line. `end` goes first so this
  // request never matches a wait on `/dashboard/trend?days=…`.
  const { data: before } = useApi<{ points: TrendPoint[] }>(
    `/dashboard/trend?end=${daysBefore(date, range.days)}&days=${range.days}&grain=${range.grain}`,
    { keepPreviousData: true },
  );
  const points = range.key === "7d" ? fallback : (data?.points ?? fallback);
  return (
    <div>
      <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-[15px] font-medium text-text">
            Collections, last {range.label}
            {range.grain === "week" ? " by week" : ""}
          </h2>
          <p className="mt-1 flex flex-wrap gap-x-4 font-mono text-xs text-faint">
            <span className="flex items-center gap-2">
              <span aria-hidden className="h-0.5 w-4 rounded-full bg-blue" />
              {range.now}
            </span>
            {before ? (
              <span className="flex items-center gap-2">
                <span
                  aria-hidden
                  className="h-0.5 w-4 rounded-full"
                  style={{ background: COMPARE }}
                />
                {range.before}
              </span>
            ) : null}
          </p>
        </div>
        <div
          role="group"
          aria-label="Chart range"
          className="flex rounded-lg border border-line bg-page p-0.5"
        >
          {RANGES.map((r) => (
            <button
              key={r.key}
              onClick={() => setRange(r)}
              aria-pressed={r.key === range.key}
              className={clsx(
                "h-7 rounded-md px-2.5 font-mono text-xs transition-colors",
                r.key === range.key
                  ? "bg-card text-text shadow-[inset_0_0_0_1px_var(--color-line)]"
                  : "text-muted hover:text-text",
              )}
            >
              {r.label.replace(" days", "D")}
            </button>
          ))}
        </div>
      </div>
      <TrendChart
        points={points}
        grain={range.grain}
        partialLast={isToday}
        compare={before?.points}
        compareLabel={range.before}
        height={280}
      />
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
      className="flex items-start gap-3 rounded-xl border border-line bg-card px-5 py-4 shadow-[0_1px_2px_rgb(31_27_22/0.05)]"
    >
      <TrendingDown
        size={16}
        className="mt-[3px] shrink-0 text-amber"
        aria-hidden
      />
      <p className="text-[15px] text-text">
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
      <header>
        <h1 className="text-[28px] leading-9 font-medium tracking-[-0.01em] sm:text-[30px]">
          Today’s hisaab
        </h1>
        <p className="mt-1 text-sm text-muted">
          {dateOnly(data.date, {
            weekday: "long",
            day: "numeric",
            month: "long",
          })}
          {data.is_today ? `, as of ${istTime(data.now)}` : ""}
        </p>
      </header>

      {data.slow_day?.is_slow ? <SlowDayBanner slow={data.slow_day} /> : null}

      <section
        aria-label="Today at a glance"
        className="panel flex flex-col gap-8 p-5 sm:p-6"
      >
        <div className="grid grid-cols-2 gap-x-8 gap-y-6 sm:grid-cols-3">
          <div className="col-span-2 sm:col-span-1">
            <StatTile
              label="Collected today"
              value={inr(k.collected_paise)}
              total
              delta={
                k.change_vs_last_week === null
                  ? null
                  : { ratio: k.change_vs_last_week, vs: compareLabel }
              }
            />
          </div>
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
        <TrendBlock
          fallback={data.trend}
          date={data.date}
          isToday={data.is_today}
        />
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
              data.outstanding.invoices.length ? (
                <>
                  <span className="ledger-total num font-mono text-text">
                    {inr(data.outstanding.total_paise)}
                  </span>{" "}
                  outstanding · {data.outstanding.customers} customer
                  {data.outstanding.customers === 1 ? "" : "s"}
                </>
              ) : (
                "Nothing outstanding"
              )
            }
          />
          <h2 id="pending-title" className="sr-only">
            Pending payments
          </h2>
          {data.outstanding.invoices.length ||
          data.outstanding.recently_paid.length ? (
            <table className="w-full text-left text-[15px]">
              <thead className="text-[13px] text-faint">
                <tr className="border-b border-line">
                  <th className="h-9 font-normal">Customer</th>
                  <th className="h-9 text-right font-normal">Amount</th>
                  <th className="h-9 pl-6 font-normal">Status</th>
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
                        className="transition-colors hover:text-blue"
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
                    <td className="py-3 pl-6">
                      <InvoiceStatusText
                        status={inv.status}
                        dueInDays={-inv.days_overdue}
                        graceDays={inv.grace_days}
                      />
                    </td>
                  </tr>
                ))}
                {data.outstanding.recently_paid.length ? (
                  <tr>
                    <td
                      colSpan={3}
                      className="pt-5 pb-1 text-[13px] text-faint"
                    >
                      Paid recently
                    </td>
                  </tr>
                ) : null}
                {data.outstanding.recently_paid.map((inv) => (
                  <tr key={inv.id} className="border-t border-line">
                    <td className="py-3">
                      <Link
                        href={`/collections/${inv.id}`}
                        className="transition-colors hover:text-blue"
                      >
                        {inv.payer_name}
                      </Link>
                      <span className="block font-mono text-xs text-faint">
                        {inv.number}
                      </span>
                    </td>
                    <td className="num py-3 text-right font-mono text-muted">
                      {inr(inv.amount_paise)}
                    </td>
                    <td className="py-3 pl-6">
                      <InvoiceStatusText
                        status="paid"
                        dueInDays={0}
                        markedPaid={inv.marked_paid}
                      />
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

        <PromisedPayments />

        <section className="panel min-w-0 p-5 sm:p-6">
          <SectionTitle
            title="Gone quiet"
            sub="Regulars who have missed far more than their usual gap"
          />
          {data.lost_regulars.length ? (
            <ul className="-mx-3 space-y-1">
              {data.lost_regulars.map((p) => (
                <li key={p.id}>
                  <Link
                    href={`/customers/${p.id}`}
                    className="flex items-center gap-3 rounded-[10px] px-3 py-3 transition-colors hover:bg-raised/60"
                  >
                    <Avatar name={p.display_name} size={36} />
                    <div className="min-w-0 flex-1">
                      <p className="font-medium">
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
                  className="flex items-center justify-between gap-3 py-3"
                >
                  <div className="min-w-0">
                    <p className="truncate text-[15px]">
                      {t.payer_id ? (
                        <Link
                          href={`/customers/${t.payer_id}`}
                          className="transition-colors hover:text-blue"
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
                    <p className="num font-mono text-[15px]">
                      {inr(t.amount_paise)}
                    </p>
                    <p className="font-mono text-xs text-faint">
                      {istTime(t.paid_at)}
                    </p>
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
      <Skeleton className="h-[560px] rounded-2xl" />
      <div className="grid gap-6 @4xl:grid-cols-2">
        <Skeleton className="h-64" />
        <Skeleton className="h-64" />
      </div>
    </div>
  );
}
