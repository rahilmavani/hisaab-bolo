import os

# Tests never send real email and always use the mock gateway.
os.environ["EMAIL_MODE"] = "log"
os.environ["PAYTM_MODE"] = "mock"
os.environ["CALL_MODE"] = "log"  # reminder calls are recorded, never placed
os.environ["DEMO_CUSTOMER_PHONE"] = ""
os.environ["DEMO_CALL_PHONES"] = ""  # the seeded story keeps its made-up numbers
os.environ["RUN_SCHEDULER"] = "false"
os.environ["AGENT_WARMUP"] = "false"
os.environ["MERCHANT_UPI_ID"] = ""  # tests don't depend on the developer's own UPI ID; UPI tests set one

import httpx  # noqa: E402
import psycopg  # noqa: E402
import pytest  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.seed import seed  # noqa: E402


@pytest.fixture(autouse=True)
def no_real_ai_descriptions(monkeypatch):
    """Paid webhooks and ticks describe payments with Claude; tests never call the real model
    (tests about descriptions install their own fake on top of this)."""
    async def none(_):
        return None
    monkeypatch.setattr("app.services.descriptions.generate", none)


@pytest.fixture(scope="session")
def settings():
    return get_settings()


@pytest.fixture(scope="session")
def seeded(settings):
    """Re-seed the demo merchant once per test session so tests see a known state."""
    return seed(verbose=False)


@pytest.fixture(scope="session")
def reseed_demo(seeded):
    """Re-seed the demo merchant and update the shared `seeded` record in place (new merchant id)."""
    def run():
        seeded.update(seed(verbose=False))
    return run


@pytest.fixture
def db(settings):
    with psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row) as conn:
        yield conn


@pytest.fixture
def restore_clock(settings):
    """Put the demo clock back where it was (the seed may have pinned it)."""
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        before = conn.execute("select override_now, set_at from app_clock where id = 1").fetchone()
    yield
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute("update app_clock set override_now = %s, set_at = %s where id = 1", before)


def sign_in(settings, email: str, password: str) -> str:
    r = httpx.post(
        f"{settings.supabase_url}/auth/v1/token?grant_type=password",
        headers={"apikey": settings.supabase_publishable_key},
        json={"email": email, "password": password},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["access_token"]


@pytest.fixture(scope="session")
def demo_token(settings, seeded):
    return sign_in(settings, settings.demo_merchant_email, settings.demo_merchant_password)


# ---------------------------------------------------------------------------
# API fixtures
# ---------------------------------------------------------------------------
import uuid as _uuid  # noqa: E402

import pytest_asyncio  # noqa: E402


@pytest_asyncio.fixture(scope="session")
async def api():
    from app.main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


def _admin_headers(settings):
    return {"apikey": settings.supabase_secret_key, "Authorization": f"Bearer {settings.supabase_secret_key}"}


def create_user(settings, email: str, password: str) -> str:
    r = httpx.post(
        f"{settings.supabase_url}/auth/v1/admin/users", headers=_admin_headers(settings), timeout=20,
        json={"email": email, "password": password, "email_confirm": True},
    )
    r.raise_for_status()
    return r.json()["id"]


def delete_user(settings, user_id: str) -> None:
    httpx.delete(f"{settings.supabase_url}/auth/v1/admin/users/{user_id}", headers=_admin_headers(settings), timeout=20)


@pytest.fixture
def other_merchant(settings):
    """A throwaway merchant with its own login, two payers and a few payments."""
    email = f"test-{_uuid.uuid4().hex[:10]}@hisaabbolo.test"
    password = "test-pass-" + _uuid.uuid4().hex[:8]
    user_id = create_user(settings, email, password)
    with psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row) as conn:
        m = conn.execute(
            "insert into merchants (name, owner_user_id) values ('Test Store', %s) returning id", (user_id,)
        ).fetchone()["id"]
        a = conn.execute(
            "insert into payers (merchant_id, display_name, email) values (%s, 'Asha Test', 'asha@example.com') returning id",
            (m,),
        ).fetchone()["id"]
        b = conn.execute(
            "insert into payers (merchant_id, display_name, phone, notes) values (%s, 'Asha T (UPI)', '9000000001', 'from upi') returning id",
            (m,),
        ).fetchone()["id"]
        conn.execute(
            "insert into payer_identifiers (payer_id, merchant_id, kind, value) values "
            "(%s, %s, 'vpa', 'asha@okaxis'), (%s, %s, 'vpa', 'asha.t@ybl'), (%s, %s, 'phone', '9000000001')",
            (a, m, b, m, b, m),
        )
        txns = []
        for i in range(3):
            txns.append(conn.execute(
                "insert into transactions (merchant_id, payer_id, amount_paise, paid_at, source, description) "
                "values (%s, %s, %s, app_now() - %s * interval '1 day', 'seed', 'Groceries') returning id",
                (m, a if i < 2 else b, 250_00 + i * 100, i + 1),
            ).fetchone()["id"])
        try:
            yield {"merchant_id": m, "user_id": user_id, "token": sign_in(settings, email, password),
                   "payer_a": a, "payer_b": b, "txns": txns}
        finally:
            conn.execute("delete from merchants where id = %s", (m,))
            delete_user(settings, user_id)


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}
