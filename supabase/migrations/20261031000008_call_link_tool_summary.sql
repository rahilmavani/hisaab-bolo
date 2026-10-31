-- During a reminder call the agent can email the Paytm link itself (Sarvam tool -> our API), and
-- it leaves a short summary of the conversation for the merchant.
alter table public.reminders
  add column call_summary text,
  -- Secret handed to the agent for this one call: the send-link tool only works with it, and only
  -- for this call's invoice.
  add column call_token text unique,
  add column call_link_sent_at timestamptz,
  -- On an email reminder: the call it was sent from (by the agent mid-call, or right after it).
  add column sent_during_call uuid references public.reminders (id) on delete set null;

-- Ask Hisaab can prepare a reminder call too (the merchant confirms it like any other action).
alter table public.pending_actions drop constraint pending_actions_tool_check;
alter table public.pending_actions add constraint pending_actions_tool_check
  check (tool in ('create_invoice', 'send_reminder', 'set_recurring', 'call_customer'));
