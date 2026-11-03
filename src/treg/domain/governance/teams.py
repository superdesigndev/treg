"""Team creation and deletion rules, and membership read models."""

import re

import sys

from sqlalchemy import delete, exists, func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlmodel import select

from ...models import (
    ShowcaseApp,
    ShowcaseAppEvent,
    ShowcaseAppKey,
    ShowcaseCursor,
    ShowcaseVote,
    ShowcaseWeek,
    AdConversion,
    ApiKey,
    ApiKeyEvent,
    ApiKeySpendDay,
    CallUsageDay,
    ArchiveEndpointStat,
    ArchiveKey,
    ArchiveKeyOrg,
    ArchiveSnapshot,
    ArenaEvaluation,
    ArenaRun,
    AsyncResourceRecord,
    AsyncTaskRecord,
    Bundle,
    CallRecord,
    CapabilityPin,
    CreditBlock,
    DenyRule,
    EnterpriseDirectoryUser,
    EnterpriseSSOAudit,
    EnterpriseSSO,
    EnterpriseSSOBlock,
    EnterpriseSSOIdentity,
    Feedback,
    CallReview,
    Media,
    Hold,
    HubListing,
    HubRun,
    HubTool,
    IdempotentCall,
    Invite,
    LedgerEntry,
    Membership,
    OAuthCode,
    OAuthGrant,
    OAuthRefresh,
    Org,
    OrgFlowControlVersion,
    PendingOAuth,
    Project,
    ProviderResource,
    Referral,
    RunRecord,
    Secret,
    TagBudget,
    TagSpend,
    TableDoc,
    TeamDatabase,
    Tool,
    ToolRequest,
    User,
    WebArenaRun,
)
from ..identity import session as sess
from ..identity import api_keys as managed_keys
from ..identity.access import _membership_by_token, _resolve_org, lock_user


MAX_OWNED_TEAMS = 10


class OwnedTeamLimitReached(Exception):
    """The account already owns the maximum number of teams."""


async def require_owned_team_slot(db: AsyncSession, user_id: int) -> None:
    """Hold the user lock through the ownership insert and its commit. No reservation counter."""
    await lock_user(db, user_id)
    owned = (await db.execute(select(Membership.id).where(
        Membership.user_id == user_id, Membership.role == "owner",
    ).limit(MAX_OWNED_TEAMS))).scalars().all()
    if len(owned) >= MAX_OWNED_TEAMS:
        raise OwnedTeamLimitReached


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "org"


async def _slug_taken(slug: str, db: AsyncSession) -> bool:
    """Current slugs and retired ones alike: a retired slug still resolves for the team that had it."""
    return (await db.execute(select(Org.id).where(
        (Org.slug == slug) | (Org.previous_slug == slug)).limit(1))).first() is not None


async def _unique_slug(base: str, db: AsyncSession) -> str:
    slug, i = base, 2
    while await _slug_taken(slug, db):
        slug, i = f"{base}-{i}", i + 1
    return slug


MAX_ORG_NAME = 80
SLUG_LEN = (3, 40)


# Letters that look like Latin ones in other scripts, and digits that stand in for letters: a name is
# judged by how it READS (`trеg-hub` with a Cyrillic е, `apol1o`).
_LOOKALIKE = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s",
    "к": "k", "м": "m", "н": "h", "т": "t", "в": "b", "г": "r", "ԁ": "d", "ɡ": "g", "ⅼ": "l",
    "α": "a", "ε": "e", "ο": "o", "ρ": "p", "τ": "t", "υ": "u", "ν": "v", "κ": "k", "ι": "i", "η": "n",
    "0": "o", "1": "l", "3": "e", "4": "a", "5": "s", "7": "t", "$": "s", "@": "a",
})
_KEPT_WORDS = ("official", "verified")


def _reads_as(text: str) -> tuple[list[str], str]:
    """The name as a reader sees it: NFKC-folded, lookalike letters mapped, lowercased; returns its
    words and the words joined with nothing between them."""
    import unicodedata
    s = unicodedata.normalize("NFKC", text or "").lower().translate(_LOOKALIKE)
    words = [w for w in re.split(r"[^a-z0-9]+", s) if w]
    return words, "".join(words)


