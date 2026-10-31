-- app_now(): an override without set_at still applies (frozen at override_now).
create or replace function public.app_now() returns timestamptz
language sql stable
set search_path = public
as $$
  select coalesce(
    (select c.override_now + coalesce(now() - c.set_at, interval '0')
       from public.app_clock c
      where c.id = 1 and c.override_now is not null),
    now())
$$;

-- Drop the Supabase default table privileges the browser never needs.
revoke truncate, trigger, references on all tables in schema public from anon, authenticated;
