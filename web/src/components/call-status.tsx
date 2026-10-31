import { MailCheck, Phone } from "lucide-react";
import { dateOnly, istDate, istTime } from "@/lib/format";
import type { CallOutcome, CallStatus, Reminder } from "@/lib/types";

/** How a reminder call went, in the merchant's words. */
export function callStatusLabel(status: CallStatus | null, durationS?: number | null): { text: string; tone: string } {
  switch (status) {
    case "queued":
      return { text: "Calling…", tone: "text-text" };
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

/** One call in an invoice's history: how it went, the agent's summary, whether the payment link
 *  went out (mid-call through the agent's tool, or right after), and the transcript. */
export function CallHistoryItem({ r, emails = [] }: { r: Reminder; emails?: Reminder[] }) {
  const status = callStatusLabel(r.call_status, r.call_duration_s);
  const outcome = callOutcomeText(r.call_outcome, r.call_promised_date);
  const linkEmail = emails[0];
  return (
    <li data-testid="call-history-item" className="rounded-xl border border-line p-3">
      <span className="flex items-center gap-1.5 text-text">
        <Phone size={14} aria-hidden className="text-blue" />
        {r.kind === "overdue" ? "Overdue reminder call" : "Reminder call"}
      </span>
      <span className="block text-muted">
        {istDate(r.sent_at)}, {istTime(r.sent_at)}
        {r.to_phone ? ` · ${r.to_phone}` : ""}
      </span>
      <span className="mt-1 block">
        <span className={status.tone}>{status.text}</span>
        {outcome ? <span className="text-text"> · {outcome}</span> : null}
        {r.call_failure && r.call_status !== "connected" ? (
          <span className="text-faint"> · {r.call_failure}</span>
        ) : null}
      </span>
      {r.call_summary ? <p className="mt-1.5 text-muted italic">&ldquo;{r.call_summary}&rdquo;</p> : null}
      {r.call_link_sent_at || linkEmail ? (
        <p className="mt-1.5 flex items-start gap-1.5 text-mint" data-testid="call-link-sent">
          <MailCheck size={14} aria-hidden className="mt-0.5 shrink-0" />
          <span>
            Payment link emailed {r.call_link_sent_at ? "during the call" : "after the call"}
            {linkEmail ? `, ${istTime(linkEmail.sent_at)}` : r.call_link_sent_at ? `, ${istTime(r.call_link_sent_at)}` : ""}
            {linkEmail?.to_email ? ` to ${linkEmail.to_email}` : ""}
          </span>
        </p>
      ) : null}
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
