"""Payer writes: edits and merges. One ledger per person; merging moves identifiers."""

import re
from uuid import UUID

from psycopg import AsyncConnection

from ..db import fetch_one
from ..phones import india_digits

EDITABLE = ("display_name", "email", "phone", "gstin", "notes")
IDENTIFIER_FIELDS = {"email": "email", "phone": "phone"}


class PayerError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


async def add_identifier(conn: AsyncConnection, merchant_id: UUID, payer_id: UUID, kind: str, value: str | None) -> bool:
    """Attach an identifier if no other payer owns it. Returns True when inserted."""
    value = (value or "").strip()
    if not value:
        return False
    if kind == "phone":
        try:
            value = normalise_phone(value) or ""
        except PayerError:
            return False
    cur = await conn.execute(
        "insert into payer_identifiers (payer_id, merchant_id, kind, value) values (%s, %s, %s, %s) "
        "on conflict (merchant_id, kind, value) do nothing",
        (payer_id, merchant_id, kind, value.lower() if kind == "email" else value),
    )
    return cur.rowcount == 1


def normalise_phone(value: str | None) -> str | None:
    """Store phones as digits without +91/0091/trunk zero, so one number maps to one person."""
    if value is None:
        return None
    digits = india_digits(value)
    if not 7 <= len(digits) <= 15:
        raise PayerError(422, "Phone should have 7 to 15 digits")
    return digits


async def update_payer(conn: AsyncConnection, merchant_id: UUID, payer_id: UUID, changes: dict) -> None:
    fields = {k: v for k, v in changes.items() if k in EDITABLE}
    if "display_name" in fields:
        fields["display_name"] = (fields["display_name"] or "").strip()
        if not fields["display_name"]:
            raise PayerError(422, "display_name cannot be empty")
    if "notes" in fields:
        fields["notes"] = (fields["notes"] or "").strip()
    if "email" in fields:
        fields["email"] = (fields["email"] or "").strip().lower() or None
    if "phone" in fields:
        # null or "" clears the phone; anything else must be a real number (whitespace alone is rejected).
        fields["phone"] = None if fields["phone"] in (None, "") else normalise_phone(fields["phone"])
    if "gstin" in fields:
        fields["gstin"] = (fields["gstin"] or "").strip().upper() or None
    async with conn.transaction():
        row = await fetch_one(
            conn, "select * from payers where id = %s and merchant_id = %s for update", (payer_id, merchant_id),
        )
        if row is None:
            raise PayerError(404, "payer not found")
        if row["merged_into"]:
            raise PayerError(409, "payer was merged into another payer")
        # One identifier belongs to one person: refuse to show another customer's phone/email here.
        for field, kind in IDENTIFIER_FIELDS.items():
            value = fields.get(field)
            if value:
                owner = await fetch_one(
                    conn,
                    "select p.display_name from payer_identifiers i join payers p on p.id = i.payer_id "
                    "where i.merchant_id = %s and i.kind = %s and i.value = %s and i.payer_id <> %s",
                    (merchant_id, kind, value, payer_id),
                )
                if owner:
                    raise PayerError(409, f"{value} already belongs to {owner['display_name']}. "
                                          "If they are the same person, merge the two customers instead.")
        if fields:
            sets = ", ".join(f"{k} = %({k})s" for k in fields)
            await conn.execute(f"update payers set {sets} where id = %(id)s", {**fields, "id": payer_id})
        for field, kind in IDENTIFIER_FIELDS.items():
            if field not in fields or fields[field] == row[field]:
                continue
            if row[field]:  # the edited contact replaces the old one
                await conn.execute(
                    "delete from payer_identifiers where payer_id = %s and kind = %s and value = %s",
                    (payer_id, kind, row[field].strip().lower() if kind == "email" else row[field]),
                )
            if fields[field]:
                await add_identifier(conn, merchant_id, payer_id, kind, fields[field])


async def merge_payers(conn: AsyncConnection, merchant_id: UUID, keep_id: UUID, other_id: UUID) -> None:
    """Fold `other` into `keep`: identifiers, payments, invoices and schedules move over."""
    if keep_id == other_id:
        raise PayerError(422, "cannot merge a payer into itself")
    async with conn.transaction():
        rows = {
            r["id"]: r for r in await (await conn.execute(
                "select * from payers where merchant_id = %s and id = any(%s) for update",
                (merchant_id, [keep_id, other_id]),
            )).fetchall()
        }
        if keep_id not in rows or other_id not in rows:
            raise PayerError(404, "payer not found")
        if rows[keep_id]["merged_into"] or rows[other_id]["merged_into"]:
            raise PayerError(409, "payer was already merged")
        keep, other = rows[keep_id], rows[other_id]

        # Identifiers move; exact duplicates already on `keep` are dropped.
        await conn.execute(
            "delete from payer_identifiers o using payer_identifiers k "
            "where o.payer_id = %s and k.payer_id = %s and o.kind = k.kind and o.value = k.value",
            (other_id, keep_id),
        )
        await conn.execute("update payer_identifiers set payer_id = %s where payer_id = %s", (keep_id, other_id))
        await conn.execute("update transactions set payer_id = %s where payer_id = %s", (keep_id, other_id))
        await conn.execute("update invoices set payer_id = %s where payer_id = %s", (keep_id, other_id))
        keep_has_schedule = await fetch_one(
            conn, "select 1 from recurring_schedules where payer_id = %s and active", (keep_id,)
        )
        if keep_has_schedule:
            await conn.execute(
                "update recurring_schedules set active = false where payer_id = %s and active", (other_id,)
            )
        await conn.execute("update recurring_schedules set payer_id = %s where payer_id = %s", (keep_id, other_id))

        fill = {f: other[f] for f in ("email", "phone", "gstin") if not keep[f] and other[f]}
        notes = "\n".join(n for n in (keep["notes"], other["notes"]) if n)
        await conn.execute(
            "update payers set email = coalesce(%(email)s, email), phone = coalesce(%(phone)s, phone), "
            "gstin = coalesce(%(gstin)s, gstin), notes = %(notes)s where id = %(id)s",
            {"email": fill.get("email"), "phone": fill.get("phone"), "gstin": fill.get("gstin"),
             "notes": notes, "id": keep_id},
        )
        await conn.execute("update payers set merged_into = %s where id = %s", (keep_id, other_id))
        # Anything previously merged into `other` now points at `keep`.
        await conn.execute("update payers set merged_into = %s where merged_into = %s", (keep_id, other_id))
