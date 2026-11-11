"""The /showcase leaderboard: applications, review, weekly activity, the public board and votes.

treg is the only writer of every showcase table:

- makers (an owner or admin of the team) apply, edit and withdraw through `apply`, `edit`,
  `withdraw`. A revision is stored in `ShowcaseApp.pending` and is not public until approved; an
  edit to a live app leaves the live version in place until then.
- staff review through `approve`, `reject`, `set_hidden`, `staff_edit`, `staff_create` (the
  private admin calls them through the staff API). Approval publishes the revision's pictures
  first and only then points the row at them, so a page never names a missing file.
- `refresh_activity` (the `treg-worker showcase activity` cron) adds the new calls of each team with
  an approved app to one `ShowcaseWeek` row per UTC week, reading only calls after the team's
  `ShowcaseCursor`, in small batches. Approval counts nothing; a new app joins the activity boards
  at the next run.
- `leaderboard` and `vote` are the public side; activity leaves only through
  `domain.showcase.public_entry`.

Every database step opens and closes its own session around picture storage I/O (AGENTS.md
rule 3). Each change appends a `ShowcaseAppEvent` under the next version, in the same commit.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import ratestore
from ..domain import showcase as rules
from ..domain.catalog import store as catalog_store
from ..domain.identity.api_keys import is_machine_email
from ..domain.images import ImageRejected, parse_key
from ..infra.db import session_maker
from ..models import (ApiKey, CallRecord, Membership, Org, ShowcaseApp, ShowcaseAppEvent, ShowcaseAppKey,
                      ShowcaseCursor, ShowcaseVote, ShowcaseWeek, User)
from ..timeutil import utcnow_naive
from . import images

log = logging.getLogger("treg.showcase")

VOTE_NS = "showcase_vote"
VOTE_WINDOW_S = 3600
VOTE_MAX_PER_USER = 60
VOTE_MAX_PER_IP = 300
APPLY_NS = "showcase_apply"
APPLY_MAX_PER_USER = 20


LIMIT_DETAIL = f"a team can list at most {rules.MAX_APPS_PER_TEAM} apps"
TEAM = 0            # the `api_key_id` of a whole-team activity source
MAX_KEYS_PER_APP = 10


class ShowcaseError(Exception):
    """A framework-neutral refusal translated by the HTTP router."""

    def __init__(self, kind: str, detail: str = ""):
        self.kind, self.detail = kind, detail
        super().__init__(kind)


def _insert(db: AsyncSession):
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert


def live_of(app: ShowcaseApp) -> dict:
    return {f: getattr(app, f) for f in rules.REVISION_FIELDS}


def _set_live(app: ShowcaseApp, rev: dict) -> None:
    for f in rules.REVISION_FIELDS:
        setattr(app, f, list(rev[f]) if f == "shot_keys" else rev[f])


def _event(db: AsyncSession, app: ShowcaseApp, action: str, changed: dict, *, actor: str, source: str,
           note: str = "") -> None:
    app.version += 1
    app.updated_at = utcnow_naive()
    db.add(ShowcaseAppEvent(app_id=app.id, version=app.version, action=action, changes=changed,
                            note=note, actor=actor, source=source))


def _check_keys(rev: dict) -> None:
    if rev["icon_key"] is not None:
        parsed = parse_key(rev["icon_key"])
        if parsed is None or parsed[0].name != "showcase_icon":
            raise ShowcaseError("invalid", "icon_key: unknown picture")
    for key in rev["shot_keys"]:
        parsed = parse_key(key)
        if parsed is None or parsed[0].name != "showcase_shot":
            raise ShowcaseError("invalid", "shot_keys: unknown picture")


def _clean(raw: dict) -> tuple[dict, dict[str, bool]]:
    """The reviewed revision and the show flags, which apply at once."""
    fields, flags = rules.split_submission(raw)
    try:
        rev = rules.clean_revision(fields)
    except rules.ApplicationInvalid as exc:
        raise ShowcaseError("invalid", f"{exc.field}: {exc.detail}") from None
    _check_keys(rev)
    return rev, flags


def _flags_of(app: ShowcaseApp) -> dict[str, bool]:
    return {f: bool(getattr(app, f)) for f in rules.SHOW_FLAGS}


def _set_flags(db: AsyncSession, app: ShowcaseApp, flags: dict[str, bool], *, actor: str, source: str) -> bool:
    """Apply show flags at once, with a "sharing" event when any changed."""
    changed = rules.flag_changes(_flags_of(app), flags)
    if changed:
        for f, value in flags.items():
            setattr(app, f, value)
        _event(db, app, "sharing", changed, actor=actor, source=source)
    return bool(changed)


def _shot_urls(key: str) -> dict:
    return {"url": images.public_url(key), "small_url": images.public_url(key.replace(".webp", "-640.webp"))}


def _preview(rev: dict | None, *, flags: dict[str, bool], public: set[str] | None = None) -> dict | None:
    """A revision with picture URLs. Pictures in `public` (all of them for the live version; for a
    proposal, those it keeps from the live version) come from the public side; new ones are
    previewed from staging, as they are public only once approved."""
    if rev is None:
        return None

    def url(k):
        if not k:
            return None
        return images.public_url(k) if public is None or k in public else images.staging_url(k)
    return {**rev, **flags, "icon_url": url(rev["icon_key"]), "shot_urls": [url(k) for k in rev["shot_keys"]]}


def app_view(app: ShowcaseApp, *, org: Org | None = None, usage: dict | None = None) -> dict:
    """The maker's and staff's view of one entry: status, the live version, the proposed one, and
    where its activity comes from."""
    view = {
        "id": app.id, "org_id": app.org_id, "status": app.status, "version": app.version,
        "usage": usage or {"source": app.usage_source},
        "live": _preview(live_of(app), flags=_flags_of(app)) if app.approved_at else None,
        "pending": _preview(app.pending, flags=_flags_of(app), public=_live_keys(app)),
        "reject_reason": app.reject_reason, "approved_at": app.approved_at.isoformat() if app.approved_at else None,
        "created_at": app.created_at.isoformat(), "updated_at": app.updated_at.isoformat(),
    }
    if org is not None:
        view["team"] = {"id": org.id, "slug": org.slug, "name": org.name}
    return view


# An app counts all of its team's calls, or only those made with the API keys linked to it. A key
# that was rotated points at its replacement (`ApiKey.replacement_key_id`); the replacement counts
# for the same app, so a rotation never needs an edit.

async def _org_keys(db: AsyncSession, org_ids) -> dict[int, ApiKey]:
    ids = sorted(set(org_ids))
    if not ids:
        return {}
    return {k.id: k for k in (await db.execute(select(ApiKey).where(ApiKey.org_id.in_(ids)))).scalars().all()}


def _chain(keys: dict[int, ApiKey], key_id: int) -> list[int]:
    """A key and every key that replaced it, oldest first."""
    out: list[int] = []
    while key_id in keys and key_id not in out:
        out.append(key_id)
        key_id = keys[key_id].replacement_key_id or 0
    return out


async def _links(db: AsyncSession, app_ids) -> dict[int, list[int]]:
    ids = sorted(set(app_ids))
    out: dict[int, list[int]] = {i: [] for i in ids}
    if ids:
        for key_id, app_id in (await db.execute(select(ShowcaseAppKey.api_key_id, ShowcaseAppKey.app_id)
                                                .where(ShowcaseAppKey.app_id.in_(ids))
                                                .order_by(ShowcaseAppKey.api_key_id))).all():
            out[app_id].append(key_id)
    return out


async def _sources(db: AsyncSession, apps) -> dict[int, list[tuple[int, int]]]:
    """Each app's activity sources: `(org_id, TEAM)`, or `(org_id, key)` for every linked key and
    its replacements."""
    apps = list(apps)
    links = await _links(db, [a.id for a in apps if a.usage_source == "keys"])
    keys = await _org_keys(db, [a.org_id for a in apps if a.usage_source == "keys"])
    out: dict[int, list[tuple[int, int]]] = {}
    for a in apps:
        if a.usage_source == "team":
            out[a.id] = [(a.org_id, TEAM)]
        else:
            out[a.id] = sorted({(a.org_id, k) for linked in links.get(a.id, []) for k in _chain(keys, linked)})
    return out


def _key_view(key: ApiKey) -> dict:
    return {"id": key.id, "name": key.name, "prefix": key.safe_prefix or "",
            "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None}


async def _views(db: AsyncSession, apps, *, orgs: dict[int, Org] | None = None) -> list[dict]:
    """`app_view` for each app, with its usage: the source and the linked keys as they are now (a
    rotated key shows as its newest replacement). Only a team's owner or admin and staff see it."""
    apps = list(apps)
    links = await _links(db, [a.id for a in apps if a.usage_source == "keys"])
    keys = await _org_keys(db, [a.org_id for a in apps if a.id in links])
    out = []
    for a in apps:
        usage = {"source": a.usage_source}
        if a.id in links:
            newest = {_chain(keys, k)[-1] for k in links[a.id] if k in keys}
            usage["keys"] = [_key_view(keys[k]) for k in sorted(newest)]
        out.append(app_view(a, org=(orgs or {}).get(a.org_id), usage=usage))
    return out


