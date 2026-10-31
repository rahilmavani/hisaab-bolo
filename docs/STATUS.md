# Hisaab Bolo: build status

_Last updated 31 Oct 2026, after the final end-to-end demo run in Chrome. Milestones M1–M6 are on `mvp`._

Hisaab Bolo runs end to end on a local stack: Next.js 16 web app, FastAPI, and a local Supabase (Postgres, Auth, Storage, Realtime). Claude (Haiku 5.5, with Sonnet 5.5 as the retry model), Sarvam (speech to text and text to speech) and Resend are called for real. Paytm runs in `PAYTM_MODE=mock`: a local checkout page sends checksum-signed webhooks through the same webhook route real Paytm will use.

## The four demo moments

All four ran end to end in Chrome on 31 Oct, with real Claude, Sarvam and Resend. Screenshots are in `evidence/m7/`.

| Moment | Result |
|---|---|
| Voice query ("Ram ne pichhle 4 mahine mein kitna diya?") | ✅ Typed in Chrome: "Ram ne pichhle 4 mahine (30 Jun se aaj tak) mein ₹12,100 diye, kul 121 payments", with Ram's card, and the spoken reply played. Spoken through the mic path (a recorded Hindi question, Sarvam speech to text): same answer, read aloud. Correct 11 of 11 times in the last tester round (text and voice; Hinglish, Devanagari, English). |
| Invoice → paid (email with Paytm link + QR, flips to Paid live) | ✅ INV-0415 (₹2,450, Kavita Verma) was created and its email delivered (Resend status "delivered"). Paying on the checkout page in a second tab flipped the invoice to "Paid · auto-matched" live, with a PAID stamp and one "INV-0415 paid" toast. |
| Per-payer ledger dashboard | ✅ Searching "Ram" opens his profile: Regular badge, ₹100 daily around 8:30 AM, ₹12,100 across 121 payments, every payment labelled. |
| Recurring + auto-reminder | ✅ Bhavesh Jain was marked weekly (₹2,085, 3 days' grace, email): INV-0416 was emailed straight away. After +7 days on the demo clock, INV-0416 went overdue and got one reminder, and INV-0417 was emailed. Resend shows all 5 demo emails delivered. Resetting the clock reopened the dues. |

## What works

- **Ledger and dashboard:** 90 days of seeded payments for "Gupta Kirana Store" (40 customers, about 8,000 payments). The dashboard shows takings for today, the week and the month, a 7/30/90-day trend, peak hours, slow-day and lost-regular alerts, pending payments and recent payments. Customer search works by name or phone number. Each customer page shows totals, payment rhythm, badges such as Regular, all their payments, and edit and merge.
- **Invoices:** create, email, remind and cancel. Each invoice gets a Paytm payment link and a QR image, and the email carries both. Paying an invoice flips it to "Paid · auto-matched" live, on every open screen, with no refresh. Webhooks are checksum-verified and idempotent, and a status poll catches payments whose webhook never arrived.
- **Recurring:** a customer can be marked weekly or monthly, with or without an email, and the schedule can be edited or stopped. Each due becomes an invoice 3 days ahead; a missed due becomes overdue after the grace days and gets exactly one reminder, which is retried if the send fails. Customers with a steady pattern get a suggestion to go recurring.
- **Background jobs:** run every 5 minutes, in-process or from the cron container. A demo clock in the sidebar (mock mode only) jumps +1 or +7 days, runs the jobs at the new time, and resets to where the seed put the clock.
- **Ask Hisaab (text and voice):**
  - Questions in Hindi, English or Hinglish, typed or spoken by holding the mic (or the Space bar). Answers come with charts, tables or customer cards, and voice answers are read aloud in the question's language.
  - Actions such as "send Ram an invoice for ₹500" only run after a Confirm tap.
  - **Number guard:** every number in an answer must come from the data the agent looked up; a number in the question counts only as a time ("4 mahine", "30 din"). Otherwise the agent retries on Sonnet, then shows the data without speech.
  - **Names:** a lookalike name ("Ramesh" when only Ram exists) gets "which customer?" choices, never someone else's money.
- **Reminder calls:** a Call button on unpaid invoices (and in Collections, or "Sharma ko call karo" in Ask Hisaab) has a Sarvam voice agent ring the customer over Twilio. If they want to pay now, the agent emails the Paytm link and QR during the call. The invoice shows the outcome, the agent's summary, whether the link went out and the transcript, and the dashboard lists "Promised on calls". Tested with a real call on 31 Oct. Setup: `docs/CALLS.md`.
- **AI descriptions:** each new payment gets a label of up to 8 words and a category. Edits the merchant makes are never overwritten.
- **Tests:** 859 API tests (pytest) and 135+ browser tests (Playwright on Chrome), many written by independent tester agents. M4 and M5 went through 6 tester rounds, M6 through 4, each fixed until the last found no blockers. Screenshots are in `evidence/m1`–`m7`.

## What's mocked or redirected

- **Paytm:** `PAYTM_MODE=mock`. Payment links, QR codes, checkout and webhooks come from a local mock that follows Paytm's documented request, response and checksum formats. The real client (`api/app/services/paytm/real.py`) is written but has never talked to Paytm.
- **Email:** every email goes to `DEMO_CUSTOMER_EMAIL` (your inbox), whoever the customer is, and is sent from `onboarding@resend.dev`. The intended recipient appears in the email. Tests use `EMAIL_MODE=log`, which sends nothing.
- **Data:** customers and payments are generated by `api/app/seed.py`. Re-seeding wipes and rebuilds the demo merchant. Before 10:00 IST it pins the demo clock to 12:30, so the day reads like a business day; later in the day it uses real time.
- **Time:** the demo clock overrides "now" for the whole app, for every merchant. It only exists in mock mode.

## What's left

1. **Hosted Supabase:** migrations are not pushed yet (steps below).
2. **Paytm keys:**
   - Run the staging contract tests (`pytest -m staging`) and confirm the response field names. The real client logs its first responses in full for this.
   - Register the Payment Status webhook in the Paytm dashboard.
   - Counter payments (soundbox or static QR) are not pulled into the ledger yet; only invoice and link payments are.
   - Cancelling an invoice doesn't expire its Paytm link yet (Paytm has an expire-link API). Until then, money paid on a cancelled invoice's link is still picked up by the 5-minute check and recorded.
3. **Email domain:** buy a domain, verify it in Resend, and stop redirecting emails to your inbox.
4. **Deployment:** Dockerfiles and Railway configs exist (`api/`, `deploy/cron/`), and the web app is ready for Vercel. Nothing has been deployed.
5. **Product gaps:**
   - Only one merchant per login.
   - Alerts show on screen only (no push or WhatsApp).
   - Voice answers appear 5–6 seconds after you let go of the mic, and the spoken reply is ready after 7–9 seconds; occasionally longer.
   - No rate limiting or production monitoring.
6. **Light "ledger paper" restyle:** another session started it on 31 Oct. It is in the working copy and not committed, and it hasn't been through a test round yet; the demo screenshots in `evidence/m7` show it.

## Setup guide (short)

### Run it locally

```bash
make db && make env && make migrate && make seed   # local Supabase + demo data
make api                                           # terminal 1, :8000
make web                                           # terminal 2, :3000
```

Open http://localhost:3000 and sign in with `DEMO_MERCHANT_EMAIL` / `DEMO_MERCHANT_PASSWORD` from `.env.example`.

### Move to hosted Supabase

1. `DATABASE_URL` in `.env` is already the Session pooler URL (port 5432, `sslmode=require`).
2. Push the schema: `supabase db push --db-url "$DATABASE_URL"`.
3. In the Supabase dashboard, under Project Settings → JWT Keys, make sure the current signing key is asymmetric (ECC or RSA).
4. Rename `.env.local` to `.env.local.off`, since it points everything at the local stack. Then add `CRON_SECRET=<any random string>` and `RUN_SCHEDULER=true` to `.env`.
5. Point `web/.env.local` at the hosted project: set `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` to the same values as in `.env`.
6. Run `make seed`, then `make api` and `make web`.

### Add Paytm keys

1. In `.env`, set `PAYTM_MODE=staging`, `PAYTM_MID=…`, `PAYTM_MERCHANT_KEY=…` and `PAYTM_WEBSITE=WEBSTAGING`.
2. Paytm must be able to reach the API: run a tunnel such as `cloudflared tunnel --url http://localhost:8000` and set `PUBLIC_API_BASE` to the https URL it prints.
3. In the Paytm dashboard, set the Payment Status webhook to `<PUBLIC_API_BASE>/webhooks/paytm/payment`. Payment links register `/webhooks/paytm/link` on their own.
4. Run `cd api && uv run pytest -m staging` to check the contract, then create and pay a ₹1 invoice. The demo clock is hidden outside mock mode.

### Reminder calls

`CALL_MODE=mock` (the default) fakes the whole call, so nothing is needed for UI work. For real calls (Sarvam agent, Twilio verified numbers, ngrok for local testing) follow `docs/CALLS.md`.

### Email

- Today, Resend sends from `onboarding@resend.dev` to `DEMO_CUSTOMER_EMAIL` only.
- With a domain: verify it in Resend, set `RESEND_FROM=billing@yourdomain`, and leave `DEMO_CUSTOMER_EMAIL` empty so emails reach real customers.
