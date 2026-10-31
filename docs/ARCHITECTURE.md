# Hisaab Bolo — Architecture (build reference for Claude Code)

The full version, with diagrams, is the team's Claude Doc "Hisaab Bolo — Technical Architecture". This file is the build contract. The Paytm details are in `docs/paytm/PAYTM_INTEGRATION.md`.

## Product (Paytm Build for India, team PiedPiper)

Merchant growth AI on top of Paytm payments. There are four must-work demo moments:
1. **Voice query:** "Ram ne pichhle 4 mahine mein kitna diya?" is answered with a spoken reply plus the payments behind it.
2. **Invoice to paid:** an email goes out with a Paytm link and QR, the payment is made, and the invoice flips to Paid live.
3. **Per-payer ledger dashboard:** KPIs, trend, peak hours, customer search and a payer profile.
4. **Recurring plus auto-reminder:** the merchant marks a payer recurring, and a missed due triggers a reminder email.

## Stack

- Next.js (App Router, TypeScript, Tailwind) on Vercel.
- FastAPI (Python 3.12) on Railway, plus a Railway cron service.
- Supabase: Postgres, Auth, Storage and Realtime.
- External APIs: Paytm staging (behind `PAYTM_MODE=mock` until a merchant ID exists), Sarvam AI (Saaras STT in `codemix` mode, Bulbul TTS), Anthropic (`claude-haiku-5-5` by default, `claude-sonnet-5-5` as fallback) and Resend (no domain, so mail only reaches the account inbox).
- Not used: WhatsApp, React Native, TimescaleDB, Redis, AWS/SES, mem0, streaming voice.

## Decisions

| Area | Decision |
| --- | --- |
| Identity | One ledger per person. `payer_identifiers` (vpa, phone, email, paytm_cust_id) maps many identifiers to one payer. Merging moves identifiers. Paytm APIs do NOT return the payer VPA. |
| Data | Seeded synthetic history: about 40 payers, 90 days, about 8k rows, including Ram (₹100 daily around 8:30), Sharma Traders (B2B invoices) and one regular who stopped 12 days ago. Live payments resolve the payer via invoice order ID. |
| Recurring | Track, auto-create dues and auto-remind. An optional email popup appears when marking recurring. With no email, the schedule only tracks (dues plus a missed flag). |
| GST | A taxable flag plus payer GSTIN on B2B invoices. No tax maths. |
| Reconciliation | Exact match on Paytm order ID only. One order ID, link and QR per invoice. |
| Agent | Typed tools. Write actions create a pending action that needs a confirm tap. The model never writes. |
| Voice | Web push-to-talk. A text box fallback hits the same agent. |
| Dynamic UI | A fixed component set: `stat_cards`, `line_chart`, `table`, `payer_card`, `invoice_card`, `confirm_action`. The model picks a type and a tool-result ID, and the server fills the rows. |

## Data model (Supabase Postgres)

All money is stored as integer paise, all times as timestamptz (displayed in IST), and every table has `merchant_id`. RLS is on.

- `merchants(id, name, paytm_mid, owner_user_id)`
- `payers(id, merchant_id, display_name, email?, phone?, gstin?, notes, merged_into?)`: a pg_trgm index on display_name.
- `payer_identifiers(payer_id, merchant_id, kind, value)`: unique (merchant_id, kind, value).
- `transactions(id, merchant_id, payer_id?, amount_paise, paid_at, source[seed|paytm_link|paytm_qr|mock], paytm_order_id, paytm_txn_id, payment_mode, description, description_by[ai|merchant], category, gst_taxable, invoice_id?, raw jsonb)`: indexes on (merchant_id, paid_at) and (payer_id, paid_at).
- `invoices(id, merchant_id, number, payer_id, amount_paise, line_items jsonb, gst_taxable, payer_gstin?, due_date, status[draft|sent|overdue|paid|cancelled], paytm_order_id unique, qr_order_id?, link_id, link_url, qr_image_url, recurring_id?, paytm_raw jsonb, sent_at, paid_at)`
- `recurring_schedules(id, merchant_id, payer_id, amount_paise, cadence[weekly|monthly], anchor_date, next_due_date, grace_days, auto_remind, active)`
- `reminders(id, invoice_id, kind[invoice|overdue], sent_at, resend_id)`
- `webhook_events(id, provider, external_id, payload jsonb, processed_at, error)`: unique (provider, external_id).
- `agent_queries(id, merchant_id, transcript, lang, tool_calls jsonb, answer, latency_ms, cost_paise)`
- `pending_actions(token, merchant_id, tool, args jsonb, expires_at, confirmed_at)`
- `app_clock(id=1, override_now?)`: every job and query uses `now()` from here (the demo time jump).
- Views: `daily_totals`, `hourly_heatmap`, `payer_stats`. `payer_stats` has count, total, median amount, typical hour, last_paid_at and median gap in days.

