import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import get_settings
from .db import close_pool, open_pool
from .routers import agent, invoices, jobs, ledger, mock_pay, recurring, webhooks
from .services.agent.runner import warm_up
from .services.jobs import scheduler_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("hisaab")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await open_pool()
    tasks = [asyncio.create_task(warm_up())]
    if get_settings().run_scheduler:
        tasks.append(asyncio.create_task(scheduler_loop()))
    yield
    for t in tasks:
        t.cancel()
    await close_pool()


app = FastAPI(title="Hisaab Bolo API", version="0.1.0", lifespan=lifespan)


# Registered before CORS so CORS wraps it: unexpected errors still reach the browser as JSON.
@app.middleware("http")
async def catch_unhandled(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception:
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"detail": "Something went wrong on our side. Please try again."}, status_code=500)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in get_settings().web_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
for r in (ledger, invoices, recurring, webhooks, mock_pay, jobs, agent):
    app.include_router(r.router)


@app.get("/health")
async def health():
    s = get_settings()
    return {"ok": True, "paytm_mode": s.paytm_mode, "email_mode": s.email_mode}