async def _view(db: AsyncSession, app: ShowcaseApp, **kw) -> dict:
    return (await _views(db, [app], **kw))[0]


async def team_role(db: AsyncSession, user_id: int, org_id: int) -> str | None:
    return (await db.execute(select(Membership.role).where(
        Membership.user_id == user_id, Membership.org_id == org_id))).scalars().first()


async def _owned_app(db: AsyncSession, user: User, app_id: int) -> ShowcaseApp:
    app = await db.get(ShowcaseApp, app_id)
    if app is None:
        raise ShowcaseError("not_found")
    if await team_role(db, user.id, app.org_id) not in ("owner", "admin"):
        raise ShowcaseError("forbidden", "owner or admin role in this team is required")
    return app


async def my_teams(user: User) -> list[dict]:
    """The teams this person may apply for (owner or admin), with how many entries each has."""
    async with session_maker() as db:
        rows = (await db.execute(
            select(Org.id, Org.slug, Org.name)
            .join(Membership, Membership.org_id == Org.id)
            .where(Membership.user_id == user.id, Membership.role.in_(("owner", "admin")), Org.demo.is_(False))
            .order_by(Org.name))).all()
        counts = dict((await db.execute(
            select(ShowcaseApp.org_id, func.count())
            .where(ShowcaseApp.status != "rejected", ShowcaseApp.org_id.in_([r[0] for r in rows]))
            .group_by(ShowcaseApp.org_id))).all()) if rows else {}
    return [{"id": i, "slug": s, "name": n, "apps": counts.get(i, 0), "limit": rules.MAX_APPS_PER_TEAM}
            for i, s, n in rows]


