"""Team creation and deletion rules, and membership read models."""

import re

from sqlalchemy import delete, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ... import crypto
from ...models import (
    AdConversion,
    Bundle,
    CallRecord,
    CapabilityPin,
    CreditBlock,
    DenyRule,
    Hold,
    IdempotentCall,
    Invite,
    LedgerEntry,
    Membership,
    OAuthCode,
    OAuthGrant,
    OAuthRefresh,
    Org,
    PendingOAuth,
    Project,
    Referral,
    RunRecord,
    Secret,
    TagBudget,
    TagSpend,
    Tool,
    ToolRequest,
    User,
)
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


# Every model carrying an `org_id`. Deleting the org without clearing these leaves rows pointing at
# a row that no longer exists, and the delete fails with a 500 at the foreign key.
#
# This list has to be kept in step with the schema, and twice it was not: the money tables arrived
# with the prepaid balance and `CapabilityPin` with capability pins, and neither was added here. The
# effect was invisible until someone tried it - since every NEW team is granted $1.00, every team has
# a CreditBlock, so NO team could be deleted at all. `test_org_delete_clears_every_org_scoped_table`
# now walks the models module and fails if a new one is ever missed, rather than trusting this list.
#
# Order matters: LedgerEntry references a CreditBlock, so it goes first; `IdempotentCall.membership_id`
# points at Membership, so Membership stays last and IdempotentCall sits above it.
ORG_SCOPED_MODELS = (
    Tool, Secret, Bundle, PendingOAuth, CallRecord, RunRecord, Invite, DenyRule, Project,
    CapabilityPin,
    TagBudget,
    TagSpend,  # before the money tables it attributes: its rows reference a Hold that is about to go
    LedgerEntry, Hold, CreditBlock,
    OAuthCode, OAuthRefresh,   # grants naming a team that no longer exists
    IdempotentCall,            # a remembered answer belongs to the team that paid for it
    ToolRequest,  # attribution rows go with the team; anonymous filings carry no org_id and stay
    AdConversion,  # pending Google Ads conversions belong to the team they'd be attributed to
    Membership,   # last: it is what makes the caller a member of the org being deleted
)


async def cascade_delete_org(org: Org, db: AsyncSession) -> None:
    """Delete every org-scoped row, then the org. Does not commit.

    THE one way a team leaves the database - owner delete, admin force-delete, the landing-sandbox
    reaper and the onboarding demo reset all come through here. There used to be three lists of
    "which tables to clear": this one, one in the sandbox reaper and one in the demo reset. Only this
    one was guarded by a test, so when `IdempotentCall` arrived the other two never learned of it,
    and on 2026-09-02 the reaper hit its foreign key on every run - from inside the sandbox mint, so
    every visitor got a 500 until it was noticed. Do not grow a private copy of this list again."""
    # OAuthGrant names its mutable team `current_org_id` to distinguish family authority from the
    # immutable `OAuthRefresh.org_id` provenance. A family can name this team on EITHER side: after
    # a move, only a retired provenance row still names the former team. Deleting just that row
    # destroys the replay evidence while leaving the live family authorised elsewhere, so a stolen
    # old token becomes "unknown" instead of revoking every descendant. Revoke the union of both
    # paths; preserving historical provenance across team deletion would need a nullable/soft FK.
    authority_grants = (await db.execute(select(OAuthGrant).where(
        OAuthGrant.current_org_id == org.id))).scalars().all()
    provenance_families = (await db.execute(select(OAuthRefresh.family_id).where(
        OAuthRefresh.org_id == org.id))).scalars().all()
    family_ids = {grant.family_id for grant in authority_grants} | set(provenance_families)
    if family_ids:
        # Delete the WHOLE family, including rows issued under other teams. Keeping only the live
        # destination token would be exactly the partial revocation that reuse detection forbids.
        for token in (await db.execute(select(OAuthRefresh).where(
            OAuthRefresh.family_id.in_(family_ids)))).scalars().all():
            await db.delete(token)
        grants = (await db.execute(select(OAuthGrant).where(
            OAuthGrant.family_id.in_(family_ids)))).scalars().all()
    else:
        grants = []
    for grant in grants:
        await db.delete(grant)
    # Referral names the team it credited as `referred_org_id`, not `org_id`, so neither the list
    # above nor a column-name walk sees it. The guard test walks FOREIGN KEYS to `org` instead, and
    # this line plus the OAuthGrant block above are its two hand-handled exceptions.
    for referral in (await db.execute(select(Referral).where(
            Referral.referred_org_id == org.id))).scalars().all():
        await db.delete(referral)
    await db.flush()
    for model in ORG_SCOPED_MODELS:
        for r in (await db.execute(select(model).where(model.org_id == org.id))).scalars().all():
            await db.delete(r)
        await db.flush()   # honour the ordering above rather than leaving it to the unit of work
    await db.delete(org)


async def drop_member_deny_rules(db: AsyncSession, user_id: int, org_id: int | None = None) -> int:
    """Delete the member-scoped rules that named a member/agent who is going away - the caller they
    were written for no longer exists, so the rule can never fire again. Left behind, they show up in
    the Policy table as a row naming a user id the team can no longer see or clean up. Mirrors how
    `delete_project` sweeps the id it deletes out of every `project_access`.

    `org_id` set = that org only (the member left THIS team but may still be in others). `org_id`
    None = every org, for when the USER row itself is deleted - `DenyRule.user_id` is a foreign key,
    so a surviving rule would dangle, which Postgres rejects outright (SQLite does not enforce it by
    default, which is why only a real deployment would have shown this). Every path that deletes a
    User row must call this first: member removal, leave, agent revoke, admin user delete AND the
    onboarding demo reset, which skipped it until the 2026-09-02 review.

    ORG-wide rules (`user_id` NULL) are untouched: they are about the team, not about one caller.
    The caller commits - this only stages the deletes, so it composes with the removal itself."""
    q = select(DenyRule).where(DenyRule.user_id == user_id)
    if org_id is not None:
        q = q.where(DenyRule.org_id == org_id)
    stale = (await db.execute(q)).scalars().all()
    for rule in stale:
        await db.delete(rule)
    return len(stale)


async def delete_membership(db: AsyncSession, membership: Membership) -> None:
    """Delete one membership and the caller-scoped state that has no meaning without it.

    The explicit IdempotentCall delete keeps SQLite tests honest even though their fast schema does
    not enforce foreign keys. The database FK also cascades as the invariant backstop, so a future
    membership-removal door cannot turn token revocation into a 500 by forgetting this helper.
    Does not commit.
    """
    await db.execute(delete(IdempotentCall).where(
        IdempotentCall.membership_id == membership.id))
    await drop_member_deny_rules(db, membership.user_id, membership.org_id)
    await db.delete(membership)
