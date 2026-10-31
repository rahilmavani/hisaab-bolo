import { dateOnly, istDate, istTime } from "@/lib/format";
import type { CallOutcome, CallStatus, Reminder } from "@/lib/types";

/** How a reminder call went, in the merchant's words. */
export function callStatusLabel(status: CallStatus | null, durationS?: number | null): { text: string; tone: string } {
  switch (status) {
    case "queued":
      return { text: "Calling…", tone: "text-sky" };
    case "connected":
      return { text: `Answered${durationS ? ` · ${durationS}s` : ""}`, tone: "text-mint" };
    case "no_answer":
      return { text: "No answer", tone: "text-amber" };
    case "busy":
      return { text: "Line busy", tone: "text-amber" };
    default:
      return { text: "Call failed", tone: "text-rose" };
  }
}

export function callOutcomeText(outcome: CallOutcome | null, promisedDate: string | null): string | null {
  switch (outcome) {
    case "will_pay":
      return promisedDate ? `Promised to pay by ${dateOnly(promisedDate)}` : "Promised to pay";
    case "already_paid":
      return "Says they've already paid";
    case "dispute":
      return "Disputes the invoice";
    case "call_later":
      return "Asked to be called later";
    case "no_commitment":
      return "Made no commitment";
    default:
      return promisedDate ? `Promised to pay by ${dateOnly(promisedDate)}` : null;
  }
}

/** One call in an invoice's history: status, what the customer said, and the transcript. */
export function CallHistoryItem({ r }: { r: Reminder }) {
  const status = callStatusLabel(r.call_status, r.call_duration_s);
  const outcome = callOutcomeText(r.call_outcome, r.call_promised_date);
  return (
    <li data-testid="call-history-item">
      <span className="text-text">{r.kind === "overdue" ? "Overdue reminder call" : "Reminder call"}</span>{" "}
      <span className="text-muted">
        {istDate(r.sent_at)}, {istTime(r.sent_at)}
        {r.to_phone ? ` to ${r.to_phone}` : ""}
      </span>
      <span className="block">
        <span className={status.tone}>{status.text}</span>
        {outcome ? <span className="text-text"> · {outcome}</span> : null}
        {r.call_failure && r.call_status !== "connected" ? (
          <span className="text-faint"> · {r.call_failure}</span>
        ) : null}
      </span>
      {r.call_transcript?.length ? (
        <details className="mt-1">
          <summary className="cursor-pointer text-xs text-muted hover:text-text">Transcript</summary>
          <ol className="mt-2 space-y-1.5 border-l border-line pl-3 text-xs">
            {r.call_transcript.map((line, i) => (
              <li key={i}>
                <span className="text-faint">{line.role === "agent" ? "Agent" : "Customer"}:</span>{" "}
                <span className="text-muted">{line.en_text}</span>
              </li>
            ))}
          </ol>
        </details>
      ) : null}
    </li>
  );
}
