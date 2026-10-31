.PHONY: db env migrate seed api web tick test test-api test-e2e

db:            ## start local Supabase (Postgres, Auth, Storage, Realtime)
	supabase start -x vector,logflare,edge-runtime,imgproxy,studio

env:           ## write .env.local and web/.env.local from the running local stack
	./scripts/local-env.sh

migrate:       ## apply new migrations to the local database
	supabase migration up

seed:          ## wipe and recreate the demo merchant (also resets the demo clock)
	cd api && uv run python -m app.seed

api:           ## FastAPI on :8000
	cd api && uv run uvicorn app.main:app --port 8000 --reload --reload-dir app

web:           ## Next.js on :3000
	cd web && npm run dev -- --port 3000

tick:          ## run the background jobs once (same as the 5-minute cron)
	@set -a; . ./.env; [ -f .env.local ] && . ./.env.local; set +a; \
	curl -s -X POST localhost:8000/jobs/tick -H "X-Cron-Secret: $$CRON_SECRET"; echo

test-api:      ## pytest (re-seeds the demo merchant first)
	cd api && uv run pytest -q

test-e2e:      ## Playwright against the running web + API (uses system Chrome)
	cd web && npx playwright test

test: test-api test-e2e
