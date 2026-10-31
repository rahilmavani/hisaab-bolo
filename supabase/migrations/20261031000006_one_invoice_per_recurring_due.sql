-- One live invoice per customer and recurring due date. The 5-minute tick and "Mark recurring"
-- (or an Ask Hisaab confirm) can create dues at the same moment; the check-then-insert in the API
-- can't stop both, this index does. A cancelled due drops out, so it can be billed again.
create unique index invoices_one_per_recurring_due
  on public.invoices (payer_id, due_date)
  where recurring_id is not null and status <> 'cancelled';
