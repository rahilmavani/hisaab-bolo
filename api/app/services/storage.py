import base64
from urllib.parse import urlparse

import httpx

from ..config import get_settings

QR_BUCKET = "qr"


async def upload_qr_png(path: str, image_b64: str) -> str:
    """Store the QR PNG in the public `qr` bucket and return its public URL."""
    s = get_settings()
    headers = {
        "apikey": s.supabase_secret_key,
        "Authorization": f"Bearer {s.supabase_secret_key}",
        "Content-Type": "image/png",
        "x-upsert": "true",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            f"{s.supabase_url}/storage/v1/object/{QR_BUCKET}/{path}", headers=headers,
            content=base64.b64decode(image_b64),
        )
    r.raise_for_status()
    return f"{s.supabase_url}/storage/v1/object/public/{QR_BUCKET}/{path}"


def is_public_url(url: str | None) -> bool:
    """Email clients fetch images from the internet; localhost URLs never load there."""
    if not url:
        return False
    host = urlparse(url).hostname or ""
    return host not in ("localhost", "127.0.0.1", "0.0.0.0", "::1") and not host.endswith(".local")
