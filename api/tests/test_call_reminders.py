"""Reminder calls: manual call, Sarvam outcome webhook, automatic call after the overdue email."""

from datetime import date, timedelta

import pytest

from app.services import calls
from app.services import email as email_svc

from .conftest import bearer


@pytest.fixture(scope="module", autouse=True)
def reseed_after_module(reseed_demo):
    """Ticks run for every merchant (the demo one too); restore the seeded story afterwards."""
    yield
    reseed_demo()


@pytest.fixture(autouse=True)
def daytime(settings, monkeypatch):
    """Automatic calls wait for daytime IST; tests run at any hour."""
    monkeypatch.setattr(settings, "call_window_ist", (0, 24))


def hook(settings, token=None):
    return f"/webhooks/sarvam/call?token={settings.call_webhook_secret if token is None else token}"


async def invoice_for(api, m, payer="payer_b", **overrides) -> dict:
    body = {
        "payer_id": str(m[payer]),
        "due_date": (date.today() + timedelta(days=5)).isoformat(),
        "amount_paise": 1234_50,
        "email": "asha.t@example.com",
    } | overrides
    r = await api.post("/invoices", json=body, headers=bearer(m["token"]))
    assert r.status_code == 201, r.text
    return r.json()


async def jump(api, token, **delta):
    r = await api.post("/admin/clock", json=delta, headers=bearer(token))
    assert r.status_code == 200, r.text
    return r.json()


def test_phone_and_amount_formats():
    assert calls.e164("9876543210") == "+919876543210"
    assert calls.e164("+91 98765-43210") == "+919876543210"
    assert calls.e164("447700900123") == "+447700900123"
    assert calls.e164("12345") is None and calls.e164(None) is None
    assert calls.spoken_amount(8500_00) == "8,500 rupees"
    assert calls.spoken_amount(1_23_456_50) == "1,23,456 rupees 50 paise"


