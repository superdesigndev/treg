"""HTTP referral cookie helpers used by signup entry points."""

from fastapi import Request

from ..application import affiliates
from ..domain import referrals
from ..infra import dub
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


AFFILIATE_COOKIE = "treg_dub"
# Dub's own client script keeps the click for 90 days by default; the server-side cookie matches it.
AFFILIATE_COOKIE_MAX_AGE = 90 * 24 * 3600


def _remember_affiliate_click(resp, request: Request) -> None:
    """Park Dub's `?dub_id=` click id until the visitor creates a team. Last click wins, as Dub's
    default attribution model does."""
    click_id = dub.valid_click_id(request.query_params.get("dub_id", ""))
    if click_id and affiliates.enabled():
        resp.set_cookie(AFFILIATE_COOKIE, click_id, httponly=True, samesite="lax",
                        secure=_is_https(request), max_age=AFFILIATE_COOKIE_MAX_AGE)


PARTNER_CODE_COOKIE = "treg_partner"


def _remember_partner_code(resp, request: Request, code: str) -> None:
    """Park the partner code `treg login --ref` carried into the browser sign-in, so the team made
    from that sign-in is attributed to the partner. Never the friend-referral cookie: a typed code
    is only ever a Dub partner's code."""
    code = dub.valid_partner_code(code)
    if code and affiliates.enabled():
        resp.set_cookie(PARTNER_CODE_COOKIE, code, httponly=True, samesite="lax",
                        secure=_is_https(request), max_age=AFFILIATE_COOKIE_MAX_AGE)


def _take_partner_code(request: Request) -> str:
    """The parked partner code, revalidated: a cookie is attacker-supplied."""
    return dub.valid_partner_code((request.cookies.get(PARTNER_CODE_COOKIE) or "").strip('"'))


def _forget_affiliate(resp) -> None:
    """Drop the parked click and partner code once a team is made: a partner earns on the person
    they brought, not on every team that browser makes in the next 90 days."""
    resp.delete_cookie(AFFILIATE_COOKIE)
    resp.delete_cookie(PARTNER_CODE_COOKIE)


def _client_country(request: Request) -> str:
    """Cloudflare's view of the caller's country, when the request came through it."""
    return request.headers.get("cf-ipcountry", "")


def _take_affiliate_click(request: Request) -> str:
    """The parked click id, revalidated: a cookie is attacker-supplied."""
    return dub.valid_click_id(request.cookies.get(AFFILIATE_COOKIE) or "")
