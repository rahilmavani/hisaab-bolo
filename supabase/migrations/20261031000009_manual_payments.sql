-- Money that reached the shop outside Paytm (straight to its UPI ID, cash): the merchant marks the
-- invoice paid and the payment is recorded with source 'manual'.
alter table public.transactions drop constraint transactions_source_check;
alter table public.transactions add constraint transactions_source_check
  check (source in ('seed', 'paytm_link', 'paytm_qr', 'mock', 'manual'));