async def team_apps(user: User, org_id: int) -> list[dict]:
    async with session_maker() as db:
        if await team_role(db, user.id, org_id) not in ("owner", "admin"):
            raise ShowcaseError("forbidden", "owner or admin role in this team is required")
        apps = (await db.execute(select(ShowcaseApp).where(ShowcaseApp.org_id == org_id)
                                 .order_by(ShowcaseApp.id))).scalars().all()
        return await _views(db, apps)


async def _team_full(db: AsyncSession, org_id: int, *, besides: int | None = None) -> bool:
    """Whether the team already lists MAX_APPS_PER_TEAM entries (rejected ones do not count)."""
    stmt = select(func.count()).where(ShowcaseApp.org_id == org_id, ShowcaseApp.status != "rejected")
    if besides is not None:
        stmt = stmt.where(ShowcaseApp.id != besides)
    return (await db.execute(stmt)).scalar_one() >= rules.MAX_APPS_PER_TEAM


async def apply(user: User, org_id: int, raw: dict) -> dict:
    """A new application for `org_id`. Owner or admin; one entry per team (MAX_APPS_PER_TEAM;
    a rejected one does not count)."""
    rev, flags = _clean(raw)
    async with session_maker() as db:
        if await team_role(db, user.id, org_id) not in ("owner", "admin"):
            raise ShowcaseError("forbidden", "owner or admin role in this team is required")
        org = await db.get(Org, org_id)
        if org is None or org.demo:
            raise ShowcaseError("forbidden", "this team cannot apply")
        await ratestore.sweep(db, APPLY_NS)
        if not await ratestore.rate_check(db, APPLY_NS, [(f"u:{user.id}", APPLY_MAX_PER_USER)], 86400):
            await db.commit()
            raise ShowcaseError("rate_limited", "too many applications today - try again tomorrow")
        if await _team_full(db, org_id):
            await db.commit()
            raise ShowcaseError("limit", LIMIT_DETAIL)
        app = ShowcaseApp(org_id=org_id, status="pending", pending=rev, submitted_by=user.id, **flags)
        db.add(app)
        await db.flush()
        _event(db, app, "applied", rules.changes({}, rev), actor=user.email, source="web")
        await db.commit()
        log.info("showcase applied app=%s team=%s", app.id, org_id)
        return await _view(db, app)


async def edit(user: User, app_id: int, raw: dict, expected_version: int) -> dict:
    """Propose a new revision. A live app stays live as it is until staff approve the change; a
    rejected application goes back to review. The show flags apply at once, unreviewed."""
    rev, flags = _clean(raw)
    async with session_maker() as db:
        app = await _owned_app(db, user, app_id)
        if app.version != expected_version:
            raise ShowcaseError("conflict", "this app changed - reload it before saving")
        base = app.pending or (live_of(app) if app.approved_at else {})
        changed = rules.changes(base, rev)
        shared = _set_flags(db, app, flags, actor=user.email, source="web")
        if not changed:
            if not shared:
                raise ShowcaseError("invalid", "nothing changed")
            await db.commit()
            return await _view(db, app)
        if app.status == "rejected":
            # Back in review, it counts towards the team's limit again.
            if await _team_full(db, app.org_id, besides=app.id):
                raise ShowcaseError("limit", LIMIT_DETAIL)
            app.status, app.reject_reason = "pending", ""
        app.pending = rev
        _event(db, app, "edited", changed, actor=user.email, source="web")
        await db.commit()
        return await _view(db, app)


