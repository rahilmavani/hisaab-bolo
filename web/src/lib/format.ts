const TZ = "Asia/Kolkata";

const inr0 = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 });
const inr2 = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", minimumFractionDigits: 2 });

/** ₹12,100 (whole rupees) or ₹1,234.50 when there are paise. */
export function inr(paise: number): string {
  return paise % 100 === 0 ? inr0.format(paise / 100) : inr2.format(paise / 100);
}

/** Axis-friendly: ₹0, ₹800, ₹12K, ₹1.2L */
export function inrCompact(paise: number): string {
  const r = paise / 100;
  if (r >= 1_00_000) return `₹${trim(r / 1_00_000)}L`;
  if (r >= 1_000) return `₹${trim(r / 1_000)}K`;
  return `₹${Math.round(r)}`;
}

function trim(n: number): string {
  return (Math.round(n * 10) / 10).toString(); // 12.5, 13, 1.2
}

export function pct(ratio: number, signed = false): string {
  const v = Math.round(ratio * 100);
  return `${signed && v > 0 ? "+" : ""}${v}%`;
}

export function istTime(iso: string): string {
  return new Intl.DateTimeFormat("en-IN", { timeZone: TZ, hour: "numeric", minute: "2-digit", hour12: true })
    .format(new Date(iso))
    .replace(" am", " AM")
    .replace(" pm", " PM");
}

export function istDate(iso: string, withYear = false): string {
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: TZ,
    day: "numeric",
    month: "short",
    ...(withYear ? { year: "numeric" } : {}),
  }).format(new Date(iso));
}

export function istDayLabel(iso: string): string {
  return new Intl.DateTimeFormat("en-IN", { timeZone: TZ, weekday: "short", day: "numeric", month: "short" }).format(
    new Date(iso),
  );
}

/** Calendar date strings (YYYY-MM-DD) are already in IST; parse them as local noon to avoid TZ drift. */
export function dateOnly(d: string, opts: Intl.DateTimeFormatOptions = { day: "numeric", month: "short" }): string {
  return new Intl.DateTimeFormat("en-IN", { timeZone: "UTC", ...opts }).format(new Date(`${d}T12:00:00Z`));
}

export function weekdayShort(d: string): string {
  return dateOnly(d, { weekday: "short" });
}

export function relativeDays(days: number): string {
  if (days === 0) return "today";
  if (days === 1) return "yesterday";
  return `${days} days ago`;
}

export function hourLabel(h: number): string {
  const suffix = h < 12 ? "AM" : "PM";
  return `${h % 12 || 12} ${suffix}`;
}

export function initials(name: string): string {
  const parts = name.trim().split(/\s+/);
  return (parts[0]?.[0] ?? "?").toUpperCase() + (parts.length > 1 ? (parts[parts.length - 1][0] ?? "").toUpperCase() : "");
}

/** YYYY-MM-DD of an instant, in IST. */
export function istISODate(iso: string): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit" }).format(
    new Date(iso),
  );
}
