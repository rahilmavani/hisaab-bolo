"""RealPaytm: Paytm staging/production APIs. Contract tests: `pytest -m staging`.

Hosts differ per API (securestage.paytmpayments.com vs securegw-stage.paytmpayments.in).
The first real responses are logged in full so field names can be confirmed
(the docs' response samples could not be captured).
"""

import logging

import httpx

from ...money import paise_to_amount_str, paise_to_rupees
from .base import (
    DuplicateOrderError,
    LinkRequest,
    LinkResult,
    PaytmError,
    QrRequest,
    QrResult,
    StatusResult,
)
from .checksum import signed_request

log = logging.getLogger(__name__)

HOSTS = {
    "staging": {"link": "https://securestage.paytmpayments.com", "gw": "https://securegw-stage.paytmpayments.in"},
    "production": {"link": "https://secure.paytmpayments.com", "gw": "https://securegw.paytmpayments.in"},
}
QR_DUPLICATE_CODES = {"QR_1021", "QR_1033", "QR_1034", "DQR_0006", "QR_1020"}


class RealPaytm:
    def __init__(self, mode: str, mid: str, key: str, callback_base: str):
        if mode not in HOSTS:
            raise ValueError(f"unknown Paytm mode {mode}")
        if not mid or not key:
            raise PaytmError("CONFIG", "PAYTM_MID and PAYTM_MERCHANT_KEY are required outside mock mode")
        self.mode, self.mid, self.key = mode, mid, key
        self.hosts = HOSTS[mode]
        self.callback_base = callback_base

    async def _post(self, url: str, body: dict, head: dict) -> dict:
        payload, _ = signed_request(body, head, self.key)
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(url, content=payload.encode(), headers={"Content-Type": "application/json"})
        try:
            data = r.json()
        except ValueError as e:
            raise PaytmError(str(r.status_code), f"non-JSON response: {r.text[:200]}") from e
        log.info("paytm %s -> %s %s", url, r.status_code, data)
        return data

    async def create_link(self, req: LinkRequest) -> LinkResult:
        body = {
            "mid": self.mid,
            "linkType": "INVOICE",
            "linkName": req.link_name,
            "linkDescription": req.description,
            "amount": float(paise_to_rupees(req.amount_paise)),
            "invoiceId": req.invoice_number,
            "expiryDate": req.expiry.strftime("%d/%m/%Y"),
            "linkOrderId": req.order_id,
            "statusCallbackUrl": f"{self.callback_base}/webhooks/paytm/link",
            "customerContact": {k: v for k, v in {
                "customerName": req.payer_name,
                "customerEmail": req.payer_email,
                "customerMobile": req.payer_phone,
            }.items() if v},
            "sendSms": False,
            "sendEmail": False,
            "linkNotes": req.notes[:254],
        }
        url = f"{self.hosts['link']}/link/create"
        data = await self._post(url, body, {"tokenType": "AES"})
        info = data.get("body", {}).get("resultInfo", {})
        if info.get("resultStatus") != "SUCCESS":
            code = str(info.get("resultCode"))
            log.warning("INVOICE link rejected (%s %s); retrying as FIXED", code, info.get("resultMessage"))
            fixed = {k: v for k, v in body.items() if k not in ("invoiceId", "expiryDate")} | {"linkType": "FIXED"}
            data = await self._post(url, fixed, {"tokenType": "AES"})
            info = data.get("body", {}).get("resultInfo", {})
            if info.get("resultStatus") != "SUCCESS":
                raise PaytmError(str(info.get("resultCode")), str(info.get("resultMessage")), data)
            link_type = "FIXED"
        else:
            link_type = "INVOICE"
        b = data["body"]
        return LinkResult(
            link_id=str(b.get("linkId") or b.get("linkID") or ""),
            short_url=b.get("shortUrl") or b.get("longUrl") or "",
            order_id=req.order_id,
            link_type=link_type,
            raw=data,
        )

    async def create_qr(self, req: QrRequest) -> QrResult:
        body = {
            "mid": self.mid,
            "orderId": req.order_id,
            "amount": paise_to_amount_str(req.amount_paise),
            "businessType": "UPI_QR_CODE",
            "posId": "HB_WEB_01",
            "displayName": req.display_name[:50],
            "invoiceDetails": req.invoice_number,
            "expiryDate": req.expiry.strftime("%Y-%m-%d %H:%M:%S"),
        }
        data = await self._post(f"{self.hosts['gw']}/paymentservices/qr/create", body,
                                {"clientId": "C11", "version": "v1"})
        b = data.get("body", {})
        info = b.get("resultInfo", {})
        if info.get("resultStatus") != "SUCCESS":
            code = str(info.get("resultCode"))
            cls = DuplicateOrderError if code in QR_DUPLICATE_CODES else PaytmError
            raise cls(code, str(info.get("resultMsg") or info.get("resultMessage")), data)
        raw = {**data, "body": {k: v for k, v in b.items() if k != "image"}}
        return QrResult(qr_code_id=b.get("qrCodeId", ""), qr_data=b.get("qrData", ""), image_b64=b.get("image", ""),
                        order_id=req.order_id, raw=raw)

    async def order_status(self, order_id: str) -> StatusResult:
        data = await self._post(f"{self.hosts['gw']}/v3/order/status", {"mid": self.mid, "orderId": order_id}, {})
        b = data.get("body", {})
        status = b.get("resultInfo", {}).get("resultStatus", "PENDING")
        if status == "NO_RECORD_FOUND":
            status = "NO_RECORD"
        elif status not in ("TXN_SUCCESS", "TXN_FAILURE", "PENDING"):
            status = "PENDING"
        return StatusResult(status=status, order_id=order_id, txn_id=b.get("txnId"), bank_txn_id=b.get("bankTxnId"),
                            txn_amount=b.get("txnAmount"), payment_mode=b.get("paymentMode"),
                            txn_date=b.get("txnDate"), raw=data)
