# Reminder calls (Sarvam voice agent over Twilio)

The merchant presses **Call customer** on an unpaid invoice (or a row in Collections, or asks Ask Hisaab
"Sharma ko call karo" and confirms). A Sarvam voice agent rings the customer from the merchant's Twilio
number, reminds them about the invoice, and, if they want to pay now, emails them the Paytm link and QR
during the call. When the call ends the invoice page shows the outcome, the agent's summary, whether the
link went out, and the transcript; the Today dashboard lists promises under "Promised on calls".

Code: `api/app/services/calls.py`, `api/app/routers/sarvam.py`, migrations `…07_call_reminders.sql` and
`…08_call_link_tool_summary.sql`, UI in `web/src/components/call-status.tsx` and `promised-payments.tsx`.

## Modes

`CALL_MODE` in `.env`:

| Mode | What happens |
| --- | --- |
| `mock` (default) | No call. After ~4 s a fake customer "picks up", asks for the link (the link email goes out for real through the usual email path), and promises to pay today. Good for UI work and demos without keys. |
| `log` | The call is recorded as queued and nothing else happens. Tests use this. |
| `sarvam` | Real calls. Needs everything below. |

## How it flows

1. `POST /invoices/{id}/call` creates a `reminders` row (`channel = 'call'`, `call_status = 'queued'`) with a
   one-time `call_token`, then calls Sarvam's instant-outbound API with the invoice as agent variables.
2. During the call the agent may use its `send_payment_link` tool:
   `POST {PUBLIC_API_BASE}/webhooks/sarvam/tool/send-link?token=SECRET` with `{"call_token": "..."}`.
   It only works for that call's invoice, only emails the customer's own email on file, and only once per
   call. It always answers 200 with a `message` for the agent to say.
3. When the call ends Sarvam posts the result to `{PUBLIC_API_BASE}/webhooks/sarvam/call?token=SECRET`:
   status, duration, transcript and the agent's outputs (`outcome`, `promised_date`, `summary`). If the
   customer picked up and the link wasn't sent mid-call, the link email goes out then.

Sarvam doesn't sign its requests, so both URLs carry `SARVAM_WEBHOOK_SECRET` (falls back to `CRON_SECRET`).

## Set up real calls

### 1. Twilio

- A voice-capable Twilio number.
- **Trial accounts only call verified numbers.** Twilio console → Phone Numbers → Manage →
  **Verified Caller IDs** → add every phone you will call (yours, your teammates'). Verify it on the
  same Twilio account whose SID is connected in Sarvam; otherwise calls fail with Twilio error 21219.
- Calling Indian numbers from a US number: Voice → Settings → **Geo Permissions** → tick India.

### 2. Sarvam (agents dashboard)

1. **Connect Twilio:** Deploy → Phone Numbers → Add connection → Twilio (Account SID + Auth Token), then
   import the number. Open the connection to copy its **Connection ID** (`Twilio-…`).
2. **Create the agent** and give it these variables (names must match exactly):
   - Inputs: `customer_name`, `merchant_name`, `invoice_number`, `amount`, `due_date`, `days_overdue`,
     `reminder_kind` (`overdue`/`upcoming`), `email_on_file` (`yes`/`no`), `email_hint` (masked email),
     `call_token` (internal; never spoken).
   - Outputs: `outcome` (Enum: `will_pay`, `already_paid`, `dispute`, `call_later`, `no_commitment`),
     `promised_date` (String, `YYYY-MM-DD`), `summary` (String, 2–3 sentences for the shop owner).
3. **Add the tool** `send_payment_link`:
   - Method `POST`, URL `{PUBLIC_API_BASE}/webhooks/sarvam/tool/send-link?token={SARVAM_WEBHOOK_SECRET}`
   - Header `Content-Type: application/json`, body `{"call_token": "@call_token"}`
   - Use it when the customer wants to pay now or asks for the link (only if `@email_on_file` is `yes`),
     at most once per call, then say the returned `message`. Never read out a full email; `@email_hint` is fine.
4. **Commit** the agent and note the version number. No deployment is needed: deployments are for inbound
   calls; outbound calls go straight through the API.
5. **API key:** Settings → API Key (the Voice Agents key, `sk_samvaad_…`; not the speech-to-text key).
   The **org ID** and **workspace ID** are in the dashboard URL. The **agent ID** is shown on the agent.

### 3. `.env`

```
CALL_MODE=sarvam
SARVAM_AGENTS_API_KEY=sk_samvaad_...
SARVAM_ORG_ID=...
SARVAM_WORKSPACE_ID=...
SARVAM_APP_ID=...                # agent ID
SARVAM_APP_VERSION=3             # the committed version
SARVAM_CONNECTION_ID=Twilio-...
SARVAM_AGENT_PHONE=+1...         # the Twilio number, E.164
SARVAM_WEBHOOK_SECRET=...        # openssl rand -hex 16; the same value goes in the tool URL
PUBLIC_API_BASE=https://<your-ngrok-or-deployed-host>
# Optional
DEMO_CALL_PHONES=Sharma Traders=+91XXXXXXXXXX   # seeded customers that ring real phones (then `make seed`)
DEMO_CUSTOMER_PHONE=             # if set, every call goes to this one number instead
SARVAM_CALL_LANGUAGE=            # e.g. Hindi, to force the opening language
```

Restart the API after changing `.env` (settings are read once at startup).

### 4. A public URL for the API (local testing)

Sarvam has to reach the two endpoints above, and it can't reach localhost:

```bash
ngrok http 8000          # copy the https://… address
```

Put that address in `PUBLIC_API_BASE` **and** in the Sarvam tool URL, then restart the API. Free ngrok
addresses change on every restart (update both places each time), unless you claim the free static domain
in the ngrok dashboard and run `ngrok http --url=<your-domain> 8000`. Deployed, use the API's HTTPS URL.

Check it: `curl https://<address>/health` should show `"call_mode": "sarvam"`.

### 5. Test call

Open an unpaid invoice of a customer whose phone is verified in Twilio and press **Call customer**. Say
"haan, link bhej do" on the call. The invoice page should then show the answered call, "Payment link
emailed during the call", the summary and the transcript; the email lands in `DEMO_CUSTOMER_EMAIL`.

## Automatic calls (opt-in)

Invoices and recurring schedules have a "Call them if it stays unpaid" checkbox (off by default). When on,
the 5-minute job calls the customer 2 days after the overdue email (`CALL_AFTER_OVERDUE_DAYS`), only
between 9 AM and 8 PM IST, with one retry 3 hours later if nobody picked up.

## Troubleshooting

| Symptom on the invoice | Cause |
| --- | --- |
| `twilio: … 21219 … unverified` | The number isn't in Verified Caller IDs on the Twilio account Sarvam uses. |
| `Calling isn't set up yet: add SARVAM_…` | A `.env` value is missing, or the API wasn't restarted. |
| Stuck on "Calling…" | Sarvam can't reach `PUBLIC_API_BASE` (ngrok closed or address changed). A queued call blocks a new one for 10 minutes. |
| Agent says it can't send the link | The tool URL or its `?token=` doesn't match `PUBLIC_API_BASE` / `SARVAM_WEBHOOK_SECRET`, or the customer has no email. |
| Answered, but no "Promised" date | The agent didn't fill `promised_date`; tighten that instruction in the agent. |
