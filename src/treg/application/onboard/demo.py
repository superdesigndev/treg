"""Cleanup for teams the retired onboarding demo created.

Those teams were REAL teams owned by the caller, populated with fake teammates (roster-only User
rows, `demo=True`, on an unusable domain so they can never log in - see the guard in
`application/auth.py`), an `echo` tool and sample CallRecords. `reset` removes the whole footprint
(org cascade + orphaned demo users). Self-contained (models only) -> no import cycle with api.py.
"""
from __future__ import annotations

from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...domain.governance.teams import cascade_delete_org, drop_member_deny_rules
from .models import Membership, Org, User

DEMO_DOMAIN = "demo.treg.local"      # fake teammates live here; api refuses login for this domain


async def existing_demo_org(db: AsyncSession, owner: User) -> Org | None:
    """A demo team this user owns, if any."""
    mems = (await db.execute(select(Membership).where(
        Membership.user_id == owner.id, Membership.role == "owner"))).scalars().all()
    for m in mems:
        org = await db.get(Org, m.org_id)
        if org is not None and org.demo:
            return org
    return None


async def reset(db: AsyncSession, owner: User) -> dict:
    """Remove every demo team this owner has, plus any demo users left with no memberships."""
    removed = []
    while True:
        org = await existing_demo_org(db, owner)
        if org is None:
            break
        await cascade_delete_org(org, db)  # the shared cascade, never a private table list
        removed.append(org.slug)
        await db.flush()
    # dashboard flow: demo teammates the user invited into their OWN (real) teams — drop those memberships
    my_org_ids = {m.org_id for m in (await db.execute(
        select(Membership).where(Membership.user_id == owner.id))).scalars().all()}
    for oid in my_org_ids:
        for m in (await db.execute(select(Membership).where(Membership.org_id == oid))).scalars().all():
            u = await db.get(User, m.user_id)
            if u is not None and u.demo:
                await db.delete(m)
    await db.flush()
    # sweep orphaned demo users (fake teammates no longer in any org)
    demo_users = (await db.execute(select(User).where(User.demo == True))).scalars().all()  # noqa: E712
    for u in demo_users:
        still = (await db.execute(select(Membership).where(Membership.user_id == u.id))).scalars().first()
        if still is None:
            # A member-scoped deny rule in the caller's REAL team may still name this user, and
            # `DenyRule.user_id` is a foreign key: sweep first or Postgres refuses the delete.
            await drop_member_deny_rules(db, u.id)
            await db.delete(u)
    await db.commit()
    return {"removed": removed}