async def withdraw(user: User, app_id: int) -> None:
    """Withdraw a proposal: an application never approved is removed; a live app keeps its live
    version and drops the pending change."""
    async with session_maker() as db:
        app = await _owned_app(db, user, app_id)
        if app.approved_at is None:
            for child in (ShowcaseAppEvent, ShowcaseAppKey, ShowcaseVote):
                await db.execute(delete(child).where(child.app_id == app.id))
            await db.delete(app)
        elif app.pending is not None:
            app.pending = None
            _event(db, app, "withdrawn", {}, actor=user.email, source="web")
        await db.commit()


def _usable(key: ApiKey) -> bool:
    return key.state != "revoked" and key.deleted_at is None


async def _taken(db: AsyncSession, keys: dict[int, ApiKey], org_id: int) -> dict[int, int]:
    """{key id: app id} for every key an app of the team uses, rotations included."""
    rows = (await db.execute(select(ShowcaseAppKey.api_key_id, ShowcaseAppKey.app_id)
                             .join(ShowcaseApp, ShowcaseApp.id == ShowcaseAppKey.app_id)
                             .where(ShowcaseApp.org_id == org_id))).all()
    return {k: app_id for linked, app_id in rows for k in _chain(keys, linked)}


async def team_keys(user: User, org_id: int) -> list[dict]:
    """The team's API keys an owner or admin may link to an app, and the app each one already
    counts for. Names and prefixes only: a key's secret is never stored."""
    async with session_maker() as db:
        if await team_role(db, user.id, org_id) not in ("owner", "admin"):
            raise ShowcaseError("forbidden", "owner or admin role in this team is required")
        keys = await _org_keys(db, [org_id])
        taken = await _taken(db, keys, org_id)
    return [{**_key_view(k), "app_id": taken.get(k.id)} for k in sorted(keys.values(), key=lambda k: k.id)
            if _usable(k)]


def _usage_label(source: str, key_ids: list[int]) -> str:
    return "team" if source == "team" else "keys:" + ",".join(str(k) for k in sorted(key_ids))


async def set_usage(user: User, app_id: int, source: str, key_ids: list[int]) -> dict:
    """Choose where an app's activity comes from: the whole team, or the given API keys of the team.
    Applied at once, not reviewed: it changes nothing anyone reads but the team's own numbers. The
    next activity run counts a newly linked key's last four weeks."""
    if source not in ("team", "keys"):
        raise ShowcaseError("invalid", "source: use team or keys")
    wanted = sorted(set(key_ids)) if source == "keys" else []
    if source == "keys" and not 1 <= len(wanted) <= MAX_KEYS_PER_APP:
        raise ShowcaseError("invalid", f"key_ids: pick 1 to {MAX_KEYS_PER_APP} API keys")
    async with session_maker() as db:
        app = await _owned_app(db, user, app_id)
        keys = await _org_keys(db, [app.org_id])
        for k in wanted:
            if k not in keys or not _usable(keys[k]):
                raise ShowcaseError("invalid", "key_ids: pick active API keys of this team")
        taken = await _taken(db, keys, app.org_id)
        if any(taken.get(k, app.id) != app.id for k in wanted):
            raise ShowcaseError("conflict", "that API key already counts for another app of this team")
        current = (await _links(db, [app.id]))[app.id]
        before, after = _usage_label(app.usage_source, current), _usage_label(source, wanted)
        if before == after:
            raise ShowcaseError("invalid", "nothing changed")
        await db.execute(delete(ShowcaseAppKey).where(ShowcaseAppKey.app_id == app.id))
        for k in wanted:
            db.add(ShowcaseAppKey(app_id=app.id, api_key_id=k))
        app.usage_source = source
        _event(db, app, "usage", {"usage": [before, after]}, actor=user.email, source="web")
        try:
            await db.commit()
        except IntegrityError:
            raise ShowcaseError("conflict", "that API key already counts for another app of this team") from None
        return await _view(db, app)


async def remove(user: User, app_id: int) -> dict:
    """The team takes its live app off the showcase at once. Only staff show it again."""
    async with session_maker() as db:
        app = await _owned_app(db, user, app_id)
        if app.status != "approved":
            raise ShowcaseError("invalid", "only a live app can be removed")
        app.status = "hidden"
        _event(db, app, "removed", {}, actor=user.email, source="web")
        await db.commit()
        return await _view(db, app)


async def queue(status: str | None = None) -> list[dict]:
    """Everything staff may act on, newest change first; `status=review` is what waits for them."""
    async with session_maker() as db:
        stmt = select(ShowcaseApp, Org).join(Org, Org.id == ShowcaseApp.org_id)
        if status == "review":
            stmt = stmt.where(ShowcaseApp.pending.is_not(None), ShowcaseApp.status != "rejected")
        elif status:
            stmt = stmt.where(ShowcaseApp.status == status)
        rows = (await db.execute(stmt.order_by(ShowcaseApp.updated_at.desc()))).all()
        apps = [a for a, _ in rows]
        since = await _pending_since(db, apps)
        views = await _views(db, apps, orgs={o.id: o for _, o in rows})
        return [{**v, "pending_since": since.get(v["id"])} for v in views]


