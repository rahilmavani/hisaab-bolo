import base64
import io
from urllib.parse import quote

import qrcode
from qrcode.constants import ERROR_CORRECT_M

from ..money import paise_to_amount_str


def upi_uri(vpa: str, payee: str, amount_paise: int, order_id: str, note: str) -> str:
    return (
        f"upi://pay?pa={quote(vpa)}&pn={quote(payee)}&am={paise_to_amount_str(amount_paise)}"
        f"&cu=INR&tr={quote(order_id)}&tn={quote(note)}"
    )


def upi_pay_uri(vpa: str, payee: str, amount_paise: int, note: str) -> str:
    """A UPI payment request to a plain UPI ID, amount filled in. No merchant-only fields (tr, mc):
    a personal UPI ID isn't a registered merchant, and apps may refuse those fields for one."""
    return (f"upi://pay?pa={quote(vpa, safe='@')}&pn={quote(payee)}&am={paise_to_amount_str(amount_paise)}"
            f"&cu=INR&tn={quote(note[:50])}")


def qr_png_b64(data: str) -> str:
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=10, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#0f1a33", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()
