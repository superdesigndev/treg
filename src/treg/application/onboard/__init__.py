"""First-run onboarding journeys and their transaction boundaries."""

import json
import logging
import sys
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ... import crypto as _crypto
from ... import models as _models
from ... import ratestore
from ...config import get_settings
from ...infra.db import session_maker
from ...domain.identity.access import _norm_email
from ...models import Invite, Org, User
from ...sandbox_identity import visitor_name


# These aliases preserve demo.reset's relative model import; without them the package move breaks resolution.
sys.modules.setdefault(__name__ + ".crypto", _crypto)
sys.modules.setdefault(__name__ + ".models", _models)

from . import demo as demo_seed


SANDBOX_HIT_NS = "sandbox_hit"
SANDBOX_RATE_MAX = 12
SANDBOX_RATE_WINDOW_S = 3600


class OnboardError(Exception):
    """A framework-neutral onboarding refusal translated by the HTTP router."""

    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(kind)


class SandboxError(Exception):
    """A framework-neutral landing sandbox refusal translated by the HTTP router."""

    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(kind)


async def _user(user_id: int, db: AsyncSession) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise RuntimeError("authenticated user disappeared")
    return user


async def provision_demo(*, user_id: int, team_name: str) -> dict:
    async with session_maker() as db:
        return await demo_seed.provision(db, await _user(user_id, db), team_name)


async def skip(*, user_id: int) -> dict:
    async with session_maker() as db:
        user = await _user(user_id, db)
        user.onboarded = True
        await db.commit()
        return {"onboarded": True}


async def reset(*, user_id: int) -> dict:
    async with session_maker() as db:
        return await demo_seed.reset(db, await _user(user_id, db))


async def seed_tool(*, org_id: int, owner_email: str) -> dict:
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        if org is None:
            raise OnboardError("org_not_found")
        return await demo_seed.seed_tool(db, org, owner_email)


async def accept_teammate(*, org_id: int, email: str) -> dict:
    async with session_maker() as db:
        email = _norm_email(email)
        if not email.endswith("@" + demo_seed.DEMO_DOMAIN):
            raise OnboardError("not_demo_email")
        invite = (await db.execute(select(Invite).where(
            Invite.org_id == org_id,
            Invite.email == email,
            Invite.status == "pending",
        ))).scalar_one_or_none()
        if invite is None:
            raise OnboardError("invite_not_found")
        return await demo_seed.accept_demo_invite(db, org_id, invite)


async def mint_sandbox(*, client_ip: str) -> dict:
    from . import sandbox as landing_sandbox

    async with session_maker() as db:
        await ratestore.sweep(db, SANDBOX_HIT_NS)
        allowed = await ratestore.rate_check(
            db, SANDBOX_HIT_NS, [(client_ip, SANDBOX_RATE_MAX)], SANDBOX_RATE_WINDOW_S)
        if not allowed:
            await db.commit()
            raise SandboxError("rate_limited")
        await db.commit()
        # Reap opportunistically, but never let the reaper close the front door: a gc failure is
        # logged and rolled back, and the visitor still gets their sandbox. Before this guard, one
        # undeletable expired sandbox turned every mint into a 500 for six hours.
        try:
            await landing_sandbox.gc(db)
        except Exception:  # noqa: BLE001 - anything the reaper raises is a bug to log, not to serve
            await db.rollback()
            logging.getLogger("treg").exception("sandbox gc failed; minting anyway")
        out = await landing_sandbox.mint(db)
        out["live"] = bool(get_settings().demo_stripe_key)
        return out


def sandbox_live_facts(org: Org) -> dict:
    from ... import sandbox as sandbox_runtime

    if not sandbox_runtime.is_sandbox(org):
        raise SandboxError("sandbox_only_live")
    return {"live": bool(get_settings().demo_stripe_key),
            "visitor": visitor_name(org.slug)}


async def accept_stripe_event(
    *, payload_factory: Callable[[], Awaitable[bytes]], signature: str,
) -> dict:
    from . import pubfeed

    secret = get_settings().demo_stripe_webhook_secret
    if not secret:
        raise SandboxError("webhook_unconfigured")
    payload = await payload_factory()
    if not pubfeed.verify_signature(payload, signature, secret):
        raise SandboxError("bad_signature")
    try:
        event = json.loads(payload)
    except ValueError:
        raise SandboxError("bad_payload")
    if event.get("type") == "charge.succeeded":
        pubfeed.push_charge(event.get("data", {}).get("object", {}) or {})
    return {"received": True}


def stripe_feed():
    from . import pubfeed

    return pubfeed.stream()


async def export_sandbox_skill(*, org_id: int, sandbox: bool) -> dict:
    from . import sandbox as landing_sandbox

    if not sandbox:
        raise SandboxError("sandbox_only_skill")
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        if org is None:
            raise RuntimeError("sandbox organization disappeared")
        return await landing_sandbox.export_skill(db, org)


def sample_skills(*, base: str) -> list[dict]:
    from . import sandbox as landing_sandbox

    return [{"name": n, "label": s["label"], "key": s["key"], "prompt": s["prompt"],
             "files": landing_sandbox.skill_files(n, base, None)}
            for n, s in landing_sandbox.SAMPLE_SKILLS.items()]


def sandbox_install_script(*, name: str, base: str, token: str | None) -> str:
    from . import sandbox as landing_sandbox

    return landing_sandbox.install_script(name, base, token)
