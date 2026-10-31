"""Tester M1: RLS through the Supabase REST API (PostgREST behind the gateway).

"every table has merchant_id. RLS is on." The browser only reads its own merchant's
rows; every write goes through the API's service connection.
"""

from __future__ import annotations

import uuid

import httpx
import psycopg
import pytest

from app.config import ROOT

READABLE = ["merchants", "payers", "payer_identifiers", "transactions", "invoices", "recurring_schedules",
            "reminders", "agent_queries", "daily_totals", "hourly_heatmap", "payer_stats"]
HIDDEN = ["webhook_events", "pending_actions", "paytm_mock_orders"]


def _env_example() -> dict[str, str]:
    out = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _headers(settings, token: str | None = None, **extra) -> dict:
    h = {"apikey": settings.supabase_publishable_key, **extra}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _get(settings, path: str, token: str | None = None, **extra) -> httpx.Response:
    return httpx.get(f"{settings.supabase_url}/rest/v1/{path}", headers=_headers(settings, token, **extra), timeout=20)


def _count(settings, table: str, token: str) -> int:
    r = _get(settings, f"{table}?select=*&limit=1", token, Prefer="count=exact")
    assert r.status_code in (200, 206), (table, r.status_code, r.text[:200])
    return int(r.headers["content-range"].split("/")[1])


def _admin(settings) -> dict:
    return {"apikey": settings.supabase_secret_key, "Authorization": f"Bearer {settings.supabase_secret_key}"}


def _sign_in(settings, email, password) -> str:
    r = httpx.post(f"{settings.supabase_url}/auth/v1/token?grant_type=password",
                   headers={"apikey": settings.supabase_publishable_key},
                   json={"email": email, "password": password}, timeout=20)
    r.raise_for_status()
    return r.json()["access_token"]


@pytest.fixture
def stranger(settings, seeded):
    """A brand-new signed-up user with no merchant at all."""
    email = f"stranger-{uuid.uuid4().hex[:10]}@hisaabbolo.test"
    password = "pw-" + uuid.uuid4().hex
    r = httpx.post(f"{settings.supabase_url}/auth/v1/admin/users", headers=_admin(settings), timeout=20,
                   json={"email": email, "password": password, "email_confirm": True})
    r.raise_for_status()
    uid = r.json()["id"]
    try:
        yield _sign_in(settings, email, password)
    finally:
        httpx.delete(f"{settings.supabase_url}/auth/v1/admin/users/{uid}", headers=_admin(settings), timeout=20)


@pytest.fixture
def demo_mid(settings, seeded):
    with psycopg.connect(settings.database_url) as c:
        return str(c.execute(
            "select m.id from merchants m join auth.users u on u.id = m.owner_user_id where u.email = %s",
            (settings.demo_merchant_email,)).fetchone()[0])


# ---------------------------------------------------------------------------
# Demo login
# ---------------------------------------------------------------------------
def test_demo_login_with_documented_credentials(settings, seeded):
    env = _env_example()
    token = _sign_in(settings, env["DEMO_MERCHANT_EMAIL"], env["DEMO_MERCHANT_PASSWORD"])
    assert token.count(".") == 2


def test_wrong_password_is_rejected(settings, seeded):
    r = httpx.post(f"{settings.supabase_url}/auth/v1/token?grant_type=password",
                   headers={"apikey": settings.supabase_publishable_key},
                   json={"email": settings.demo_merchant_email, "password": "wrong-password"}, timeout=20)
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Owner reads
# ---------------------------------------------------------------------------
def test_owner_reads_full_own_ledger(settings, demo_token, demo_mid):
    m = _get(settings, "merchants?select=id,name", demo_token).json()
    assert m == [{"id": demo_mid, "name": "Gupta Kirana Store"}]
    with psycopg.connect(settings.database_url) as c:
        db_counts = {t: c.execute(f"select count(*) from {t} where merchant_id = %s", (demo_mid,)).fetchone()[0]
                     for t in ("payers", "transactions", "invoices", "payer_identifiers", "reminders")}
    for t, n in db_counts.items():
        assert _count(settings, t, demo_token) == n, t
    for t in ("payers", "transactions", "invoices"):
        mids = {r["merchant_id"] for r in _get(settings, f"{t}?select=merchant_id", demo_token).json()}
        assert mids == {demo_mid}, t


