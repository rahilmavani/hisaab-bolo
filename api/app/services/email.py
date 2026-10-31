"""Customer emails through Resend.

Resend has no verified domain here, so it only delivers to the account inbox:
every customer email goes to DEMO_CUSTOMER_EMAIL, with the intended address
shown at the top. EMAIL_MODE=log prints instead of sending (tests).
"""

import html
import logging
import uuid
from dataclasses import dataclass
from datetime import date

import httpx

from ..config import get_settings
from ..money import format_inr
from .storage import is_public_url

log = logging.getLogger(__name__)
SENT: list[dict] = []  # log-mode outbox, inspected by tests


@dataclass
class EmailResult:
    id: str
    delivered_to: str
    intended_to: str


class EmailError(Exception):
    pass


async def send_email(intended_to: str, subject: str, html_body: str, attachments: list[dict] | None = None) -> EmailResult:
    s = get_settings()
    to = s.demo_customer_email or intended_to
    if s.email_mode == "log":
        msg = {"to": to, "intended_to": intended_to, "subject": subject, "html": html_body,
               "attachments": attachments or []}
        SENT.append(msg)
        log.info("email (log mode) to=%s intended=%s subject=%s", to, intended_to, subject)
        return EmailResult(id=f"log-{uuid.uuid4().hex[:12]}", delivered_to=to, intended_to=intended_to)
    if not s.resend_api_key:
        raise EmailError("RESEND_API_KEY is not set")
    payload = {"from": f"Hisaab Bolo <{s.resend_from}>", "to": [to], "subject": subject, "html": html_body}
    if attachments:
        payload["attachments"] = attachments
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post("https://api.resend.com/emails", json=payload,
                              headers={"Authorization": f"Bearer {s.resend_api_key}"})
    if r.status_code >= 300:
        log.error("resend %s: %s", r.status_code, r.text[:300])
        raise EmailError(f"Resend rejected the email ({r.status_code}): {r.text[:200]}")
    return EmailResult(id=r.json().get("id", ""), delivered_to=to, intended_to=intended_to)


# ---------------------------------------------------------------------------
# Templates (email-safe HTML: tables + inline styles), styled after slide 6.
# ---------------------------------------------------------------------------
def salutation(payer_name: str) -> str:
    first = payer_name.strip().split()[0] if payer_name.strip() else "there"
    return f"{first} ji"


def _qr_block(qr_url: str | None, qr_b64: str | None) -> tuple[str, list[dict]]:
    if is_public_url(qr_url):
        src, attachments = qr_url, []
    elif qr_b64:
        src, attachments = "cid:invoice-qr", [{"filename": "pay-qr.png", "content": qr_b64, "content_id": "invoice-qr"}]
    else:
        return "", []
    img = f'<img src="{html.escape(src or "")}" width="132" height="132" alt="UPI QR code" style="display:block;border:0">'
    return img, attachments


def render_payment_email(
    *,
    kind: str,
    merchant_name: str,
    payer_name: str,
    intended_to: str,
    number: str,
    amount_paise: int,
    issued_on: date,
    due_date: date,
    pay_url: str,
    qr_url: str | None,
    qr_b64: str | None,
    line_items: list[dict],
    gst_taxable: bool,
    payer_gstin: str | None,
    days_overdue: int = 0,
    upi_payee: str | None = None,
) -> tuple[str, str, list[dict]]:
    """Returns (subject, html, attachments) for an invoice or a reminder."""
    amount = format_inr(amount_paise)
    esc = html.escape
    issued = f"{issued_on.day} {issued_on.strftime('%b')}"
    due = f"{due_date.day} {due_date.strftime('%b')}"
    if kind == "overdue":
        subject = f"Reminder: {amount} due to {merchant_name}"
        heading = subject
        lead = (f"Hi {esc(salutation(payer_name))}, invoice #{esc(number)} from {issued} is still pending"
                + (f" ({days_overdue} day{'s' if days_overdue != 1 else ''} past due)" if days_overdue > 0 else "")
                + ". Pay in one tap or scan:")
    else:
        subject = f"Invoice {number} from {merchant_name}: {amount} due {due}"
        heading = f"{amount} due to {merchant_name} by {due}"
        lead = f"Hi {esc(salutation(payer_name))}, here is invoice #{esc(number)}. Pay in one tap or scan:"

    rows = "".join(
        f'<tr><td style="padding:6px 0;color:#0f1a33">{esc(str(i.get("name", "")))}'
        f'{" × " + str(i.get("qty")) if i.get("qty", 1) != 1 else ""}</td>'
        f'<td style="padding:6px 0;text-align:right;color:#0f1a33">'
        f'{format_inr(int(i.get("unit_price_paise", 0)) * int(i.get("qty", 1)))}</td></tr>'
        for i in line_items
    )
    gst = (f'<p style="margin:8px 0 0;color:#55617d;font-size:13px">GST invoice'
           f'{" · Customer GSTIN " + esc(payer_gstin) if payer_gstin else ""}</p>') if gst_taxable else ""
    qr_img, attachments = _qr_block(qr_url, qr_b64)
    body = f"""<!doctype html><html><body style="margin:0;background:#eef2fa;font-family:Arial,Helvetica,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef2fa;padding:24px 12px"><tr><td align="center">
<table role="presentation" width="560" cellpadding="0" cellspacing="0" style="max-width:560px;background:#f4f7fd;border-radius:20px;padding:28px">
<tr><td>
<p style="margin:0 0 14px;color:#55617d;font-size:13px">To: {esc(intended_to)}</p>
<h1 style="margin:0 0 10px;color:#0f1a33;font-size:22px;line-height:1.3">{esc(heading)}</h1>
<p style="margin:0 0 18px;color:#33405e;font-size:15px;line-height:1.5">{lead}</p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
<td valign="middle">
<a href="{esc(pay_url)}" style="display:inline-block;background:#2f6fed;color:#ffffff;text-decoration:none;font-weight:bold;font-size:15px;padding:12px 20px;border-radius:999px">Pay {amount} {"with any UPI app" if upi_payee else "with Paytm"}</a>
<p style="margin:10px 0 0;color:#55617d;font-size:13px">or scan with any UPI app{(" · pays " + esc(upi_payee)) if upi_payee else ""}</p>
</td>
<td width="140" align="right" valign="middle">{qr_img}</td>
</tr></table>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:22px;border-top:1px solid #d9e0ee;font-size:14px">
{rows}
<tr><td style="padding:10px 0 0;font-weight:bold;color:#0f1a33">Total</td><td style="padding:10px 0 0;text-align:right;font-weight:bold;color:#0f1a33">{amount}</td></tr>
</table>
{gst}
<p style="margin:18px 0 0;color:#8a94ab;font-size:12px">Invoice {esc(number)} · issued {issued} · due {due} · sent by {esc(merchant_name)} via Hisaab Bolo</p>
</td></tr></table>
</td></tr></table></body></html>"""
    return subject, body, attachments
