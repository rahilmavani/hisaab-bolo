from ...config import get_settings
from .base import (
    DuplicateOrderError,
    LinkRequest,
    LinkResult,
    PaytmClient,
    PaytmError,
    QrRequest,
    QrResult,
    StatusResult,
)
from .mock import MockPaytm
from .real import RealPaytm

__all__ = [
    "DuplicateOrderError", "LinkRequest", "LinkResult", "MockPaytm", "PaytmClient", "PaytmError",
    "QrRequest", "QrResult", "RealPaytm", "StatusResult", "get_paytm",
]


def get_paytm() -> PaytmClient:
    s = get_settings()
    if s.mock_mode:
        return MockPaytm()
    return RealPaytm(s.paytm_mode, s.paytm_mid, s.paytm_merchant_key, s.public_api_base)
