from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .config import get_settings

_pool: AsyncConnectionPool | None = None


async def open_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(
            get_settings().database_url,
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row, "autocommit": True},
            # Survive database restarts: drop dead connections instead of handing them out.
            check=AsyncConnectionPool.check_connection,
            open=False,
        )
        await _pool.open(wait=True)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def connection() -> AsyncIterator[AsyncConnection]:
    pool = await open_pool()
    async with pool.connection() as conn:
        yield conn


async def get_conn() -> AsyncIterator[AsyncConnection]:
    """FastAPI dependency: one autocommit connection per request."""
    async with connection() as conn:
        yield conn


async def fetch_all(conn: AsyncConnection, sql: str, params: dict | tuple | None = None) -> list[dict]:
    cur = await conn.execute(sql, params)
    return await cur.fetchall()


async def fetch_one(conn: AsyncConnection, sql: str, params: dict | tuple | None = None) -> dict | None:
    cur = await conn.execute(sql, params)
    return await cur.fetchone()


async def app_now(conn: AsyncConnection) -> datetime:
    row = await fetch_one(conn, "select app_now() as now")
    assert row
    return row["now"]


async def app_today(conn: AsyncConnection) -> date:
    row = await fetch_one(conn, "select app_today() as today")
    assert row
    return row["today"]