async def _pending_since(db: AsyncSession, apps) -> dict[int, str]:
    """When each app's waiting proposal was last sent: its pictures expire from staging about 30
    days after that, so the queue can warn before an approval would find them gone."""
    ids = [a.id for a in apps if a.pending is not None]
    if not ids:
        return {}
    rows = (await db.execute(select(ShowcaseAppEvent.app_id, func.max(ShowcaseAppEvent.created_at))
                             .where(ShowcaseAppEvent.app_id.in_(ids),
                                    ShowcaseAppEvent.action.in_(("applied", "edited")))
                             .group_by(ShowcaseAppEvent.app_id))).all()
    return {app_id: at.isoformat() for app_id, at in rows}


async def staff_get(app_id: int) -> dict:
    async with session_maker() as db:
        app = await db.get(ShowcaseApp, app_id)
        if app is None:
            raise ShowcaseError("not_found")
        org = await db.get(Org, app.org_id)
        events = (await db.execute(select(ShowcaseAppEvent).where(ShowcaseAppEvent.app_id == app_id)
                                   .order_by(ShowcaseAppEvent.version.desc()))).scalars().all()
        since = await _pending_since(db, [app])
        return {**await _view(db, app, orgs={org.id: org}), "pending_since": since.get(app.id), "history": [
            {"version": e.version, "action": e.action, "changes": e.changes, "note": e.note,
             "actor": e.actor, "source": e.source, "created_at": e.created_at.isoformat()} for e in events]}


def _live_keys(app: ShowcaseApp) -> set[str]:
    return {k for k in (app.icon_key, *app.shot_keys) if k} if app.approved_at else set()


async def _publish_revision(rev: dict, *, already: set[str] = frozenset()) -> None:
    """Publish the revision's pictures, except those already live on this entry."""
    for key in ([rev["icon_key"]] if rev["icon_key"] else []) + list(rev["shot_keys"]):
        if key in already:
            continue
        try:
            await images.publish(key)
        except ImageRejected as exc:
            raise ShowcaseError("invalid", exc.detail) from None


async def _versioned(db: AsyncSession, app_id: int, expected_version: int) -> ShowcaseApp:
    app = await db.get(ShowcaseApp, app_id)
    if app is None:
        raise ShowcaseError("not_found")
    if app.version != expected_version:
        raise ShowcaseError("conflict", "this app changed - reload it before acting")
    return app


async def approve(app_id: int, expected_version: int, *, actor: str, source: str = "admin") -> dict:
    """Make the pending revision live: publish its pictures, then point the row at them."""
    async with session_maker() as db:
        app = await _versioned(db, app_id, expected_version)
        if app.pending is None:
            raise ShowcaseError("invalid", "nothing is waiting for review")
        if app.status == "rejected" and await _team_full(db, app.org_id, besides=app.id):
            raise ShowcaseError("limit", LIMIT_DETAIL)
        rev, live_keys = dict(app.pending), _live_keys(app)
    await _publish_revision(rev, already=live_keys)  # storage I/O with no session open
    async with session_maker() as db:
        app = await _versioned(db, app_id, expected_version)
        before = live_of(app) if app.approved_at else {}
        old_keys = [app.icon_key, *app.shot_keys] if app.approved_at else []
        _set_live(app, rev)
        app.pending, app.reject_reason = None, ""
        app.status = "approved" if app.status in ("pending", "rejected") else app.status
        app.approved_at = app.approved_at or utcnow_naive()
        _event(db, app, "approved", rules.changes(before, rev), actor=actor, source=source)
        await db.commit()
        view = await _view(db, app)
    await images.retire([k for k in old_keys if k not in ([rev["icon_key"], *rev["shot_keys"]])])
    return view


async def reject(app_id: int, expected_version: int, reason: str, *, actor: str, source: str = "admin") -> dict:
    """Refuse the pending revision with a reason the maker sees. A live app stays live."""
    reason = " ".join((reason or "").split())
    if not 1 <= len(reason) <= 500:
        raise ShowcaseError("invalid", "give a reason (1-500 characters)")
    async with session_maker() as db:
        app = await _versioned(db, app_id, expected_version)
        if app.pending is None:
            raise ShowcaseError("invalid", "nothing is waiting for review")
        app.reject_reason = reason
        if app.approved_at is None:
            app.status = "rejected"            # the maker may edit and resubmit
        else:
            app.pending = None                 # the live version stays
        _event(db, app, "rejected", {}, actor=actor, source=source, note=reason)
        await db.commit()
        return await _view(db, app)


