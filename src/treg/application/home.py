"""What bare `treg` shows, in one read: the caller's account, team, balance and own tools, and how
many tools the catalog holds."""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..domain import money
from ..domain.catalog import store as catalog_store
from ..domain.governance import access as access_policy
from ..domain.identity.access import Caller
from ..models import Tool
from . import billing


async def summary(db: AsyncSession, email: str, caller: Caller | None, *, teams: int) -> dict:
    """`caller` is None when no team resolves (none yet, or a stale active one): the account and its
    team count still answer, so the CLI can name the fix in the same round trip."""
    # The first read in a process parses every catalog file; off the event loop, so a role that has
    # not loaded the catalog yet keeps serving sign-ins meanwhile.
    cat = await asyncio.to_thread(catalog_store.load)
    out = {"account": email, "teams": teams, "team": None,
           "catalog_tools": catalog_store.headline_counts(cat)[0].replace(",", "")}
    if caller is None:
        return out
    balance = await money.balance_of(db, caller.org_id)
    # Reading the balance is one of auto top-up's trigger points, as on GET /orgs/{id}/balance.
    billing.maybe_schedule_autotopup(caller.org)
    rows = (await db.execute(select(Tool).where(Tool.org_id == caller.org_id))).scalars().all()
    return out | {"team": caller.org.slug, "role": caller.role, "balance_micro": balance,
                  "own_tools": [t.name for t in access_policy.usable_tools(caller, rows)]}
