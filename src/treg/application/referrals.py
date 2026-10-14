"""Application journeys for the referral program."""

import logging

from sqlalchemy import inspect as sa_inspect

from ..infra.db import session_maker
from ..domain import referrals
from ..models import User


async def _user(db, user_id: int) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise RuntimeError("referral user disappeared")
    return user


async def get_referral_summary(user_id: int) -> dict:
    async with session_maker() as db:
        user = await _user(db, user_id)
        # Mint the code here too, not only on POST. Asking for this page IS the lazy trigger the code
        # was always meant to hang off, and every caller needs a usable `link` — a response carrying an
        # empty one is a footgun for any client that doesn't know to POST first.
        # Log the `user_id` primitive we already hold, never `user.id`: a failure below may have
        # rolled the session back, which expires the user row, and reading an expired attribute in
        # an async session raises MissingGreenlet - the log line would 500 the page it protects.
        try:
            await referrals.ensure_code(db, user)
        except Exception as exc:  # noqa: BLE001 — a code we couldn't mint is an empty link, not a 500
            logging.getLogger("treg").warning(
                "referral code mint failed for user %s: %s", user_id, exc)
        try:
            await referrals.sweep(db, referrer_user_id=user_id)
        except Exception as exc:  # noqa: BLE001 — pragma: no cover
            logging.getLogger("treg").warning(
                "referral sweep failed for user %s: %s", user_id, exc)
        # A recovery rollback above (the sweep's contained payout failure, or either except branch)
        # expired every object this session tracks; revive the user before summary's first attribute
        # read becomes implicit async I/O (MissingGreenlet). Same idiom as signup's
        # _redeem_referral. No-op on the happy path.
        if sa_inspect(user).expired:
            await db.refresh(user)
        return await referrals.summary(db, user)


async def mint_code(user_id: int) -> str:
    async with session_maker() as db:
        user = await _user(db, user_id)
        return await referrals.ensure_code(db, user)
