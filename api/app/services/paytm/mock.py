"""MockPaytm: same inputs/outputs as RealPaytm, state kept in `paytm_mock_orders`.

`simulate_payment` plays the gateway: it marks the order paid, builds the exact
link-payment webhook field set, signs it with the mock key using the real
PaytmChecksum library and POSTs it (form-encoded) to our real webhook route.
"""

import logging
import secrets
from datetime import datetime

import httpx

from ...config import get_settings
from ...db import app_now, connection, fetch_one
from ...money import IST, paise_to_amount_str
from ..qr import qr_png_b64, upi_uri
from .base import LinkRequest, LinkResult, PaytmError, QrRequest, QrResult, StatusResult
from .checksum import sign_form

log = logging.getLogger(__name__)


class MockPaytm:
    mode = "mock"

    async def create_link(self, req: LinkRequest) -> LinkResult:
        s = get_settings()
        if req.amount_paise < 100:
            raise PaytmError("5009", "Amount should be greater than equal to Rs 1")
        async with connection() as conn:
            n = (await fetch_one(conn, "select nextval('paytm_mock_link_seq') as n"))["n"]
            link_id = f"MOCK_LI_{n}"
            await conn.execute(
                "insert into paytm_mock_orders (order_id, kind, link_id, amount_paise) values (%s, 'link', %s, %s) "
                "on conflict (order_id) do update set link_id = excluded.link_id",
                (req.order_id, link_id, req.amount_paise),
            )
        short_url = f"{s.public_web_base}/mock-pay/{req.order_id}"
        raw = {"mock": True, "linkId": link_id, "shortUrl": short_url, "linkOrderId": req.order_id,
               "resultInfo": {"resultStatus": "SUCCESS", "resultCode": "200"}}
        return LinkResult(link_id=link_id, short_url=short_url, order_id=req.order_id, raw=raw)

    async def create_qr(self, req: QrRequest) -> QrResult:
        data = upi_uri("mock@paytm", req.display_name, req.amount_paise, req.order_id, req.invoice_number)
        async with connection() as conn:
            await conn.execute(
                "insert into paytm_mock_orders (order_id, kind, amount_paise) values (%s, 'qr', %s) "
                "on conflict (order_id) do nothing",
                (req.order_id, req.amount_paise),
            )
        qr_id = f"MOCK_QR_{secrets.token_hex(6).upper()}"
        return QrResult(qr_code_id=qr_id, qr_data=data, image_b64=qr_png_b64(data), order_id=req.order_id,
                        raw={"mock": True, "qrCodeId": qr_id, "qrData": data,
                             "resultInfo": {"resultStatus": "SUCCESS", "resultCode": "QR_0001"}})

    async def order_status(self, order_id: str) -> StatusResult:
        async with connection() as conn:
            row = await fetch_one(conn, "select * from paytm_mock_orders where order_id = %s", (order_id,))
        if row is None:
            return StatusResult(status="NO_RECORD", order_id=order_id, raw={"mock": True})
        txn_date = row["txn_at"].astimezone(IST).strftime("%Y-%m-%d %H:%M:%S.0") if row["txn_at"] else None
        return StatusResult(
            status=row["status"] if row["status"] != "PENDING" else "PENDING",
            order_id=order_id,
            txn_id=row["txn_id"],
            bank_txn_id=f"BANK{row['txn_id'][-10:]}" if row["txn_id"] else None,
            txn_amount=paise_to_amount_str(row["amount_paise"]),
            payment_mode=row["payment_mode"],
            txn_date=txn_date,
            raw={"mock": True, "status": row["status"]},
        )


def _txn_id(now: datetime) -> str:
    return now.strftime("%Y%m%d%H%M%S") + "MOCK" + "".join(secrets.choice("0123456789") for _ in range(18))


async def simulate_payment(
    order_id: str,
    outcome: str,
    *,
    payer_phone: str | None = None,
    payer_email: str | None = None,
    cust_id: str | None = None,
    link_description: str = "",
    link_notes: str = "",
) -> dict[str, str]:
    """Settle a mock order and return the signed webhook form fields."""
    s = get_settings()
    success = outcome.startswith("success")
    async with connection() as conn:
        async with conn.transaction():
            order = await fetch_one(conn, "select * from paytm_mock_orders where order_id = %s for update", (order_id,))
            if order is None:
                raise PaytmError("404", "Data Not Found")
            if order["status"] == "TXN_SUCCESS":
                raise PaytmError("ALREADY_PAID", "This link has already been paid")
            now = (await app_now(conn)).astimezone(IST)
            txn_id = _txn_id(now)
            await conn.execute(
                "update paytm_mock_orders set status = %s, txn_id = %s, payment_mode = 'UPI', txn_at = %s "
                "where order_id = %s",
                ("TXN_SUCCESS" if success else "TXN_FAILURE", txn_id, now, order_id),
            )
    fields = {
        "ORDERID": order_id,
        "MID": s.paytm_mid_active,
        "TXNID": txn_id,
        "TXNAMOUNT": paise_to_amount_str(order["amount_paise"]),
        "CURRENCY": "INR",
        "PAYMENTMODE": "UPI",
        "TXNDATE": now.strftime("%Y-%m-%d"),
        "TXNDATETIME": now.strftime("%Y-%m-%d %H:%M:%S.0"),
        "STATUS": "TXN_SUCCESS" if success else "TXN_FAILURE",
        "RESPCODE": "01" if success else "227",
        "RESPMSG": "Txn Success" if success else "Your payment has been declined by your bank.",
        "GATEWAYNAME": "PPBLC",
        "BANKTXNID": f"{secrets.randbelow(10**12):012d}",
        "BANKNAME": "Paytm Payments Bank",
        "LINKDESCRIPTION": link_description,
        "PAYMENTEMAILID": payer_email or "",
        "PAYMENTMOBILENUMBER": payer_phone or "",
        "PAYMENTFORM": "",
        "CUSTID": cust_id or "",
        "MERC_UNQ_REF": order["link_id"] or "",
        "LINKNOTES": link_notes,
    }
    fields["CHECKSUMHASH"] = sign_form(fields, s.paytm_mock_key)
    return fields


async def send_webhook(path: str, fields: dict[str, str]) -> int:
    """Deliver like Paytm does: an S2S form POST to our public webhook URL."""
    url = f"{get_settings().public_api_base}{path}"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, data=fields)
    log.info("mock webhook %s -> %s", url, r.status_code)
    return r.status_code