## Agent tools (`merchant_id` is injected server-side and is never a parameter)

| Tool | Kind | Component |
| --- | --- | --- |
| `find_payer(name)` | read | chips if more than one match |
| `get_payer_summary(payer_id, from, to)` | read | `payer_card` |
| `list_transactions(payer_id?, from, to, limit)` | read | `table` |
| `get_sales_summary(from, to, grain)` | read | `line_chart` + `stat_cards` |
| `list_dues(status)` | read | `table` |
| `create_invoice(payer_id, amount, due_date, items, email?)` | action | `confirm_action` |
| `send_reminder(invoice_id)` | action | `confirm_action` |
| `set_recurring(payer_id, cadence, amount, email?)` | action | `confirm_action` |

Voice path:
1. Record with `MediaRecorder`.
2. `POST /voice/ask`. If Sarvam rejects webm, convert to 16 kHz WAV with ffmpeg.
3. Sarvam STT.
4. Claude tool loop, at most 4 turns, with the system prompt and tools prompt-cached. The prompt includes today's date in IST.
5. The agent returns JSON `{speech, display, components[], pending_action?}`.
6. **Number guard:** every number in `speech` must appear in a tool result; otherwise retry once on Sonnet, then fall back to showing the data with no speech.
7. Bulbul TTS. Text renders first, and the audio plays when ready.

Target: about 5 seconds.

## API surface (FastAPI; Supabase JWT on all routes except webhooks, cron and mock)

- `GET /dashboard/summary?date=`
- `GET /payers?q=` · `GET /payers/{id}` · `PATCH /payers/{id}` · `POST /payers/{id}/merge`
- `PATCH /transactions/{id}`
- `POST /payers/{id}/recurring`
- `POST /invoices` · `POST /invoices/{id}/remind` · `POST /invoices/{id}/check`
- `POST /voice/ask` · `POST /ask` · `POST /actions/{token}/confirm`
- `POST /webhooks/paytm/link` · `POST /webhooks/paytm/payment` (form key-value, checksum-verified, idempotent)
- `POST /jobs/tick` (cron every 5 minutes: recurring dues, overdue reminders, status polling, AI descriptions, alerts)
- `POST /admin/clock` · `GET /mock-pay/{order_id}` (mock mode only)

## Low-friction ledger

- **AI description:** Haiku writes up to 8 words plus a category from the amount, time, link/invoice notes and that payer's last 5 descriptions. Merchant edits win.
- **Lost-regular alert:** `now - last_paid_at > 3 × median gap` and at least 10 payments.
- **Slow-day banner:** today up to this hour is at least 30% below the same weekday's 4-week average to the same hour.
- **Recurring suggestion chip (stretch):** steady amount with a gap of 1, 7 or about 30 days.

## Collections flow

1. An invoice is created (from the form or a confirmed voice action) and gets a new order ID.
2. Paytm creates the link (with `linkOrderId` set to the order ID and `statusCallbackUrl` pointing at us) and the dynamic QR.
3. The QR PNG goes to Supabase Storage, and Resend sends the email with the pay button and the QR image URL.
4. The webhook arrives. We verify the checksum, dedupe on TXNID, match ORDERID and the amount, then mark the invoice paid, insert the transaction and add identifiers.
5. Realtime flips the dashboard.

Overdue (`due + grace`) triggers a reminder email with the same link. Recurring schedules with an email create invoices per due date; without an email they only track.

## Risks to design for

- The Paytm webhook may never arrive: a "Check payment" button and cron polling cover it.
- The Paytm merchant ID doesn't exist yet: mock mode.
- Resend without a domain: all demo customer emails go to the team inbox.
- Speech-to-text mishears names: trigram search plus disambiguation chips.
- Wi-Fi or mic failure: the text fallback.
- The recurring demo needs time to pass: the clock jump.