async def set_hidden(app_id: int, expected_version: int, hidden: bool, *, actor: str, source: str = "admin") -> dict:
    async with session_maker() as db:
        app = await _versioned(db, app_id, expected_version)
        if app.approved_at is None:
            raise ShowcaseError("invalid", "only an approved app can be hidden or shown")
        target = "hidden" if hidden else "approved"
        if app.status == target:
            raise ShowcaseError("invalid", f"already {target}")
        before, app.status = app.status, target
        _event(db, app, "hidden" if hidden else "unhidden", {"status": [before, target]},
               actor=actor, source=source)
        await db.commit()
        return await _view(db, app)


async def staff_edit(app_id: int, expected_version: int, raw: dict, *, actor: str, source: str = "admin") -> dict:
    """Staff correct an entry. A pending revision is edited in place (it still needs approval); a
    live app without one is changed directly, its pictures published first. Show flags apply at once."""
    rev, flags = _clean(raw)
    async with session_maker() as db:
        app = await _versioned(db, app_id, expected_version)
        direct = app.pending is None and app.approved_at is not None
        live_keys = _live_keys(app)
    if direct:
        await _publish_revision(rev, already=live_keys)
    async with session_maker() as db:
        app = await _versioned(db, app_id, expected_version)
        old_keys: list = []
        if direct:
            before, old_keys = live_of(app), [app.icon_key, *app.shot_keys]
            _set_live(app, rev)
        else:
            before = app.pending or {}
            app.pending = rev
        changed = rules.changes(before, rev)
        shared = _set_flags(db, app, flags, actor=actor, source=source)
        if not changed and not shared:
            raise ShowcaseError("invalid", "nothing changed")
        if changed:
            _event(db, app, "edited", changed, actor=actor, source=source)
        await db.commit()
        view = await _view(db, app)
    if old_keys:
        await images.retire([k for k in old_keys if k not in ([rev["icon_key"], *rev["shot_keys"]])])
    return view


async def staff_create(org_id: int, raw: dict, *, approve_now: bool, actor: str, source: str = "admin") -> dict:
    """Staff list an app for a team, pending or approved at once, with the show flags they set.
    The team limit still applies."""
    rev, flags = _clean(raw)
    async with session_maker() as db:
        if await db.get(Org, org_id) is None:
            raise ShowcaseError("not_found", "no such team")
        if await _team_full(db, org_id):
            raise ShowcaseError("limit", LIMIT_DETAIL)
        app = ShowcaseApp(org_id=org_id, status="pending", pending=rev, **flags)
        db.add(app)
        await db.flush()
        _event(db, app, "applied", rules.changes({}, rev), actor=actor, source=source)
        await db.commit()
        app_id, version = app.id, app.version
    if approve_now:
        return await approve(app_id, version, actor=actor, source=source)
    return await staff_get(app_id)


# Activity is counted per source: a team's calls (`api_key_id` TEAM) for an app that shows its
# team's activity, or one API key's calls for an app linked to keys. Each source an approved app uses
# is counted once, however many apps read it. Counted a little at a time: each run reads only the
# source's calls after its cursor, over the (org_id, id) index for a team or (org_id, api_key_id, id)
# for a key, in batches of BATCH_ROWS, each its own short transaction holding the cursor row locked,
# so overlapping runs never count a call twice. Ids are given as rows are inserted, so a call that is
# written late still lands after the cursor. Calls younger than LAG wait for the next run: a lower
# id still being committed by another writer is then already visible. Sources take turns, a batch
# each, until MAX_ROWS calls are read, so one busy source cannot hold the others back; a busy
# source's first weeks are counted over several runs. A cursor left behind the window jumps to it.

BATCH_ROWS = 5_000
MAX_ROWS = 200_000
LAG = timedelta(minutes=5)

Source = tuple[int, int]   # (org_id, api_key_id or TEAM)


def _categories_by_endpoint() -> dict[str, str]:
    return {e["id"]: (e.get("domain") or "other") for e in catalog_store.load().endpoints}


async def _window_floor(db: AsyncSession, org_id: int, since: date) -> int:
    """Just before the team's first call of the window, found on the (org_id, created_at) index;
    past its newest call when there is none. A key's calls are the team's, so it starts there too."""
    begin = datetime(since.year, since.month, since.day)
    first = (await db.execute(select(CallRecord.id).where(
        CallRecord.org_id == org_id, CallRecord.created_at >= begin)
        .order_by(CallRecord.created_at).limit(1))).scalar_one_or_none()
    if first is not None:
        return first - 1
    newest = (await db.execute(select(func.max(CallRecord.id)).where(CallRecord.org_id == org_id))).scalar_one()
    return newest or 0


