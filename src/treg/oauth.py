"""Compatibility facade for connection OAuth helpers.

New code belongs in ``domain.connections`` and ``infra``. Existing imports can use this module while
callers move to those boundaries.
"""

from __future__ import annotations

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from . import oauth_providers
from .domain.connections.oauth_flow import pkce_challenge as pkce_challenge
from .domain.connections.refresh import (
    EXPIRING_SOON_DAYS as EXPIRING_SOON_DAYS,
    _expires_at as _expires_at,
    ensure_fresh as _ensure_fresh,
    expiry_state as expiry_state,
    secret_is_refreshable as secret_is_refreshable,
)
from .infra.oauth_exchange import HTTPXOAuthExchangePort
from .infra.oauth_refresh import HTTPXOAuthRefreshPort
from .models import PendingOAuth, Secret


async def exchange_code(pending: PendingOAuth, code: str, client: httpx.AsyncClient) -> dict:
    return await HTTPXOAuthExchangePort(client).exchange_code(pending, code)


async def refresh(blob: dict, client: httpx.AsyncClient) -> dict:
    return await HTTPXOAuthRefreshPort(client).exchange(blob)


async def ensure_fresh(secret: Secret, db: AsyncSession, client: httpx.AsyncClient) -> None:
    await _ensure_fresh(secret, db, HTTPXOAuthRefreshPort(client), oauth_providers.platform_refresh_blob)