def reserved_reason(text: str, extra: frozenset[str] | set[str] = frozenset()) -> str | None:
    """Why a team name or slug is reserved, or None. A team's slug is the first half of every hub
    tool id it publishes (`<slug>.<name>`) and the "by <team>" on its pages, so a name that reads as
    treg itself, as official, or as a catalog provider (`extra`, from the catalog) would let a
    stranger's tool pass for ours or theirs (hub simulation runs 2 and 3). Judged as it reads:
    lookalike letters mapped, separators removed, so `trеg-hub`, `t-r-e-g`, `tregg`, `hunter-io`
    and `Hunter.io data` are all caught. Superadmins may still use one."""
    words, joined = _reads_as(text)
    if joined.startswith("treg") or "treg" in words or any(w.startswith("treg") for w in words):
        return f"{text!r} is reserved: names that read as treg itself are kept for treg"
    if any(w.startswith(k) for w in words for k in _KEPT_WORDS):   # a word, so "unverified" passes
        return f"{text!r} is reserved: a team may not call itself official or verified"
    for name in extra:
        exact = name.startswith("=")           # a platform (people, web, ...): only the whole name
        flat = _reads_as(name.lstrip("="))[1]
        if flat and (joined == flat or (not exact and (flat in words or (len(flat) >= 5 and joined.startswith(flat))))):
            return f"{text!r} is reserved: it reads as {name!r}, a provider or platform in the treg catalog"
    return None


async def rename_org(db: AsyncSession, org: Org, *, name: str | None = None, slug: str | None = None,
                     reserved: frozenset[str] | set[str] = frozenset(), allow_reserved: bool = False) -> None:
    """Change a team's display name and/or slug. Caller commits.

    A slug change retires the old slug into ``previous_slug`` so credentials pinned to it keep
    working. Raises ValueError with a user-facing message on bad input or a taken slug."""
    if not allow_reserved:
        for text in (name, slug):
            if text and (why := reserved_reason(text, reserved)):
                raise ValueError(why)
    if name is not None:
        name = name.strip()
        if not name:
            raise ValueError("name must not be empty")
        org.name = name[:MAX_ORG_NAME]
    if slug is not None and slug != org.slug:
        if slug != _slugify(slug) or not SLUG_LEN[0] <= len(slug) <= SLUG_LEN[1]:
            raise ValueError(
                f"slug must be {SLUG_LEN[0]}-{SLUG_LEN[1]} lowercase letters, digits or hyphens")
        if slug.startswith("sbx-"):  # the sandbox-team shape (onboard/sandbox.py) is privilege-shaped
            raise ValueError("slug prefix 'sbx-' is reserved")
        if slug != org.previous_slug and await _slug_taken(slug, db):
            raise ValueError("slug is taken")
        org.previous_slug, org.slug = org.slug, slug


async def _make_org_membership(
    db: AsyncSession, user: User, name: str, slug_base: str, role: str, webhook_url: str | None = None
) -> tuple[Org, str]:
    """Create an Org + human Membership with only its signed default credential.

    The non-null legacy column stays empty until its later removal. The returned team-pinned
    identity token preserves the existing create-org response contract without manufacturing a
    second, hash-backed ``legacy_human`` key for a brand-new membership. Caller commits.
    """
    if role == "owner":
        await require_owned_team_slot(db, user.id)
    org = Org(name=name, slug=await _unique_slug(slug_base, db))
    db.add(org)
    await db.flush()
    membership = Membership(
            user_id=user.id, org_id=org.id, role=role,
            token_hash="", webhook_url=webhook_url,
        )
    db.add(membership)
    await db.flush()
    default = await managed_keys.ensure_default_key(db, membership, user)
    token = sess.make_identity(
        user.id, user.token_version, org=org.slug,
        key_generation=default.default_generation if default else None,
        scope=sess.TEAM_SCOPE,
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
        claims = sess.read_identity_claims(x_treg_token) if x_treg_token else None
        # A newly typed Default key is authoritative for its team. Legacy unmarked identity tokens
        # keep header-first selection during the migration window.
        ref = ((claims or {}).get("org", "") if (claims or {}).get("scope") == sess.TEAM_SCOPE
               else x_treg_org or (claims or {}).get("org", ""))
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
            "avatar_key": org.avatar_key,
        })
    return out


# Every model carrying an `org_id`, in foreign-key order: LedgerEntry before CreditBlock,
# IdempotentCall before Membership, Membership last.
ORG_SCOPED_MODELS = (
    ArenaEvaluation, ArenaRun, WebArenaRun,
    TableDoc, TeamDatabase,
    Tool, Secret, Bundle, PendingOAuth, CallRecord, RunRecord, Invite, DenyRule, Project,
    ApiKeyEvent, ApiKey,
    ApiKeySpendDay,  # the spend rollup: a deleted team's history goes with it
    CallUsageDay,  # the call usage rollup, likewise
    ShowcaseWeek, ShowcaseCursor,  # the /showcase activity rows: a deleted team's go with it
    CapabilityPin,
    OrgFlowControlVersion,  # configuration history belongs to the deleted team
    TagBudget,
    TagSpend,  # before the money tables it attributes: its rows reference a Hold that is about to go
    AsyncResourceRecord, ProviderResource, AsyncTaskRecord, LedgerEntry, Hold, CreditBlock,
    OAuthCode, OAuthRefresh,   # grants naming a team that no longer exists
    IdempotentCall,            # a remembered answer belongs to the team that paid for it
    ToolRequest,  # attribution rows go with the team; anonymous filings carry no org_id and stay
    Feedback,
    HubListing,   # a tool's search listing goes with the team that asked for it
    HubTool,      # a maker's published tools go with the team that owned them
    CallReview,
    Media,        # hosted reference files expire on their own; a deleted team's go now
    AdConversion,  # pending Google Ads conversions belong to the team they'd be attributed to
    EnterpriseSSOIdentity, EnterpriseSSOBlock, EnterpriseDirectoryUser, EnterpriseSSOAudit, EnterpriseSSO,
    Membership,   # last: it is what makes the caller a member of the org being deleted
)