async def _locked_cursor(db: AsyncSession, source: Source) -> ShowcaseCursor:
    """The source's cursor row, created if missing and locked for this batch. Created first and then
    locked, so two runs that both find no cursor cannot both insert one."""
    org_id, key_id = source
    await db.execute(_insert(db)(ShowcaseCursor).values(org_id=org_id, api_key_id=key_id, through_id=-1,
                                                        updated_at=utcnow_naive())
                     .on_conflict_do_nothing(index_elements=[ShowcaseCursor.org_id, ShowcaseCursor.api_key_id]))
    return (await db.execute(select(ShowcaseCursor).where(ShowcaseCursor.org_id == org_id,
                                                          ShowcaseCursor.api_key_id == key_id)
                             .with_for_update().execution_options(populate_existing=True))).scalar_one()


async def _fold_batch(db: AsyncSession, source: Source, *, since: date, young: datetime, limit: int,
                      domains: dict[str, str], first: bool) -> tuple[int, bool]:
    """Add the source's next `limit` calls after its cursor to its week rows and move the cursor, in
    the caller's transaction. Returns (calls read, caught up)."""
    org_id, key_id = source
    cursor = await _locked_cursor(db, source)
    if first or cursor.through_id < 0:   # a new cursor never reads history before the window
        cursor.through_id = max(cursor.through_id, await _window_floor(db, org_id, since))
    which = [CallRecord.org_id == org_id, CallRecord.id > cursor.through_id]
    if key_id != TEAM:
        which.append(CallRecord.api_key_id == key_id)
    rows = (await db.execute(
        select(CallRecord.id, CallRecord.created_at, CallRecord.provider, CallRecord.endpoint_id,
               CallRecord.refused_by)
        .where(*which).order_by(CallRecord.id).limit(limit))).all()
    counted, through, read, waiting = [], cursor.through_id, 0, False
    for call_id, at, provider, endpoint_id, refused_by in rows:
        if at >= young:
            waiting = True
            break
        through, read = call_id, read + 1
        # Refused calls never reached a provider, so they are not activity.
        if refused_by is None and at.date() >= since:
            counted.append((at, provider, endpoint_id))
    for start, delta in rules.fold_calls(counted, lambda e: domains.get(e, "other")).items():
        key = start.isoformat()
        week = await db.get(ShowcaseWeek, (org_id, key_id, key))
        if week is None:
            week = ShowcaseWeek(org_id=org_id, api_key_id=key_id, week_start=key)
            db.add(week)
        merged = rules.add_to_week({"calls": week.calls, "active_days": week.active_days,
                                    "providers": week.providers, "categories": week.categories}, delta)
        week.calls, week.active_days = merged["calls"], merged["active_days"]
        week.providers, week.categories = merged["providers"], merged["categories"]
        week.computed_at = utcnow_naive()
    cursor.through_id, cursor.updated_at = through, utcnow_naive()
    return read, waiting or len(rows) < limit


async def _approved_sources(db: AsyncSession) -> tuple[list[ShowcaseApp], dict[int, list[Source]]]:
    apps = (await db.execute(select(ShowcaseApp).where(ShowcaseApp.status == "approved")
                             .order_by(ShowcaseApp.id))).scalars().all()
    return list(apps), await _sources(db, apps)


async def refresh_activity(session_factory: Callable = session_maker, *, now: datetime | None = None,
                           max_rows: int = MAX_ROWS, batch_rows: int = BATCH_ROWS) -> dict:
    """One scheduled pass: add the new calls of every source an approved app uses to its week rows,
    then drop rows older than KEEP_WEEKS and those of sources no approved app uses. Returns
    `{sources, calls, caught_up, newest_week}`."""
    now = now or utcnow_naive()
    since = rules.window_start(now.date())
    young = now - LAG
    domains = _categories_by_endpoint()
    async with session_factory() as db:
        _, by_app = await _approved_sources(db)
    sources = sorted({src for srcs in by_app.values() for src in srcs})
    read, waiting, started = 0, list(sources), set()
    while waiting and read < max_rows:
        for source in list(waiting):
            if read >= max_rows:
                break
            async with session_factory() as db:
                n, done = await _fold_batch(db, source, since=since, young=young, domains=domains,
                                            limit=min(batch_rows, max_rows - read), first=source not in started)
                await db.commit()
            started.add(source)
            read += n
            if done:
                waiting.remove(source)
    async with session_factory() as db:
        await db.execute(delete(ShowcaseWeek).where(ShowcaseWeek.week_start < rules.keep_from(now.date()).isoformat()))
        await db.commit()
        # What is wanted now, not at the start of the run: an app approved meanwhile keeps its rows.
        _, current = await _approved_sources(db)
        wanted = {src for srcs in current.values() for src in srcs}
        stored = set((await db.execute(select(ShowcaseCursor.org_id, ShowcaseCursor.api_key_id))).all())
        stored |= set((await db.execute(select(ShowcaseWeek.org_id, ShowcaseWeek.api_key_id).distinct())).all())
        await db.commit()
    for org_id, key_id in sorted(stored - wanted):
        # A source's cursor and rows go together, under the cursor lock, so a run counting it waits.
        async with session_factory() as db:
            await _locked_cursor(db, (org_id, key_id))
            for model in (ShowcaseWeek, ShowcaseCursor):
                await db.execute(delete(model).where(model.org_id == org_id, model.api_key_id == key_id))
            await db.commit()
    log.info("showcase activity: %d sources, %d calls read, caught up %s", len(sources), read, not waiting)
    return {"sources": len(sources), "calls": read, "caught_up": not waiting,
            "newest_week": rules.last_complete_week(now.date()).isoformat()}


