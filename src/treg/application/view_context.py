"""What a server-rendered view says about its caller beyond the route's own answer: the providers
it holds a credential for, the provider its team pinned for a job, which credential would serve an
endpoint, and the team's auto top-up policy."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..domain.identity.access import Caller, _role_at_least
from ..models import CapabilityPin
from . import billing, connect
from .call.access import catalog_endpoint_access
from .call.types import CallFailure


async def connected_providers(caller: Caller) -> set[str]:
    rows = await connect.list_connections(org_id=caller.org_id)
    return {str(row.get("provider") or "") for row in rows if row.get("provider")}


async def pinned_provider(db: AsyncSession, caller: Caller, capability: str | None) -> str | None:
    if not capability:
        return None
    pin = (await db.execute(select(CapabilityPin).where(
        CapabilityPin.org_id == caller.org_id,
        CapabilityPin.capability == capability).order_by(CapabilityPin.id))).scalars().first()
    return pin.provider if pin is not None else None


async def endpoint_access(db: AsyncSession, caller: Caller, endpoint_id: str) -> str | None:
    try:
        access = await catalog_endpoint_access(endpoint_id=endpoint_id, authorization_method="", caller=caller, db=db)
    except CallFailure:
        return None
    return access.get("detail")


async def autotopup(caller: Caller) -> dict | None:
    """The policy `GET /billing` shows an admin, when billing is configured."""
    if caller.org is None or not _role_at_least(caller.role, "admin"):
        return None
    state = await billing.get_billing_state(caller.org_id)
    return state.get("autotopup") if state.get("configured") else None