async def test_call_now_records_a_queued_call_with_invoice_variables(api, other_merchant):
    h = bearer(other_merchant["token"])
    inv = await invoice_for(api, other_merchant)
    calls.PLACED.clear()
    r = await api.post(f"/invoices/{inv['id']}/call", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["delivered_to"] == "+919000000001" and body["reminder"]["call_status"] == "queued"
    placed = calls.PLACED[-1]
    assert placed["variables"] | {"due_date": ""} == {
        "customer_name": "Asha ji", "merchant_name": "Test Store", "invoice_number": inv["number"],
        "amount": "1,234 rupees 50 paise", "due_date": "", "days_overdue": "0",
        "reminder_kind": "upcoming", "link_sent_by_email": "yes",
    }
    assert placed["metadata"] == {"invoice_id": inv["id"], "invoice_number": inv["number"]}

    again = await api.post(f"/invoices/{inv['id']}/call", headers=h)
    assert again.status_code == 409 and "already in progress" in again.json()["detail"]

    detail = (await api.get(f"/invoices/{inv['id']}", headers=h)).json()
    assert [(x["channel"], x["call_status"]) for x in detail["reminders"]][-1] == ("call", "queued")
    assert detail["last_call_status"] == "queued"


async def test_call_needs_a_phone_and_an_unpaid_invoice(api, other_merchant, db):
    h = bearer(other_merchant["token"])
    no_phone = await invoice_for(api, other_merchant, payer="payer_a", email="asha@example.com")
    r = await api.post(f"/invoices/{no_phone['id']}/call", headers=h)
    assert r.status_code == 422 and "no phone number" in r.json()["detail"]

    paid = await invoice_for(api, other_merchant)
    db.execute("update invoices set status = 'paid' where id = %s", (paid["id"],))
    assert (await api.post(f"/invoices/{paid['id']}/call", headers=h)).status_code == 409


async def test_webhook_records_outcome_and_emails_the_link(api, other_merchant, settings):
    h = bearer(other_merchant["token"])
    inv = await invoice_for(api, other_merchant)
    attempt = (await api.post(f"/invoices/{inv['id']}/call", headers=h)).json()["reminder"]["call_attempt_id"]
    payload = {
        "attempt_id": attempt, "status": "connected", "duration": 47, "interaction_id": "ix_1",
        "final_agent_variables": {"outcome": "will_pay", "promised_date": "2026-11-05", "customer_name": "Asha ji"},
        "interaction_transcript": [{"role": "agent", "en_text": "Namaste"}, {"role": "user", "en_text": "Kal dunga"}],
    }
    assert (await api.post(hook(settings, "wrong"), json=payload)).status_code == 401

    email_svc.SENT.clear()
    r = await api.post(hook(settings), json=payload)
    assert r.status_code == 200, r.text
    assert r.json() | {"ok": True} == {"ok": True, "result": "recorded", "status": "connected",
                                        "outcome": "will_pay", "link_emailed": True}
    assert email_svc.SENT and email_svc.SENT[-1]["intended_to"] == "asha.t@example.com"

    dup = await api.post(hook(settings), json=payload)
    assert dup.json()["result"] == "duplicate" and len(email_svc.SENT) == 1

    detail = (await api.get(f"/invoices/{inv['id']}", headers=h)).json()
    call = next(x for x in detail["reminders"] if x["channel"] == "call")
    assert call["call_status"] == "connected" and call["call_duration_s"] == 47
    assert call["call_outcome"] == "will_pay" and call["call_promised_date"] == "2026-11-05"
    assert call["call_transcript"][1]["en_text"] == "Kal dunga"
    assert detail["last_call_promised_date"] == "2026-11-05"

    unknown = await api.post(hook(settings), json={"attempt_id": "nope", "status": "connected"})
    assert unknown.status_code == 200 and unknown.json()["result"] == "unmatched"


async def test_unanswered_call_is_recorded_without_an_email(api, other_merchant, settings):
    h = bearer(other_merchant["token"])
    inv = await invoice_for(api, other_merchant)
    attempt = (await api.post(f"/invoices/{inv['id']}/call", headers=h)).json()["reminder"]["call_attempt_id"]
    email_svc.SENT.clear()
    r = await api.post(hook(settings), json={"attempt_id": attempt, "status": "no_answer", "duration": None,
                                             "failure_reason": "twilio: no answer"})
    assert r.json()["status"] == "no_answer" and "link_emailed" not in r.json()
    assert not email_svc.SENT
    # Nothing in flight any more: the merchant can try again straight away.
    assert (await api.post(f"/invoices/{inv['id']}/call", headers=h)).status_code == 200


async def test_recurring_call_remind_needs_a_phone(api, other_merchant):
    h = bearer(other_merchant["token"])
    r = await api.post(f"/payers/{other_merchant['payer_a']}/recurring",
                       json={"cadence": "monthly", "amount_paise": 500_00, "email": "asha@example.com",
                             "call_remind": True}, headers=h)
    assert r.status_code == 422 and "phone number" in r.json()["detail"]

    r = await api.post(f"/payers/{other_merchant['payer_a']}/recurring",
                       json={"cadence": "monthly", "amount_paise": 500_00, "email": "asha@example.com",
                             "call_remind": True, "phone": "+91 90000 00002"}, headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["schedule"]["call_remind"] is True
    assert r.json()["payer"]["phone"] == "9000000002"


async def test_missed_recurring_due_gets_a_call_after_the_email(api, other_merchant, settings, restore_clock):
    h = bearer(other_merchant["token"])
    r = await api.post(f"/payers/{other_merchant['payer_b']}/recurring",
                       json={"cadence": "monthly", "amount_paise": 700_00, "email": "asha.t@example.com",
                             "call_remind": True}, headers=h)
    assert r.status_code == 201, r.text
    number = r.json()["dues_created"][0]["invoice"]
    calls.PLACED.clear()

    out = await jump(api, other_merchant["token"], days=4)  # overdue today: the email goes first
    assert number in out["jobs"]["overdue"]["reminded"] and out["jobs"]["calls"]["called"] == []

    out = await jump(api, other_merchant["token"], days=2)  # still unpaid two days later: call
    assert out["jobs"]["calls"]["called"] == [number]
    assert calls.PLACED[-1]["variables"]["reminder_kind"] == "overdue"
    assert calls.PLACED[-1]["variables"]["days_overdue"] == "6"
    attempt = calls.PLACED[-1]["attempt_id"]

    out = await jump(api, other_merchant["token"], hours=1)  # no webhook yet: never a second call
    assert out["jobs"]["calls"]["called"] == []

    await api.post(hook(settings), json={"attempt_id": attempt, "status": "busy"})
    out = await jump(api, other_merchant["token"], hours=1)
    assert out["jobs"]["calls"]["called"] == []  # retries wait a few hours
    out = await jump(api, other_merchant["token"], hours=3)
    assert out["jobs"]["calls"]["called"] == [number]  # one retry...

    await api.post(hook(settings), json={"attempt_id": calls.PLACED[-1]["attempt_id"], "status": "no_answer"})
    out = await jump(api, other_merchant["token"], days=1)
    assert out["jobs"]["calls"]["called"] == []  # ...and no more


async def test_automatic_calls_wait_for_daytime(api, other_merchant, settings, monkeypatch, restore_clock):
    h = bearer(other_merchant["token"])
    inv = await invoice_for(api, other_merchant, due_date=date.today().isoformat(), call_remind=True)
    monkeypatch.setattr(settings, "call_window_ist", (0, 0))
    out = await jump(api, other_merchant["token"], days=3)
    assert out["jobs"]["calls"] == {"called": [], "retry_later": [], "waiting_for_daytime": [inv["number"]]}
    monkeypatch.setattr(settings, "call_window_ist", (0, 24))
    out = await jump(api, other_merchant["token"], hours=1)
    assert out["jobs"]["calls"]["called"] == [inv["number"]]


async def test_one_off_invoice_call_remind_needs_a_phone(api, other_merchant):
    r = await api.post("/invoices", headers=bearer(other_merchant["token"]), json={
        "payer_id": str(other_merchant["payer_a"]), "due_date": date.today().isoformat(),
        "amount_paise": 100_00, "call_remind": True,
    })
    assert r.status_code == 422 and "phone number" in r.json()["detail"]
