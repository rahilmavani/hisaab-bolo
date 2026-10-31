import clsx from "clsx";
import type { InvoiceStatus } from "@/lib/types";

/** Overdue means past due + grace (the backend's rule); inside the grace period it is only "past due". */
/** markedPaid: the merchant confirmed money that came outside Paytm, so it wasn't matched automatically. */
export function invoiceStatusLabel(
  status: InvoiceStatus,
  dueInDays: number,
  graceDays = 0,
  markedPaid = false,
): { text: string; tone: string } {
  if (status === "paid")
    return {
      text: markedPaid ? "Paid · marked by you" : "Paid · auto-matched",
      tone: "text-mint",
    };
  if (status === "cancelled") return { text: "Cancelled", tone: "text-faint" };
  if (status === "draft") return { text: "Draft", tone: "text-muted" };
  const late = -dueInDays;
  if (late > graceDays || (status === "overdue" && late > 0)) {
    return {
      text: `Overdue · ${late} day${late === 1 ? "" : "s"}`,
      tone: "text-amber",
    };
  }
  if (late > 0)
    return {
      text: `Past due · ${late} day${late === 1 ? "" : "s"}, in grace`,
      tone: "text-text",
    };
  if (dueInDays === 0) return { text: "Due today", tone: "text-text" };
  if (dueInDays === 1) return { text: "Due tomorrow", tone: "text-text" };
  return { text: `Due in ${dueInDays} days`, tone: "text-muted" };
}

export function isOverdue(
  status: InvoiceStatus,
  dueInDays: number,
  graceDays = 0,
): boolean {
  return (
    ["sent", "draft", "overdue"].includes(status) && -dueInDays > graceDays
  );
}

export function InvoiceStatusText({
  status,
  dueInDays,
  graceDays = 0,
  markedPaid = false,
  className,
}: {
  status: InvoiceStatus;
  dueInDays: number;
  graceDays?: number;
  markedPaid?: boolean;
  className?: string;
}) {
  const { text, tone } = invoiceStatusLabel(
    status,
    dueInDays,
    graceDays,
    markedPaid,
  );
  return <span className={clsx(tone, className)}>{text}</span>;
}
