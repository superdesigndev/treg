"""The `call_admission` decision point: an installed policy extension's say before treg's money pays.

Asked only when a handler listens, so a deployment without an extension reads nothing extra. The
two facts it adds (the team's first owner, whether the team ever bought credit) are read on the
session the call already holds, which is then committed: no connection waits on the extension.
A refusal names the remedy, never the rule (docs/context/architecture/policy-extensions.md).
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ... import policy
from ...config import get_settings
from ...domain import money
from ...domain.identity.access import first_owner_email
from .resolve import MarketplaceCall, _catalog_endpoint_for
from .types import AuthorizationFailed, CallerSnapshot


async def admit(db: AsyncSession, mk: MarketplaceCall, caller: CallerSnapshot) -> None:
    """Return when the call may spend treg's money; raise a 403 `AuthorizationFailed` when not."""
    if not policy.registry.asks_call_admission:
        return
    owner_email = await first_owner_email(db, caller.org_id)
    has_paid = await money.has_purchased(db, caller.org_id)
    await db.commit()
    decision = await policy.registry.call_admission(policy.CallAdmissionContext(
        org_id=caller.org_id,
        user_id=caller.user.id,
        caller_email=caller.user.email,
        owner_email=owner_email,
        org_has_paid=has_paid,
        org_created_at=caller.org.created_at,
        api_key_id=caller.api_key_id,
        endpoint_id=mk.endpoint_id,
        provider=mk.provider,
        platform=str((_catalog_endpoint_for(mk.endpoint_id) or {}).get("platform") or ""),
        funding="billed_oauth" if mk.billed_oauth else "platform",
        estimate_micro=int(mk.estimate_micro or 0),
    ))
    if decision is not policy.CallDecision.DENY:
        return
    wallet = f"treg's {mk.provider} " + ("app (pay-per-use)" if mk.billed_oauth else "key")
    alt = (f"  or bring your own {mk.provider} developer app (BYO OAuth) - those calls are never metered"
           if mk.billed_oauth else
           f"  or use your own key: treg connections connect --provider {mk.provider}")
    raise AuthorizationFailed("policy_denied", status_code=403, detail={
        "error": "funds_required",
        "message": (f"{mk.endpoint_id} can't run on {wallet} for this team until it adds funds. "
                    f"Nothing was charged.\n"
                    f"  add funds:      {get_settings().public_url}/app#billing\n" + alt),
        "topup_url": "/app#billing",
        "provider": mk.provider,
        "endpoint_id": mk.endpoint_id,
    })
