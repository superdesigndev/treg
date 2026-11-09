"""The `call_admission` decision point: an installed policy extension's say before treg's money pays.

Asked only when a handler listens, so a deployment without an extension reads nothing extra. The two
facts it adds (the team's first owner, whether the team ever bought credit) are one read on the
session the call already holds, which is then committed: no connection waits on the extension.
A refusal names the remedy, never the rule (docs/context/architecture/policy-extensions.md).
"""
from __future__ import annotations

from sqlalchemy import exists
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ... import policy
from ...config import get_settings
from ...models import CreditBlock, Membership, User
from .reserve import wallet_and_alternative
from .resolve import MarketplaceCall, _catalog_endpoint_for
from .types import AuthorizationFailed, CallerSnapshot


async def admit(db: AsyncSession, mk: MarketplaceCall, caller: CallerSnapshot) -> None:
    """Return when the call may spend treg's money; raise a 403 `AuthorizationFailed` when not."""
    if not policy.registry.asks_call_admission:
        return
    owner_email = (select(User.email).join(Membership, Membership.user_id == User.id)
                   .where(Membership.org_id == caller.org_id, Membership.role == "owner")
                   .order_by(Membership.id).limit(1).scalar_subquery())
    has_paid = exists().where(CreditBlock.org_id == caller.org_id, CreditBlock.kind == "purchased")
    owner, paid = (await db.execute(select(owner_email, has_paid))).one()
    await db.commit()
    decision = await policy.registry.call_admission(policy.CallAdmissionContext(
        org_id=caller.org_id,
        user_id=caller.user.id,
        caller_email=caller.user.email,
        owner_email=owner,
        org_has_paid=bool(paid),
        org_created_at=caller.org.created_at,
        api_key_id=caller.api_key_id,
        endpoint_id=mk.endpoint_id,
        provider=mk.provider,
        platform=str((_catalog_endpoint_for(mk.endpoint_id) or {}).get("platform") or ""),
        funding="billed_oauth" if mk.billed_oauth else "platform",
        estimate_micro=int(mk.estimate_micro or 0),
    ))
    if decision is policy.CallDecision.DENY:
        raise refusal(mk, has_paid=bool(paid))


def refusal(mk: MarketplaceCall, *, has_paid: bool) -> AuthorizationFailed:
    """The public refusal: adding funds is offered only to a team that never bought credit, the one
    remedy a team that already paid could not use."""
    wallet, alt = wallet_and_alternative(mk)
    if has_paid:
        error = "own_key_required"
        message = (f"{mk.endpoint_id} can't run on {wallet} for this team. Nothing was charged.\n"
                   + alt.replace("  or ", "  ", 1))
    else:
        error, message = "funds_required", (
            f"{mk.endpoint_id} can't run on {wallet} for this team until it adds funds. Nothing was charged.\n"
            f"  add funds:      {get_settings().public_url}/app#billing\n" + alt)
    detail = {"error": error, "message": message, "provider": mk.provider, "endpoint_id": mk.endpoint_id}
    if not has_paid:
        detail["topup_url"] = "/app#billing"
    return AuthorizationFailed("admission_denied", status_code=403, detail=detail)


def routed_refusal(endpoint_id: str, refusals: list, tried: list[dict]) -> AuthorizationFailed:
    """Every candidate of a routed job was refused treg's money: the job's refusal, same remedy."""
    unpaid = isinstance(refusals[0].detail, dict) and refusals[0].detail.get("error") == "funds_required"
    if unpaid:
        error = "funds_required"
        message = (f"no candidate for {endpoint_id} can run on treg's money for this team until it adds "
                   f"funds. Nothing was charged.\n  add funds:      {get_settings().public_url}/app#billing\n"
                   "  or connect your own key for one of the providers tried")
    else:
        error = "own_key_required"
        message = (f"no candidate for {endpoint_id} can run on treg's money for this team. Nothing was "
                   "charged.\n  connect your own key for one of the providers tried")
    detail = {"error": error, "message": message, "endpoint_id": endpoint_id, "tried": tried}
    if unpaid:
        detail["topup_url"] = "/app#billing"
    return AuthorizationFailed("admission_denied", status_code=403, detail=detail)
