# Hisaab Bolo

Merchant growth AI on top of Paytm payments (Paytm Build for India, team PiedPiper).
Spec: `docs/ARCHITECTURE.md`, Paytm details: `docs/paytm/PAYTM_INTEGRATION.md`, plan: `docs/PLAN.md`,
status: `docs/STATUS.md`, reminder calls: `docs/CALLS.md`.

```
web/       Next.js 16 (App Router, Tailwind v4): dashboard, customers, collections, Ask Hisaab
api/       FastAPI (Python 3.12, uv): ledger, invoices, Paytm (mock/real), webhooks, jobs, agent
supabase/  migrations for Postgres + RLS + realtime + storage
```

## Run it locally

Needs Docker, the Supabase CLI, `uv`, Node 22+ and Google Chrome (Playwright uses it).

```bash
# first time only: the local stack signs logins with an ES256 key that isn't in git
echo '[]' > supabase/signing_keys.json && supabase gen signing-key --algorithm ES256 --append
make db        # local Supabase on ports 554xx
make env       # writes .env.local + web/.env.local from the local stack
make migrate
make seed      # Gupta Kirana Store: 40 payers, ~8k payments over 90 days
make api       # :8000 (separate terminal)
make web       # :3000 (separate terminal)
```

Real third-party keys (Anthropic, Sarvam, Resend) live in `.env` (see `.env.example`).
Reminder calls run in `CALL_MODE=mock` until you set up Sarvam and Twilio (`docs/CALLS.md`).
Sign in with the demo merchant from `.env.example` (`DEMO_MERCHANT_EMAIL` / `DEMO_MERCHANT_PASSWORD`).

## Tests

```bash
make test-api  # pytest; re-seeds the demo merchant
make test-e2e  # Playwright; screenshots land in evidence/
```
