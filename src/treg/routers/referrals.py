"""HTTP routes for a person's referral program."""

from fastapi import APIRouter, Depends

from ..application import referrals
from ..config import get_settings
from ..domain.identity.access import require_identity
from ..models import User


# app is the APIRouter alias so mechanically moved @app decorators stay byte-identical.
app = APIRouter()


@app.get("/referrals")
async def my_referrals(
    user: User = Depends(require_identity),
) -> dict:
    """This person's referral link and everyone who has used it.

    Also runs the payout sweep, scoped to this user. There is no scheduler in treg, so the two
    trigger points are any top-up (`billing._credit`) and this page — which means someone checking
    whether their reward has landed is the one who makes it land. That is the same lazy,
    caller-pays-for-their-own-cleanup bargain as `ledger.reap_stale_holds`.
    """
    return await referrals.get_referral_summary(user.id)


@app.post("/referrals/code")
async def mint_referral_code(
    user: User = Depends(require_identity),
) -> dict:
    """Mint this person's referral code, or return the one they already have.

    Idempotent, so the dashboard can call it every time the page opens without checking first. Codes
    are minted here rather than at signup because most people never open this page, and a code
    nobody has seen is a unique index entry earning nothing.
    """
    code = await referrals.mint_code(user.id)
    return {"code": code, "link": f"{get_settings().public_url.rstrip('/')}/?ref={code}"}


router = app
