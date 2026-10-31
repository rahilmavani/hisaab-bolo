"""Shared request validation."""

from datetime import date
from typing import Annotated

from fastapi import HTTPException
from pydantic import AfterValidator

MIN_DATE, MAX_DATE = date(2000, 1, 1), date(2100, 12, 31)


def no_nul(value: str | None) -> str | None:
    if value is not None and "\x00" in value:
        raise ValueError("text cannot contain NUL characters")
    return value


SafeStr = Annotated[str, AfterValidator(no_nul)]


def check_query_text(value: str, field: str = "q") -> str:
    if "\x00" in value:
        raise HTTPException(422, f"{field} cannot contain NUL characters")
    return value


def check_date(value: date | None, field: str = "date") -> date | None:
    if value is not None and not MIN_DATE <= value <= MAX_DATE:
        raise HTTPException(422, f"{field} must be between {MIN_DATE} and {MAX_DATE}")
    return value
