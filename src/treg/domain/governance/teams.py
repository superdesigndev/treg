"""Team creation rules and membership read models."""

import re

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ... import crypto
from ...models import Membership, Org, Tool, User
from ..identity import session as sess
from ..identity.access import _membership_by_token, _resolve_org


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "org"


async def _unique_slug(base: str, db: AsyncSession) -> str:
    slug, i = base, 2
    while (await db.execute(select(Org).where(Org.slug == slug))).scalar_one_or_none() is not None:
        slug, i = f"{base}-{i}", i + 1
    return slug


async def _make_org_membership(
    db: AsyncSession, user: User, name: str, slug_base: str, role: str, webhook_url: str | None = None
) -> tuple[Org, str]:
    """Create an Org + an owner/role Membership for `user`, minting a fresh org-scoped token.
    Returns (org, plaintext token). Caller commits.
    """
    org = Org(name=name, slug=await _unique_slug(slug_base, db))
    db.add(org)
    await db.flush()
    token = crypto.new_token()
    db.add(
        Membership(
            user_id=user.id, org_id=org.id, role=role,
            token_hash=crypto.hash_token(token), webhook_url=webhook_url,
        )
    )
    return org, token


async def list_user_orgs(
    *, user_id: int, x_treg_token: str, x_treg_org: str, db: AsyncSession,
) -> list[dict]:
    # Active means the membership token's org, or the browser/identity token's selected org.
    current: int | None = None
    if x_treg_token and (membership := await _membership_by_token(x_treg_token, db)):
        current = membership.org_id
    else:
        # X-Treg-Org wins, then a team-pinned identity token's claim, matching require_member.
        ref = x_treg_org or (
            (sess.read_claims(x_treg_token) or {}).get("org", "") if x_treg_token else ""
        )
        org = await _resolve_org(ref, db)
        current = org.id if org else None
    memberships = (
        await db.execute(select(Membership).where(Membership.user_id == user_id))
    ).scalars().all()
    org_ids = [membership.org_id for membership in memberships]
    # Batch org and tool-count reads for the org switcher; per-membership queries make this N+1.
    orgs = {
        org.id: org for org in (await db.execute(
            select(Org).where(Org.id.in_(org_ids))
        )).scalars().all()
    }
    tool_counts = dict((await db.execute(
        select(Tool.org_id, func.count(Tool.id))
        .where(Tool.org_id.in_(org_ids))
        .group_by(Tool.org_id)
    )).all())
    out: list[dict] = []
    for membership in memberships:
        org = orgs.get(membership.org_id)
        if org is None:
            continue
        out.append({
            "org_id": org.id,
            "slug": org.slug,
            "name": org.name,
            "role": membership.role,
            "active": org.id == current,
            "demo": org.demo,
            "tool_count": tool_counts.get(org.id, 0),
        })
    return out