async def _upvotes(db: AsyncSession, app_id: int | None = None) -> dict[int, int]:
    query = select(ShowcaseVote.app_id, func.count()).group_by(ShowcaseVote.app_id)
    if app_id is not None:
        query = query.where(ShowcaseVote.app_id == app_id)
    return {app: int(n) for app, n in (await db.execute(query)).all()}


def _public_app(app: ShowcaseApp) -> dict:
    return {
        "id": app.id, "name": app.name, "url": app.url, "tagline": app.tagline,
        "description": app.description, "category": app.category,
        "category_label": rules.CATEGORIES.get(app.category, "Other"),
        "maker": {"name": app.maker_name, "x_handle": app.x_handle} if (app.maker_name or app.x_handle) else None,
        "launch_post_url": app.launch_post_url or None,
        "icon_url": images.public_url(app.icon_key),
        "shots": [_shot_urls(k) for k in app.shot_keys],
    }


async def leaderboard(db: AsyncSession) -> dict:
    """The public leaderboard: approved apps only, activity through `rules.public_entry`."""
    apps, by_app = await _approved_sources(db)
    orgs = sorted({a.org_id for a in apps})
    weeks = (await db.execute(select(ShowcaseWeek).where(ShowcaseWeek.org_id.in_(orgs)))).scalars().all() if orgs else []
    upvotes = await _upvotes(db)
    ref = rules.last_complete_week(utcnow_naive().date())   # the week still running never counts
    by_source: dict[Source, list[rules.Week]] = {}
    for w in weeks:
        by_source.setdefault((w.org_id, w.api_key_id), []).append(rules.Week(
            week_start=date.fromisoformat(w.week_start), calls=w.calls, active_days=w.active_days,
            providers=frozenset(w.providers or []), categories=dict(w.categories or {})))
    folded: dict[int, tuple[rules.Activity, rules.Consent, int]] = {}
    entries = []
    for a in apps:
        consent = rules.Consent(calls=a.show_calls, streak=a.show_streak,
                                providers=a.show_providers, categories=a.show_categories)
        own = rules.merge_weeks(w for src in by_app[a.id] for w in by_source.get(src, []))
        folded[a.id] = (rules.activity(own, ref), consent, upvotes.get(a.id, 0))
        entries.append({**_public_app(a), **rules.public_entry(*folded[a.id])})
    return {"week": ref.isoformat(), "apps": entries, "boards": rules.boards(folded)}


def may_vote(user: User) -> bool:
    """A real person with a proven email: not an agent or demo identity, not suspended."""
    return bool(user.email_verified_at) and not user.demo and not user.suspended \
        and not is_machine_email(user.email)


async def my_upvotes(db: AsyncSession, user: User) -> list[int]:
    return list((await db.execute(
        select(ShowcaseVote.app_id).where(ShowcaseVote.user_id == user.id)
        .order_by(ShowcaseVote.app_id))).scalars().all())


async def vote(db: AsyncSession, user: User, app_id: int, up: bool, client_ip: str) -> dict:
    """Upvote an approved app, or take this person's upvote back. Commits."""
    if not may_vote(user):
        raise ShowcaseError("not_allowed")
    app = await db.get(ShowcaseApp, app_id)
    if app is None or app.status != "approved":
        raise ShowcaseError("not_found")
    await ratestore.sweep(db, VOTE_NS)
    if not await ratestore.rate_check(
            db, VOTE_NS, [(f"u:{user.id}", VOTE_MAX_PER_USER),
                          *ratestore.ip_limit(client_ip, VOTE_MAX_PER_IP, key=f"i:{client_ip}")],
            VOTE_WINDOW_S):
        await db.commit()
        raise ShowcaseError("rate_limited")
    if up:
        ins = _insert(db)(ShowcaseVote).values(app_id=app_id, user_id=user.id, created_at=utcnow_naive())
        await db.execute(ins.on_conflict_do_nothing(index_elements=[ShowcaseVote.app_id, ShowcaseVote.user_id]))
    else:
        await db.execute(delete(ShowcaseVote).where(ShowcaseVote.app_id == app_id, ShowcaseVote.user_id == user.id))
    await db.commit()
    return {"id": app_id, "up": up, "upvotes": (await _upvotes(db, app_id)).get(app_id, 0)}
