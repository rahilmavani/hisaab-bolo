-- New Supabase projects do not grant Data API access by default. The browser only
-- reads (realtime + RLS-scoped selects); every write goes through the API.

grant usage on schema public to authenticated, service_role;

grant select on
  public.merchants, public.payers, public.payer_identifiers, public.recurring_schedules,
  public.invoices, public.transactions, public.reminders, public.agent_queries, public.app_clock,
  public.daily_totals, public.hourly_heatmap, public.payer_stats
to authenticated;

grant all on all tables in schema public to service_role;
grant usage, select on all sequences in schema public to service_role;

grant execute on function public.app_now(), public.app_today(), public.my_merchant_ids()
  to authenticated, service_role;
grant execute on function public.next_invoice_number(uuid) to service_role;
