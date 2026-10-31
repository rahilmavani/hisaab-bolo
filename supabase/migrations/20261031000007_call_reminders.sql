-- Reminder calls: a Sarvam voice agent (over the merchant's Twilio number) calls the customer about
-- an unpaid invoice. A call is a reminder like an email, on another channel; Sarvam reports the
-- outcome to /webhooks/sarvam/call and the row is filled in then.
alter table public.reminders
  add column channel text not null default 'email' check (channel in ('email', 'call')),
  add column to_phone text,
  add column call_attempt_id text unique,
  add column call_status text check (call_status in ('queued', 'connected', 'no_answer', 'busy', 'failed')),
  add column call_duration_s int,
  add column call_outcome text,
  add column call_promised_date date,
  add column call_transcript jsonb,
  add column call_failure text,
  add column call_ended_at timestamptz;

-- Opt-in per invoice: call the customer when it stays unpaid after the overdue email.
-- Recurring dues take it from their schedule.
alter table public.invoices add column call_remind boolean not null default false;
alter table public.recurring_schedules add column call_remind boolean not null default false;