ARCHIVE_PRIVATE_SCOPES = ("org", "conn")


async def private_archive_key_ids(db: AsyncSession, org_id: int, *, limit: int | None = None) -> list[int]:
    """The archive keys that are the team's own (docs/context/architecture/archive.md, "Opting
    out"): keys under its `org:`/`conn:` scopes. A private key holds only that team's snapshots,
    because only that team ever computes its hash ("Sharing"). Platform-key answers are public
    questions and are never the team's. Ascending by id, so a bounded pass is resumable."""
    owned = select(ArchiveSnapshot.key_id).where(
        ArchiveSnapshot.origin_org_id == org_id).distinct().scalar_subquery()
    rows = await db.execute(
        select(ArchiveKey.id)
        .where(ArchiveKey.scope.in_(ARCHIVE_PRIVATE_SCOPES), ArchiveKey.id.in_(owned))
        .order_by(ArchiveKey.id).limit(limit or sys.maxsize))
    return [int(r[0]) for r in rows]


async def archive_bodies_only_under(db: AsyncSession, key_ids: list[int]) -> set[str]:
    """The content hashes that only snapshots under `key_ids` point at: the bodies that become
    orphans once those keys go. Bodies are content-addressed and deduplicated across keys, so a
    hash some other key's snapshot shares is not in the set. One anti-join, so the hash list
    never travels back into a query as parameters."""
    if not key_ids:
        return set()
    other = aliased(ArchiveSnapshot)
    shared = exists().where(other.content_hash == ArchiveSnapshot.content_hash,
                            other.key_id.not_in(key_ids))
    rows = await db.execute(
        select(ArchiveSnapshot.content_hash).distinct()
        .where(ArchiveSnapshot.key_id.in_(key_ids), ~shared))
    return set(rows.scalars().all())


async def erase_archive_rows(db: AsyncSession, org_id: int, key_ids: list[int] | None = None) -> int:
    """Delete the team's archive rows in `db` (no commit) -> keys deleted.

    The `ArchiveKeyOrg` marks tying the team to questions it paid for go in one idempotent
    statement; the keys (default: every private key of the team, else the given ones) go with
    their snapshots and request shapes, and the endpoint running totals move with them. The
    totals are taken from what THIS statement deleted (`RETURNING`), never from a count read
    beforehand: two erasers on the same rows (the sweep and an owner delete, two instances in a
    rolling deploy) would otherwise both subtract, and only one of them deleted. The bytes an
    object store holds for those snapshots are the application's to remove BEFORE this
    (application/archive_erasure.py): a row that outlives a failed object delete is what lets the
    next pass retry, a deleted row would leave the object behind for good."""
    await db.execute(delete(ArchiveKeyOrg).where(ArchiveKeyOrg.org_id == org_id))
    if key_ids is None:
        key_ids = await private_archive_key_ids(db, org_id)
    if not key_ids:
        return 0
    # Snapshots first (they reference the key and, through `body_of`, each other within a key).
    gone = (await db.execute(
        delete(ArchiveSnapshot).where(ArchiveSnapshot.key_id.in_(key_ids))
        .returning(ArchiveSnapshot.key_id, ArchiveSnapshot.body_storage,
                   ArchiveSnapshot.body.is_not(None), ArchiveSnapshot.size_bytes))).all()
    keys_gone = (await db.execute(
        delete(ArchiveKey).where(ArchiveKey.id.in_(key_ids))
        .returning(ArchiveKey.id, ArchiveKey.endpoint_id))).all()
    endpoint_of = {int(key_id): endpoint_id for key_id, endpoint_id in keys_gone}
    # Running totals (archive.md, "The running totals"): atomic column arithmetic, like the pruner.
    per_endpoint: dict[str, list[int]] = {}
    for endpoint_id in endpoint_of.values():
        per_endpoint.setdefault(endpoint_id, [0, 0, 0, 0])[3] += 1
    for key_id, storage, has_body, size in gone:
        endpoint_id = endpoint_of.get(int(key_id))
        if endpoint_id is None:          # its key went in another eraser's statement
            continue
        acc = per_endpoint.setdefault(endpoint_id, [0, 0, 0, 0])
        acc[0] += 1
        if storage is not None or bool(has_body):
            acc[1] += 1
            acc[2] += int(size or 0)
    for endpoint_id, (n_snaps, n_bodies, n_bytes, n_keys) in per_endpoint.items():
        await db.execute(
            update(ArchiveEndpointStat).where(ArchiveEndpointStat.endpoint_id == endpoint_id)
            .values(snapshots=ArchiveEndpointStat.snapshots - n_snaps,
                    bodies_kept=ArchiveEndpointStat.bodies_kept - n_bodies,
                    kept_bytes=ArchiveEndpointStat.kept_bytes - n_bytes,
                    keys=ArchiveEndpointStat.keys - n_keys))
    return len(keys_gone)


