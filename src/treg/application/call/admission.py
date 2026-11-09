"""The `call_admission` decision point: an installed policy extension's say before treg's money pays.

Asked only when a handler listens, so a deployment without an extension reads nothing extra. The two
facts it adds (the team's first owner, whether the team kept any credit it bought) are one read on the
session the call already holds, which is then committed: no connection waits on the extension.
A refusal names the remedy, never the rule (docs/context/architecture/policy-extensions.md).
"""
from __future__ import annotations

from sqlalchemy import and_, exists, not_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ... import policy
from ...config import get_settings
from ...models import CreditBlock, LedgerEntry, Membership, User
from .reserve import wallet_and_alternative
from .resolve import MarketplaceCall, _catalog_endpoint_for
from .types import AuthorizationFailed, CallerSnapshot


class AdmissionDenied(AuthorizationFailed):
    """A 403 `admission_denied`. `has_paid` decides the remedy, so a routed job's summary of its
    refused candidates offers the same one."""

    def __init__(self, detail: dict, *, has_paid: bool) -> None:
        super().__init__("admission_denied", status_code=403, detail=detail)
        self.has_paid = has_paid


async def admit(db: AsyncSession, mk: MarketplaceCall, caller: CallerSnapshot) -> None:
    """Return when the call may spend treg's money; raise `AdmissionDenied` when not."""
    if not policy.registry.asks_call_admission:
        return
    owner_email = (select(User.email).join(Membership, Membership.user_id == User.id)
                   .where(Membership.org_id == caller.org_id, Membership.role == "owner")
                   .order_by(Membership.id).limit(1).scalar_subquery())
    # A top-up Stripe refunded is revoked (money.md "revoke"); only one never revoked counts as paid.
    kept_purchase = exists().where(
        CreditBlock.org_id == caller.org_id, CreditBlock.kind == "purchased",
        not_(exists().where(and_(LedgerEntry.block_id == CreditBlock.id, LedgerEntry.kind == "revoke"))))
    owner, paid = (await db.execute(select(owner_email, kept_purchase))).one()
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
        wallet, alt = wallet_and_alternative(mk)
        raise _denied(f"{mk.endpoint_id} can't run on {wallet} for this team", alt, has_paid=bool(paid),
                      provider=mk.provider, endpoint_id=mk.endpoint_id)


def routed_refusal(endpoint_id: str, refusals: list[AdmissionDenied], tried: list[dict]) -> AdmissionDenied:
    """Every candidate of a routed job was refused treg's money: the job's refusal, same remedy."""
    return _denied(f"no candidate for {endpoint_id} can run on treg's money for this team",
                   "  or connect your own key for one of the providers tried",
                   has_paid=refusals[0].has_paid, endpoint_id=endpoint_id, tried=tried)


def _denied(subject: str, alt: str, *, has_paid: bool, **fields) -> AdmissionDenied:
    """Adding funds is offered only to a team that kept no credit it bought: a team that did could
    not lift the refusal that way."""
    if has_paid:
        detail = {"error": "own_key_required",
                  "message": f"{subject}. Nothing was charged.\n" + alt.replace("  or ", "  ", 1)}
    else:
        detail = {"error": "funds_required",
                  "message": (f"{subject} until it adds funds. Nothing was charged.\n"
                              f"  add funds:      {get_settings().public_url}/app#billing\n" + alt),
                  "topup_url": "/app#billing"}
    return AdmissionDenied(detail | fields, has_paid=has_paid)
