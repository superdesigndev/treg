"""Shared timestamp conventions for database-backed server code."""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def as_naive(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=None) if (dt is not None and dt.tzinfo is not None) else dt
