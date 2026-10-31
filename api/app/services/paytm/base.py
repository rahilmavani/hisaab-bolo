"""One interface for every Paytm call; PAYTM_MODE picks MockPaytm or RealPaytm."""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal, Protocol

TxnStatus = Literal["TXN_SUCCESS", "TXN_FAILURE", "PENDING", "NO_RECORD"]


@dataclass
class LinkResult:
    link_id: str
    short_url: str
    order_id: str
    link_type: str = "INVOICE"
    raw: dict = field(default_factory=dict)


@dataclass
class QrResult:
    qr_code_id: str
    qr_data: str
    image_b64: str
    order_id: str
    raw: dict = field(default_factory=dict)


@dataclass
class StatusResult:
    status: TxnStatus
    order_id: str
    txn_id: str | None = None
    bank_txn_id: str | None = None
    txn_amount: str | None = None
    payment_mode: str | None = None
    txn_date: str | None = None
    raw: dict = field(default_factory=dict)


class PaytmError(Exception):
    def __init__(self, code: str, message: str, raw: dict | None = None):
        super().__init__(f"Paytm {code}: {message}")
        self.code = code
        self.message = message
        self.raw = raw or {}


class DuplicateOrderError(PaytmError):
    """The gateway already has an order with this ID (e.g. QR sharing the link's order ID)."""


@dataclass
class LinkRequest:
    order_id: str
    amount_paise: int
    invoice_number: str
    link_name: str
    description: str
    expiry: date
    payer_name: str
    payer_email: str | None = None
    payer_phone: str | None = None
    notes: str = ""


@dataclass
class QrRequest:
    order_id: str
    amount_paise: int
    display_name: str
    invoice_number: str
    expiry: datetime


class PaytmClient(Protocol):
    async def create_link(self, req: LinkRequest) -> LinkResult: ...

    async def create_qr(self, req: QrRequest) -> QrResult: ...

    async def order_status(self, order_id: str) -> StatusResult: ...
