"""HTTP referral cookie helpers used by signup entry points."""

from fastapi import Request

from ..domain import referrals
from .auth_helpers import _is_https


REFERRAL_COOKIE = "treg_ref"
# A month. The gap between clicking a friend's link and actually creating a team is measured in days
# for the people this program is for — they read the landing, think about it, and come back. Shorter
# would silently drop the referrals that took the longest to convert, which are exactly the genuine
# ones; much longer would keep attributing a signup to a link somebody forgot they ever clicked.
REFERRAL_COOKIE_MAX_AGE = 30 * 24 * 3600


def _remember_referral(resp, request: Request, code: str) -> None:
    """Park a referral code from `/?ref=…` until the visitor creates their first team.

    Same shape as `_remember_oauth_return`: httponly (no script needs it), `samesite=lax` so it
    survives the click through to a GitHub/Google sign-in and back, and `secure` only when we are
    actually on HTTPS so local development still works.

    First code wins is NOT enforced here — the cookie is simply overwritten. Someone who clicks two
    different referral links before signing up gets attributed to the second, which is both the
    normal advertising convention (last touch) and the one that needs no extra state.
    """
    resp.set_cookie(REFERRAL_COOKIE, code, httponly=True, samesite="lax",
                    secure=_is_https(request), max_age=REFERRAL_COOKIE_MAX_AGE)


def _take_referral(request: Request) -> str:
    """The parked code, revalidated. "" when there is none or it is not a shape we ever mint.

    Validated on READ as well as on write, exactly like `_take_oauth_return`: a cookie is
    attacker-supplied, and this value reaches a database query. `normalize_code` is a strict
    allowlist, so anything odd becomes "" and the signup simply proceeds unreferred.
    """
    return referrals.normalize_code((request.cookies.get(REFERRAL_COOKIE) or "").strip('"'))
