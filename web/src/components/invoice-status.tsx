import clsx from "clsx";
import type { InvoiceStatus } from "@/lib/types";

export function invoiceStatusLabel(status: InvoiceStatus, dueInDays: number): { text: string; tone: string } {
  if (status === "paid") return { text: "Paid · auto-matched", tone: "text-mint" };
  if (status === "cancelled") return { text: "Cancelled", tone: "text-faint" };
  if (status === "draft") return { text: "Draft", tone: "text-muted" };
  if (status === "overdue" || dueInDays < 0) {
    const d = Math.abs(dueInDays);
    return { text: `Overdue · ${d} day${d === 1 ? "" : "s"}`, tone: "text-amber" };
  }
  if (dueInDays === 0) return { text: "Due today", tone: "text-sky" };
  if (dueInDays === 1) return { text: "Due tomorrow", tone: "text-sky" };
  return { text: `Due in ${dueInDays} days`, tone: "text-muted" };
}

export function InvoiceStatusText({ status, dueInDays, className }: { status: InvoiceStatus; dueInDays: number; className?: string }) {
  const { text, tone } = invoiceStatusLabel(status, dueInDays);
  return <span className={clsx(tone, className)}>{text}</span>;
}
