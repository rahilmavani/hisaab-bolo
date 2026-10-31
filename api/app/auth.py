"""Supabase JWT verification. Tokens are ES256-signed; keys come from the project's JWKS."""

import asyncio
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg import AsyncConnection

from .config import get_settings
from .db import fetch_one, get_conn

_bearer = HTTPBearer(auto_error=False)
_jwks: jwt.PyJWKClient | None = None


@dataclass(frozen=True)
class Merchant:
    id: UUID
    name: str
    user_id: UUID


def _jwks_client() -> jwt.PyJWKClient:
    global _jwks
    if _jwks is None:
        _jwks = jwt.PyJWKClient(get_settings().supabase_jwks_url, cache_keys=True, lifespan=3600)
    return _jwks


async def verify_token(token: str) -> dict:
    try:
        alg = jwt.get_unverified_header(token).get("alg")
        if alg not in ("ES256", "RS256"):
            raise jwt.InvalidAlgorithmError(f"unsupported alg {alg}")
        key = await asyncio.to_thread(_jwks_client().get_signing_key_from_jwt, token)
        return jwt.decode(token, key.key, algorithms=[alg], audience="authenticated")
    except jwt.PyJWTError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid token: {e}") from e


async def current_merchant(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    conn: Annotated[AsyncConnection, Depends(get_conn)],
) -> Merchant:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    claims = await verify_token(creds.credentials)
    row = await fetch_one(conn, "select id, name from merchants where owner_user_id = %s", (claims["sub"],))
    if row is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no merchant for this user")
    return Merchant(id=row["id"], name=row["name"], user_id=UUID(claims["sub"]))


def require_cron_secret(x_cron_secret: Annotated[str | None, Header()] = None) -> None:
    expected = get_settings().cron_secret
    if not expected or x_cron_secret != expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad cron secret")


CurrentMerchant = Annotated[Merchant, Depends(current_merchant)]
Conn = Annotated[AsyncConnection, Depends(get_conn)]
