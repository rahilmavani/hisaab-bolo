-- Recurring dues go to the schedule's own email (the one typed in the "mark recurring"
-- popup); no email means tracking only, even if the customer has one on file.
alter table public.recurring_schedules add column email text;

-- A due that went overdue keeps its reminder pending until an email actually goes out,
-- so one failed send is retried on the next tick instead of being lost.
alter table public.invoices add column overdue_reminder_pending boolean not null default false;

-- Where the seed put the demo clock; "reset" returns here rather than to wall time.
alter table public.app_clock add column base_override_now timestamptz, add column base_set_at timestamptz;
