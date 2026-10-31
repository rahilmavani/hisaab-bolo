-- Hisaab Bolo schema. Money is integer paise; times are timestamptz (shown in IST).
-- Every business table carries merchant_id. RLS is on everywhere; the FastAPI
-- service connects as the table owner and scopes every query by merchant_id.

create extension if not exists pg_trgm with schema extensions;

-- ---------------------------------------------------------------------------
-- Demo clock: every job and query reads "now" through app_now().
-- override_now is the jumped-to instant; time keeps flowing from set_at.
-- ---------------------------------------------------------------------------
create table public.app_clock (
  id int primary key default 1 check (id = 1),
  override_now timestamptz,
  set_at timestamptz
);
insert into public.app_clock (id) values (1);

create or replace function public.app_now() returns timestamptz
language sql stable
set search_path = public
as $$
  select coalesce(
    (select c.override_now + (now() - c.set_at)
       from public.app_clock c
      where c.id = 1 and c.override_now is not null),
    now())
$$;

create or replace function public.app_today() returns date
language sql stable
set search_path = public
as $$ select (public.app_now() at time zone 'Asia/Kolkata')::date $$;

-- ---------------------------------------------------------------------------
-- Core tables
-- ---------------------------------------------------------------------------
create table public.merchants (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  paytm_mid text,
  owner_user_id uuid unique references auth.users (id) on delete set null,
  invoice_seq int not null default 0,
  created_at timestamptz not null default now()
);

create table public.payers (
  id uuid primary key default gen_random_uuid(),
  merchant_id uuid not null references public.merchants (id) on delete cascade,
  display_name text not null,
  email text,
  phone text,
  gstin text,
  notes text not null default '',
  merged_into uuid references public.payers (id) on delete set null,
  created_at timestamptz not null default now()
);
create index payers_merchant_idx on public.payers (merchant_id);
create index payers_name_trgm_idx on public.payers using gin (display_name extensions.gin_trgm_ops);

create table public.payer_identifiers (
  id bigint generated always as identity primary key,
  payer_id uuid not null references public.payers (id) on delete cascade,
  merchant_id uuid not null references public.merchants (id) on delete cascade,
  kind text not null check (kind in ('vpa', 'phone', 'email', 'paytm_cust_id')),
  value text not null,
  created_at timestamptz not null default now(),
  unique (merchant_id, kind, value)
);
create index payer_identifiers_payer_idx on public.payer_identifiers (payer_id);

create table public.recurring_schedules (
  id uuid primary key default gen_random_uuid(),
  merchant_id uuid not null references public.merchants (id) on delete cascade,
  payer_id uuid not null references public.payers (id) on delete cascade,
  amount_paise bigint not null check (amount_paise >= 100),
  cadence text not null check (cadence in ('weekly', 'monthly')),
  anchor_date date not null,
  next_due_date date not null,
  grace_days int not null default 3 check (grace_days >= 0),
  auto_remind boolean not null default true,
  active boolean not null default true,
  created_at timestamptz not null default now()
);
create index recurring_merchant_idx on public.recurring_schedules (merchant_id, active);
create unique index recurring_one_active_per_payer on public.recurring_schedules (payer_id) where active;

create table public.invoices (
  id uuid primary key default gen_random_uuid(),
  merchant_id uuid not null references public.merchants (id) on delete cascade,
  number text not null,
  payer_id uuid not null references public.payers (id) on delete cascade,
  amount_paise bigint not null check (amount_paise >= 100),
  line_items jsonb not null default '[]'::jsonb,
  gst_taxable boolean not null default false,
  payer_gstin text,
  due_date date not null,
  grace_days int not null default 0 check (grace_days >= 0),
  status text not null default 'draft'
    check (status in ('draft', 'sent', 'overdue', 'paid', 'cancelled')),
  email_to text,
  paytm_order_id text not null unique,
  qr_order_id text unique,
  link_id text,
  link_url text,
  qr_image_url text,
  recurring_id uuid references public.recurring_schedules (id) on delete set null,
  paytm_raw jsonb not null default '{}'::jsonb,
  sent_at timestamptz,
  paid_at timestamptz,
  created_at timestamptz not null default now(),
  unique (merchant_id, number)
);
create index invoices_merchant_status_idx on public.invoices (merchant_id, status, due_date);
create index invoices_payer_idx on public.invoices (payer_id);
create index invoices_link_idx on public.invoices (link_id);