async def cascade_delete_org(org: Org, db: AsyncSession) -> None:
    """Delete every org-scoped row, then the org. Does not commit.

    THE one way a team leaves the database - owner delete, admin force-delete, the landing-sandbox
    reaper and the onboarding demo reset all come through here. Do not grow a private copy of this
    list."""
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
    # HubRun names the team that CALLED as `caller_org_id` (the foreign key) and the maker only
    # by number: a caller's runs go with the caller; a maker's deletion leaves callers' traces.
    for run in (await db.execute(select(HubRun).where(HubRun.caller_org_id == org.id))).scalars().all():
        await db.delete(run)
    # The team's /showcase entries go with it, with their history, votes and key links (their
    # keys point at the entry, not the team, so the walk below would not reach them). Their public
    # pictures become unreferenced files; nothing names them any more.
    for app in (await db.execute(select(ShowcaseApp).where(ShowcaseApp.org_id == org.id))).scalars().all():
        for child in (ShowcaseVote, ShowcaseAppEvent, ShowcaseAppKey):
            for r in (await db.execute(select(child).where(child.app_id == app.id))).scalars().all():
                await db.delete(r)
        await db.flush()
        await db.delete(app)
    await db.flush()
    for model in ORG_SCOPED_MODELS:
        for r in (await db.execute(select(model).where(model.org_id == org.id))).scalars().all():
            await db.delete(r)
        await db.flush()   # honour the ordering above rather than leaving it to the unit of work
    # The archive keys no foreign key at the team (an own answer's reach is its KEY's scope), so
    # the FK walk in the guard test never sees these rows; they are the team's all the same. A
    # deletion that should also clear the object store runs `archive_erasure.erase_org` first.
    await erase_archive_rows(db, org.id)
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
    onboarding demo reset.

    ORG-wide rules (`user_id` NULL) are untouched: they are about the team, not about one caller.
    The caller commits - this only stages the deletes, so it composes with the removal itself."""
    q = select(DenyRule).where(DenyRule.user_id == user_id)
    if org_id is not None:
        q = q.where(DenyRule.org_id == org_id)
    stale = (await db.execute(q)).scalars().all()
    for rule in stale:
        await db.delete(rule)
    return len(stale)


async def delete_membership(
    db: AsyncSession, membership: Membership, *, actor_email: str = "",
) -> None:
    """Delete one membership and the caller-scoped state that has no meaning without it.

    The explicit IdempotentCall delete keeps SQLite tests honest even though their fast schema does
    not enforce foreign keys. The database FK also cascades as the invariant backstop, so a future
    membership-removal door cannot turn token revocation into a 500 by forgetting this helper.
    Does not commit.
    """
    user = await db.get(User, membership.user_id)
    await managed_keys.revoke_membership_keys(db, membership, actor_email=(
        actor_email or (user.email if user is not None else membership.created_by)
    ))
    await db.execute(delete(IdempotentCall).where(
        IdempotentCall.membership_id == membership.id))
    await drop_member_deny_rules(db, membership.user_id, membership.org_id)
    await db.delete(membership)


async def ensure_sso_membership(db: AsyncSession, user: User, org_id: int) -> tuple[Membership, bool]:
    """Join an authenticated SSO user to a team as a member; caller commits."""
    membership = (await db.execute(select(Membership).where(
        Membership.org_id == org_id, Membership.user_id == user.id,
    ))).scalar_one_or_none()
    if membership is not None:
        return membership, False
    membership = Membership(user_id=user.id, org_id=org_id, role="member", token_hash="")
    db.add(membership)
    await db.flush()
    await managed_keys.ensure_default_key(db, membership, user)
    return membership, True
