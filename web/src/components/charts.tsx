"use client";

import { area as d3area, curveMonotoneX, line as d3line } from "d3-shape";
import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import {
  dateOnly,
  hourLabel,
  inr,
  inrCompact,
  weekdayShort,
} from "@/lib/format";
import type { HeatCell, TrendPoint } from "@/lib/types";

const SURFACE = "#fdfbf7";
const GRID = "rgba(31,27,22,0.08)";
const SERIES = "#2a5fd6";
// The earlier period on the trend chart; the dashboard legend uses it too.
export const COMPARE = "#c2b8a7";
const HEAT = "42,95,214";

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(([entry]) =>
      setWidth(entry.contentRect.width),
    );
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, width] as const;
}

function niceMax(v: number): number {
  if (v <= 0) return 100_00;
  const exp = Math.pow(10, Math.floor(Math.log10(v)));
  const n = v / exp;
  const step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10;
  return step * exp;
}

export type Grain = "day" | "week" | "month";

function bucketLabel(bucket: string, grain: Grain, short: boolean): string {
  if (grain === "month") return dateOnly(bucket, { month: "short" });
  if (grain === "week") return `w/c ${dateOnly(bucket)}`;
  return short ? weekdayShort(bucket) : dateOnly(bucket);
}

export function TrendChart({
  points,
  grain,
  height = 190,
  partialLast = false,
  compare,
  compareLabel = "Previous",
}: {
  points: TrendPoint[];
  grain: Grain;
  height?: number;
  /** The last bucket is still filling up (today / this week): draw it dashed. */
  partialLast?: boolean;
  /** The period before, drawn as a quiet line. Aligned from the end, so the latest buckets line up. */
  compare?: TrendPoint[];
  compareLabel?: string;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [active, setActive] = useState<number | null>(null);
  const gradId = useId().replace(/:/g, "");
  const pad = { top: 14, right: 12, bottom: 30, left: 44 };
  const innerW = Math.max(0, width - pad.left - pad.right);
  const innerH = height - pad.top - pad.bottom;
  const shift = compare ? points.length - compare.length : 0;
  const compareAt = (i: number) => compare?.[i - shift];
  const max = niceMax(
    Math.max(
      ...points.map((p) => p.total_paise),
      ...(compare ?? []).map((p) => p.total_paise),
      0,
    ),
  );
  const x = (i: number) =>
    pad.left +
    (points.length <= 1 ? innerW / 2 : (i / (points.length - 1)) * innerW);
  const y = (v: number) => pad.top + innerH - (v / max) * innerH;

  const { linePath, areaPath, comparePath } = useMemo(() => {
    const l = d3line<TrendPoint>()
      .x((_, i) => x(i))
      .y((d) => y(d.total_paise))
      .curve(curveMonotoneX);
    const a = d3area<TrendPoint>()
      .x((_, i) => x(i))
      .y0(pad.top + innerH)
      .y1((d) => y(d.total_paise))
      .curve(curveMonotoneX);
    // Only the part of the earlier period that falls inside this chart's buckets.
    const offset = Math.max(0, -shift);
    const visible = (compare ?? []).slice(offset);
    const c = d3line<TrendPoint>()
      .x((_, i) => x(i + offset + shift))
      .y((d) => y(d.total_paise))
      .curve(curveMonotoneX);
    return {
      linePath: l(points) ?? "",
      areaPath: a(points) ?? "",
      comparePath: visible.length > 1 ? (c(visible) ?? "") : "",
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [points, compare, width, max, partialLast]);

  const ticks = [0, max / 2, max];
  const short = grain === "day" && points.length <= 8;
  const last = points.length - 1;
  const labelText = (i: number) =>
    bucketLabel(points[i].label_bucket ?? points[i].bucket, grain, short);
  // Axis labels are 12px Plex Mono, about 7.2px a character.
  const labelW = (i: number) => labelText(i).length * 7.2;
  const widest = Math.max(0, ...points.map((_, i) => labelW(i)));
  const labelEvery = Math.max(
    1,
    Math.ceil(points.length / Math.max(2, Math.floor(innerW / (widest + 16)))),
  );
  // Where a label starts, from its anchor: the first and last hug the edges unless the axis is short.
  const labelLeft = (i: number) =>
    i === 0 && !short
      ? x(i)
      : i === last && !short
        ? x(i) - labelW(i)
        : x(i) - labelW(i) / 2;
  // The last label always shows; drop regular labels that would collide with it.
  const labelled = (i: number) =>
    i === last ||
    (i % labelEvery === 0 && labelLeft(i) + labelW(i) + 8 <= labelLeft(last));
  const dashFrom = partialLast && points.length > 1 ? x(last - 1) : null;
  const clipId = `${gradId}-clip`;
  const shown = active ?? null;

  function onMove(clientX: number, rect: DOMRect) {
    const rel = clientX - rect.left - pad.left;
    const i = Math.round((rel / Math.max(1, innerW)) * last);
    setActive(Math.max(0, Math.min(last, i)));
  }

  function onKey(e: KeyboardEvent) {
    if (e.key === "ArrowRight")
      setActive((a) => Math.min(last, (a ?? last) + (a === null ? 0 : 1)));
    else if (e.key === "ArrowLeft")
      setActive((a) => Math.max(0, (a ?? last) - 1));
    else if (e.key === "Escape") setActive(null);
  }

  return (
    <div ref={ref} className="relative w-full" style={{ height }}>
      {width > 0 && points.length > 0 ? (
        <svg
          width={width}
          height={height}
          role="img"
          aria-label={`Collections by ${grain}, ${points.length} points. Use arrow keys to read values.`}
          tabIndex={0}
          onKeyDown={onKey}
          onBlur={() => setActive(null)}
          onPointerMove={(e) =>
            onMove(e.clientX, e.currentTarget.getBoundingClientRect())
          }
          onPointerLeave={() => setActive(null)}
          className="block touch-pan-y overflow-visible outline-none"
        >
          <defs>
            <linearGradient id={gradId} x1="0" x2="0" y1="0" y2="1">
              <stop offset="0%" stopColor={SERIES} stopOpacity={0.14} />
              <stop offset="100%" stopColor={SERIES} stopOpacity={0} />
            </linearGradient>
            {dashFrom !== null ? (
              <>
                <clipPath id={`${clipId}-solid`}>
                  <rect x={0} y={0} width={dashFrom} height={height} />
                </clipPath>
                <clipPath id={`${clipId}-dash`}>
                  <rect
                    x={dashFrom}
                    y={0}
                    width={width - dashFrom}
                    height={height}
                  />
                </clipPath>
              </>
            ) : null}
          </defs>
          {ticks.map((t) => (
            <g key={t}>
              <line
                x1={pad.left}
                x2={width - pad.right}
                y1={y(t)}
                y2={y(t)}
                stroke={GRID}
                strokeWidth={1}
              />
              <text
                x={pad.left - 8}
                y={y(t)}
                dy="0.32em"
                textAnchor="end"
                className="fill-faint num text-xs"
              >
                {inrCompact(t)}
              </text>
            </g>
          ))}
          {comparePath ? (
            <path
              d={comparePath}
              fill="none"
              stroke={COMPARE}
              strokeWidth={1.5}
              strokeLinejoin="round"
              strokeLinecap="round"
            />
          ) : null}
          <path d={areaPath} fill={`url(#${gradId})`} />
          <path
            d={linePath}
            fill="none"
            stroke={SERIES}
            strokeWidth={2}
            strokeLinejoin="round"
            strokeLinecap="round"
            clipPath={dashFrom !== null ? `url(#${clipId}-solid)` : undefined}
          />
          {dashFrom !== null ? (
            <>
              {/* The still-filling bucket: same curve, dashed. */}
              <path
                d={linePath}
                fill="none"
                stroke={SERIES}
                strokeWidth={2}
                strokeDasharray="4 5"
                strokeLinecap="round"
                clipPath={`url(#${clipId}-dash)`}
              />
              {/* Left of the point at its own height, with a halo, so neither line can run through it. */}
              <text
                x={x(last) - 12}
                y={y(points[last].total_paise)}
                dy="0.32em"
                textAnchor="end"
                stroke={SURFACE}
                strokeWidth={4}
                strokeLinejoin="round"
                paintOrder="stroke"
                className="fill-muted num text-xs"
              >
                so far
              </text>
            </>
          ) : null}
          {points.map((p, i) =>
            labelled(i) ? (
              <text
                key={p.bucket}
                x={x(i)}
                y={height - 8}
                textAnchor={
                  i === 0 && !short
                    ? "start"
                    : i === last && !short
                      ? "end"
                      : "middle"
                }
                className="fill-muted num text-xs"
              >
                {labelText(i)}
              </text>
            ) : null,
          )}
          {shown !== null ? (
            <line
              x1={x(shown)}
              x2={x(shown)}
              y1={pad.top}
              y2={pad.top + innerH}
              stroke="rgba(31,27,22,0.22)"
            />
          ) : null}
          <circle
            cx={x(shown ?? last)}
            cy={y(points[shown ?? last].total_paise)}
            r={5}
            fill={SERIES}
            stroke={SURFACE}
            strokeWidth={2}
          />
        </svg>
      ) : null}
      {shown !== null && width > 0 ? (
        <div
          className="pointer-events-none absolute top-0 z-10 rounded-[10px] border border-line-strong bg-card px-3 py-2 text-sm shadow-[0_8px_24px_rgb(31_27_22/0.08)]"
          style={{
            left: Math.min(Math.max(x(shown) - 70, 0), width - 150),
            transform: "translateY(-6px)",
          }}
        >
          <p className="text-muted">
            {bucketLabel(
              points[shown].label_bucket ?? points[shown].bucket,
              grain,
              false,
            )}
          </p>
          <p className="num font-medium text-text">
            {inr(points[shown].total_paise)}
          </p>
          <p className="text-muted">
            {points[shown].txn_count} payment
            {points[shown].txn_count === 1 ? "" : "s"}
          </p>
          {compareAt(shown) ? (
            <p className="num text-faint">
              {compareLabel}: {inr(compareAt(shown)!.total_paise)}
            </p>
          ) : null}
        </div>
      ) : null}
      <div className="sr-only">
        <table>
          <caption>Collections by {grain}</caption>
          <tbody>
            {points.map((p) => (
              <tr key={p.bucket}>
                <th scope="row">
                  {bucketLabel(p.label_bucket ?? p.bucket, grain, false)}
                </th>
                <td>{inr(p.total_paise)}</td>
                <td>{p.txn_count} payments</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const HOURS = Array.from({ length: 17 }, (_, i) => i + 6); // 6 AM – 10 PM

export function PeakHoursHeatmap({ cells }: { cells: HeatCell[] }) {
  const [hover, setHover] = useState<HeatCell | null>(null);
  const byKey = new Map(cells.map((c) => [`${c.dow}-${c.hour}`, c]));
  const max = Math.max(1, ...cells.map((c) => c.total_paise));
  // One hue, paper -> full blue, linear so the busiest hours stand out on paper; the floor keeps quiet hours visible.
  const alpha = (v: number) => (v <= 0 ? 0.04 : 0.08 + 0.92 * (v / max));

  return (
    <div className="relative">
      <div
        className="grid gap-1"
        style={{
          gridTemplateColumns: `2.5rem repeat(${HOURS.length}, minmax(0, 1fr))`,
        }}
      >
        {DOW.map((d, di) => (
          <div key={d} className="contents">
            <div className="pr-2 text-right num text-xs leading-5 text-faint">
              {d}
            </div>
            {HOURS.map((h) => {
              const cell = byKey.get(`${di + 1}-${h}`) ?? {
                dow: di + 1,
                hour: h,
                total_paise: 0,
                txn_count: 0,
              };
              return (
                <div
                  key={h}
                  onPointerEnter={() => setHover(cell)}
                  onPointerLeave={() => setHover(null)}
                  className="h-5 rounded-[4px]"
                  style={{
                    background: `rgba(${HEAT},${alpha(cell.total_paise)})`,
                  }}
                />
              );
            })}
          </div>
        ))}
        <div />
        {HOURS.map((h) => (
          <div key={h} className="pt-1 text-center num text-xs whitespace-nowrap text-faint">
            {h % 3 === 0 ? hourLabel(h).replace(" ", "") : ""}
          </div>
        ))}
      </div>
      <div className="mt-4 flex flex-wrap items-center justify-between gap-3 text-sm text-faint">
        <span aria-live="polite" className="min-h-4">
          {hover
            ? `${DOW[hover.dow - 1]} ${hourLabel(hover.hour)}: ${inr(hover.total_paise)} from ${hover.txn_count} payment${hover.txn_count === 1 ? "" : "s"}`
            : "Last 4 weeks. Point at a square for the total."}
        </span>
        <span className="flex shrink-0 items-center gap-1">
          Less
          {[0.08, 0.3, 0.55, 0.8, 1].map((a) => (
            <span
              key={a}
              className="h-3 w-3 rounded-[3px]"
              style={{ background: `rgba(${HEAT},${a})` }}
            />
          ))}
          More
        </span>
      </div>
      <div className="sr-only">
        <table>
          <caption>Takings by weekday and hour, last 4 weeks</caption>
          <tbody>
            {cells.map((c) => (
              <tr key={`${c.dow}-${c.hour}`}>
                <th scope="row">
                  {DOW[c.dow - 1]} {hourLabel(c.hour)}
                </th>
                <td>{inr(c.total_paise)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function MonthlyBars({
  months,
  height = 150,
}: {
  months: { month: string; total_paise: number; txn_count: number }[];
  height?: number;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const pad = { top: 24, bottom: 26, side: 8 };
  const innerH = height - pad.top - pad.bottom;
  const max = niceMax(Math.max(...months.map((m) => m.total_paise), 0));
  const band = months.length ? (width - pad.side * 2) / months.length : 0;
  const barW = Math.min(24, band * 0.5);
  const lastIdx = months.length - 1;
  const labelIdx = hover ?? lastIdx;

  return (
    <div ref={ref} className="relative w-full" style={{ height }}>
      {width > 0 ? (
        <svg
          width={width}
          height={height}
          role="img"
          aria-label="Monthly totals, last 6 months"
          className="block"
        >
          <line
            x1={0}
            x2={width}
            y1={pad.top + innerH}
            y2={pad.top + innerH}
            stroke={GRID}
          />
          {months.map((m, i) => {
            const h = (m.total_paise / max) * innerH;
            const cx = pad.side + band * i + band / 2;
            const top = pad.top + innerH - h;
            const r = Math.min(4, h);
            const x0 = cx - barW / 2;
            const path =
              h > 0
                ? `M${x0},${pad.top + innerH} V${top + r} Q${x0},${top} ${x0 + r},${top} H${x0 + barW - r} Q${x0 + barW},${top} ${x0 + barW},${top + r} V${pad.top + innerH} Z`
                : "";
            return (
              <g
                key={m.month}
                onPointerEnter={() => setHover(i)}
                onPointerLeave={() => setHover(null)}
              >
                <rect
                  x={cx - band / 2}
                  y={0}
                  width={band}
                  height={height}
                  fill="transparent"
                />
                {path ? (
                  <path
                    d={path}
                    fill={SERIES}
                    opacity={hover === null || hover === i ? 1 : 0.45}
                  />
                ) : null}
                {i === labelIdx && m.total_paise > 0 ? (
                  <text
                    x={cx}
                    y={top - 7}
                    textAnchor="middle"
                    className="fill-text num text-xs font-medium"
                  >
                    {inrCompact(m.total_paise)}
                  </text>
                ) : null}
                <text
                  x={cx}
                  y={height - 6}
                  textAnchor="middle"
                  className="fill-muted num text-xs"
                >
                  {dateOnly(`${m.month}-01`, { month: "short" })}
                </text>
              </g>
            );
          })}
        </svg>
      ) : null}
      <div className="sr-only">
        <table>
          <caption>Monthly totals</caption>
          <tbody>
            {months.map((m) => (
              <tr key={m.month}>
                <th scope="row">{m.month}</th>
                <td>{inr(m.total_paise)}</td>
                <td>{m.txn_count} payments</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