def test_owner_reads_views_through_rls(settings, demo_token, demo_mid):
    ram = _get(settings, "payers?select=id&display_name=eq.Ram", demo_token).json()
    assert len(ram) == 1
    stats = _get(settings, f"payer_stats?payer_id=eq.{ram[0]['id']}", demo_token).json()
    assert stats and stats[0]["txn_count"] == 121 and stats[0]["median_amount_paise"] == 100_00
    daily = _get(settings, "daily_totals?select=merchant_id,day&order=day.desc&limit=5", demo_token).json()
    assert daily and {d["merchant_id"] for d in daily} == {demo_mid}
    heat = _get(settings, "hourly_heatmap?select=merchant_id,hour", demo_token).json()
    assert heat and {h["merchant_id"] for h in heat} == {demo_mid}
    clock = _get(settings, "app_clock?select=id,override_now", demo_token).json()
    assert clock == [{"id": 1, "override_now": None}]


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("table", READABLE)
def test_stranger_sees_no_rows(settings, stranger, table):
    r = _get(settings, f"{table}?select=*", stranger)
    assert r.status_code == 200, (r.status_code, r.text[:200])
    assert r.json() == [], f"{table} leaks {len(r.json())} rows to an unrelated user"


def test_stranger_cannot_reach_demo_rows_by_id_filter(settings, stranger, demo_mid):
    for t in ("payers", "transactions", "invoices", "payer_stats", "daily_totals"):
        r = _get(settings, f"{t}?merchant_id=eq.{demo_mid}", stranger)
        assert r.status_code == 200 and r.json() == [], t
    r = httpx.post(f"{settings.supabase_url}/rest/v1/rpc/my_merchant_ids",
                   headers=_headers(settings, stranger), json={}, timeout=20)
    assert r.status_code == 200 and r.json() == []


def test_two_merchants_are_isolated_both_ways(settings, demo_token, demo_mid, other_merchant):
    om, tok = str(other_merchant["merchant_id"]), other_merchant["token"]
    payers = _get(settings, "payers?select=id,merchant_id", tok).json()
    assert len(payers) == 2 and {p["merchant_id"] for p in payers} == {om}
    txns = _get(settings, "transactions?select=id,merchant_id", tok).json()
    assert len(txns) == 3 and {t["merchant_id"] for t in txns} == {om}
    stats = _get(settings, "payer_stats?select=merchant_id", tok).json()
    assert stats and {s["merchant_id"] for s in stats} == {om}
    assert _get(settings, f"payers?merchant_id=eq.{demo_mid}", tok).json() == []
    assert _get(settings, f"payers?merchant_id=eq.{om}", demo_token).json() == []
    assert _get(settings, f"daily_totals?merchant_id=eq.{om}", demo_token).json() == []


@pytest.mark.parametrize("table", READABLE + HIDDEN)
def test_anonymous_reads_nothing(settings, seeded, table):
    r = _get(settings, f"{table}?select=*")
    assert r.status_code in (401, 403) or r.json() == [], (table, r.status_code)


@pytest.mark.parametrize("table", HIDDEN)
def test_server_only_tables_hidden_from_owner(settings, demo_token, table):
    r = _get(settings, f"{table}?select=*", demo_token)
    assert r.status_code in (401, 403) or r.json() == [], (table, r.status_code)


