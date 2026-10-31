"""Paytm checksum helpers (pip `paytmchecksum` 1.7.0).

Outgoing JSON APIs sign the exact JSON string of `body`; we serialize it once and
splice it into the request so the signed bytes are the sent bytes (else 5028).
Incoming webhooks are form key-value pairs signed over the sorted values.
"""

import json
import logging

from paytmchecksum import PaytmChecksum

log = logging.getLogger(__name__)


def dumps(obj: dict) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def signed_request(body: dict, head: dict, key: str) -> tuple[str, str]:
    """Returns (request_json, signature) where signature covers exactly `body` as sent."""
    body_str = dumps(body)
    signature = PaytmChecksum.generateSignature(body_str, key)
    request = '{"body":' + body_str + ',"head":' + dumps({**head, "signature": signature}) + "}"
    return request, signature


def sign_form(params: dict[str, str], key: str) -> str:
    return PaytmChecksum.generateSignature({k: v for k, v in params.items() if k != "CHECKSUMHASH"}, key)


def verify_form(params: dict[str, str], key: str) -> bool:
    """True only for a well-formed CHECKSUMHASH made with `key`. Never raises."""
    data = {k: ("" if v is None else str(v)) for k, v in params.items()}
    checksum = data.pop("CHECKSUMHASH", None)
    if not checksum or not key:
        return False
    try:
        return bool(PaytmChecksum.verifySignature(data, key, checksum))
    except Exception as e:  # garbled base64 / wrong key size / bad padding
        log.warning("checksum verification error: %s", e)
        return False