create table public.transactions (
  id uuid primary key default gen_random_uuid(),
  merchant_id uuid not null references public.merchants (id) on delete cascade,
  payer_id uuid references public.payers (id) on delete set null,
  amount_paise bigint not null check (amount_paise > 0),
  paid_at timestamptz not null,
  source text not null check (source in ('seed', 'paytm_link', 'paytm_qr', 'mock')),
  paytm_order_id text,
  paytm_txn_id text,
  payment_mode text,
  description text,
  description_by text check (description_by in ('ai', 'merchant')),
  category text,
  gst_taxable boolean not null default false,
  invoice_id uuid references public.invoices (id) on delete set null,
  raw jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
create index transactions_merchant_paid_idx on public.transactions (merchant_id, paid_at);
create index transactions_payer_paid_idx on public.transactions (payer_id, paid_at);
create unique index transactions_txn_id_uniq on public.transactions (paytm_txn_id) where paytm_txn_id is not null;
create index transactions_needs_description_idx on public.transactions (merchant_id) where description is null;

create table public.reminders (
  id uuid primary key default gen_random_uuid(),
  merchant_id uuid not null references public.merchants (id) on delete cascade,
  invoice_id uuid not null references public.invoices (id) on delete cascade,
  kind text not null check (kind in ('invoice', 'overdue')),
  to_email text,
  sent_at timestamptz not null default now(),
  resend_id text
);
create index reminders_invoice_idx on public.reminders (invoice_id, kind);

create table public.webhook_events (
  id bigint generated always as identity primary key,
  provider text not null,
  external_id text not null,
  merchant_id uuid references public.merchants (id) on delete cascade,
  route text,
  payload jsonb not null,
  received_at timestamptz not null default now(),
  processed_at timestamptz,
  error text,
  unique (provider, external_id)
);

create table public.agent_queries (
  id uuid primary key default gen_random_uuid(),
  merchant_id uuid not null references public.merchants (id) on delete cascade,
  transcript text not null,
  lang text,
  tool_calls jsonb not null default '[]'::jsonb,
  answer jsonb,
  model text,
  latency_ms int,
  cost_paise int,
  created_at timestamptz not null default now()
);

create table public.pending_actions (
  token text primary key,
  merchant_id uuid not null references public.merchants (id) on delete cascade,
  tool text not null check (tool in ('create_invoice', 'send_reminder', 'set_recurring')),
  args jsonb not null,
  summary jsonb not null default '{}'::jsonb,
  result jsonb,
  expires_at timestamptz not null,
  confirmed_at timestamptz,
  created_at timestamptz not null default now()
);

-- Mock Paytm state (PAYTM_MODE=mock only): what the fake gateway "knows".
create table public.paytm_mock_orders (
  order_id text primary key,
  merchant_id uuid references public.merchants (id) on delete cascade,
  kind text not null check (kind in ('link', 'qr')),
  link_id text,
  amount_paise bigint not null,
  status text not null default 'PENDING' check (status in ('PENDING', 'TXN_SUCCESS', 'TXN_FAILURE')),
  txn_id text,
  payment_mode text,
  txn_at timestamptz,
  created_at timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Invoice numbers: INV-0001, per merchant.
-- ---------------------------------------------------------------------------
create or replace function public.next_invoice_number(p_merchant uuid) returns text
language sql volatile
set search_path = public
as $$
  update public.merchants set invoice_seq = invoice_seq + 1
   where id = p_merchant
  returning 'INV-' || lpad(invoice_seq::text, 4, '0')
$$;

-- ---------------------------------------------------------------------------
-- Views (security_invoker so RLS applies to callers)
-- ---------------------------------------------------------------------------
create view public.daily_totals with (security_invoker = true) as
select merchant_id,
       (paid_at at time zone 'Asia/Kolkata')::date as day,
       sum(amount_paise)::bigint as total_paise,
       count(*)::int as txn_count,
       count(distinct payer_id)::int as payer_count
  from public.transactions
 group by merchant_id, (paid_at at time zone 'Asia/Kolkata')::date;

-- Last 28 days up to the app clock; dow is ISO (1 = Monday).
create view public.hourly_heatmap with (security_invoker = true) as
select merchant_id,
       extract(isodow from paid_at at time zone 'Asia/Kolkata')::int as dow,
       extract(hour from paid_at at time zone 'Asia/Kolkata')::int as hour,
       sum(amount_paise)::bigint as total_paise,
       count(*)::int as txn_count
  from public.transactions
 where paid_at > public.app_now() - interval '28 days'
   and paid_at <= public.app_now()
 group by 1, 2, 3;

create view public.payer_stats with (security_invoker = true) as
with t as (
  select merchant_id, payer_id, amount_paise, paid_at,
         extract(epoch from paid_at - lag(paid_at) over (partition by payer_id order by paid_at)) / 86400.0 as gap_days,
         (extract(hour from paid_at at time zone 'Asia/Kolkata') * 60
          + extract(minute from paid_at at time zone 'Asia/Kolkata'))::int as minute_of_day
    from public.transactions
   where payer_id is not null
)
select payer_id,
       merchant_id,
       count(*)::int as txn_count,
       sum(amount_paise)::bigint as total_paise,
       (percentile_cont(0.5) within group (order by amount_paise))::bigint as median_amount_paise,
       mode() within group (order by minute_of_day / 60) as typical_hour,
       (percentile_cont(0.5) within group (order by minute_of_day))::int as typical_minute,
       min(paid_at) as first_paid_at,
       max(paid_at) as last_paid_at,
       round((percentile_cont(0.5) within group (order by gap_days))::numeric, 2) as median_gap_days
  from t
 group by payer_id, merchant_id;

-- ---------------------------------------------------------------------------
-- RLS: owners can read their merchant's rows (dashboard realtime uses this).
-- All writes go through the API's service connection.
-- ---------------------------------------------------------------------------
create or replace function public.my_merchant_ids() returns setof uuid
language sql stable security definer
set search_path = public
as $$ select id from public.merchants where owner_user_id = auth.uid() $$;

alter table public.app_clock enable row level security;
alter table public.merchants enable row level security;
alter table public.payers enable row level security;
alter table public.payer_identifiers enable row level security;
alter table public.recurring_schedules enable row level security;
alter table public.invoices enable row level security;
alter table public.transactions enable row level security;
alter table public.reminders enable row level security;
alter table public.webhook_events enable row level security;
alter table public.agent_queries enable row level security;
alter table public.pending_actions enable row level security;
alter table public.paytm_mock_orders enable row level security;

create policy merchants_owner_read on public.merchants
  for select to authenticated using (owner_user_id = (select auth.uid()));

create policy payers_owner_read on public.payers
  for select to authenticated using (merchant_id in (select public.my_merchant_ids()));
create policy payer_identifiers_owner_read on public.payer_identifiers
  for select to authenticated using (merchant_id in (select public.my_merchant_ids()));
create policy recurring_owner_read on public.recurring_schedules
  for select to authenticated using (merchant_id in (select public.my_merchant_ids()));
create policy invoices_owner_read on public.invoices
  for select to authenticated using (merchant_id in (select public.my_merchant_ids()));
create policy transactions_owner_read on public.transactions
  for select to authenticated using (merchant_id in (select public.my_merchant_ids()));
create policy reminders_owner_read on public.reminders
  for select to authenticated using (merchant_id in (select public.my_merchant_ids()));
create policy agent_queries_owner_read on public.agent_queries
  for select to authenticated using (merchant_id in (select public.my_merchant_ids()));
create policy app_clock_read on public.app_clock
  for select to authenticated using (true);

revoke all on public.webhook_events, public.pending_actions, public.paytm_mock_orders from anon, authenticated;
revoke execute on function public.next_invoice_number(uuid) from anon, authenticated, public;

-- ---------------------------------------------------------------------------
-- Realtime: the dashboard listens for invoice status flips and new payments.
-- ---------------------------------------------------------------------------
alter publication supabase_realtime add table public.invoices, public.transactions, public.reminders;

-- ---------------------------------------------------------------------------
-- Storage: invoice QR PNGs are public so email clients can load them.
-- ---------------------------------------------------------------------------
insert into storage.buckets (id, name, public)
values ('qr', 'qr', true)
on conflict (id) do nothing;