# ---------------------------------------------------------------------------
# Clients cannot write
# ---------------------------------------------------------------------------
def test_owner_cannot_write_through_rest(settings, demo_token, demo_mid):
    base = f"{settings.supabase_url}/rest/v1"
    h = _headers(settings, demo_token, Prefer="return=representation")
    r = httpx.post(f"{base}/payers", headers=h, json={"merchant_id": demo_mid, "display_name": "Injected"}, timeout=20)
    assert r.status_code in (401, 403), r.status_code
    r = httpx.patch(f"{base}/invoices?number=eq.INV-0412", headers=h, json={"status": "paid"}, timeout=20)
    assert r.status_code in (401, 403) or r.json() == [], r.status_code
    with psycopg.connect(settings.database_url) as c:
        tid = c.execute("select id from transactions where merchant_id = %s limit 1", (demo_mid,)).fetchone()[0]
    r = httpx.delete(f"{base}/transactions?id=eq.{tid}", headers=h, timeout=20)
    assert r.status_code in (401, 403) or r.json() == [], r.status_code
    with psycopg.connect(settings.database_url) as c:
        assert c.execute("select count(*) from transactions where id = %s", (tid,)).fetchone()[0] == 1
        assert c.execute("select status from invoices where merchant_id = %s and number = 'INV-0412'",
                         (demo_mid,)).fetchone()[0] == "overdue"
        assert c.execute("select count(*) from payers where display_name = 'Injected'").fetchone()[0] == 0


def test_owner_cannot_jump_the_demo_clock(settings, demo_token):
    h = _headers(settings, demo_token, Prefer="return=representation")
    r = httpx.patch(f"{settings.supabase_url}/rest/v1/app_clock?id=eq.1", headers=h,
                    json={"override_now": "2030-01-01T00:00:00Z", "set_at": "2026-01-01T00:00:00Z"}, timeout=20)
    assert r.status_code in (401, 403) or r.json() == [], r.status_code
    with psycopg.connect(settings.database_url) as c:
        assert c.execute("select override_now from app_clock where id = 1").fetchone()[0] is None


def test_clients_cannot_mint_invoice_numbers(settings, demo_token, demo_mid):
    with psycopg.connect(settings.database_url) as c:
        before = c.execute("select invoice_seq from merchants where id = %s", (demo_mid,)).fetchone()[0]
    r = httpx.post(f"{settings.supabase_url}/rest/v1/rpc/next_invoice_number",
                   headers=_headers(settings, demo_token), json={"p_merchant": demo_mid}, timeout=20)
    assert r.status_code in (401, 403, 404), r.status_code
    with psycopg.connect(settings.database_url) as c:
        assert c.execute("select invoice_seq from merchants where id = %s", (demo_mid,)).fetchone()[0] == before


# ---------------------------------------------------------------------------
# Storage: "The QR PNG goes to Supabase Storage" and the email loads it by URL
# ---------------------------------------------------------------------------
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002"
    "000154a24f5d0000000049454e44ae426082"
)


def test_qr_bucket_serves_public_urls_and_rejects_client_uploads(settings, demo_token):
    base = f"{settings.supabase_url}/storage/v1"
    name = f"tester-probe-{uuid.uuid4().hex[:8]}.png"
    admin = {**_admin(settings), "Content-Type": "image/png"}
    up = httpx.post(f"{base}/object/qr/{name}", headers=admin, content=PNG_1PX, timeout=20)
    assert up.status_code == 200, up.text[:200]
    try:
        pub = httpx.get(f"{base}/object/public/qr/{name}", timeout=20)  # no apikey, like an email client
        assert pub.status_code == 200 and pub.content == PNG_1PX
        for token in (None, demo_token):
            h = {"apikey": settings.supabase_publishable_key, "Content-Type": "image/png"}
            if token:
                h["Authorization"] = f"Bearer {token}"
            r = httpx.post(f"{base}/object/qr/client-{name}", headers=h, content=PNG_1PX, timeout=20)
            assert r.status_code >= 400, f"client upload to qr bucket allowed (token={'demo' if token else 'anon'})"
    finally:
        httpx.request("DELETE", f"{base}/object/qr", headers=_admin(settings),
                      json={"prefixes": [name, f"client-{name}"]}, timeout=20)
