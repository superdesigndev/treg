"""Managed API-key administration HTTP routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..domain.governance import teams
from ..domain.identity import api_keys as managed
from ..domain.identity import session as identity_session
from ..domain.identity.access import Caller, _role_at_least, require_member
from ..infra.db import get_session
from ..models import ApiKey, ApiKeyEvent, Membership, Org, User
from ..timeutil import utcnow_naive


router = APIRouter()


class KeyNameIn(BaseModel):
    name: str


def _name(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise HTTPException(status_code=422, detail="key name is required")
    if len(value) > 80:
        raise HTTPException(status_code=422, detail="key name must be 80 characters or fewer")
    return value


def _admin(caller: Caller) -> bool:
    return _role_at_least(caller.role, "admin")


def _assigned(caller: Caller, key: ApiKey) -> bool:
    return key.membership_id == caller.membership.id


async def _key(org_id: int, key_id: int, db: AsyncSession, caller: Caller) -> ApiKey:
    if caller.org_id != org_id:
        raise HTTPException(status_code=403, detail="use this team's credential")
    row = await db.get(ApiKey, key_id)
    if row is None or row.org_id != org_id:
        raise HTTPException(status_code=404, detail="key not found")
    return row


def _agent_display_name(identity: str, org_slug: str) -> str:
    local = identity.split("@", 1)[0]
    prefix = f"agent-{org_slug}-"
    return local[len(prefix):] if local.startswith(prefix) else local


def _view(
    key: ApiKey,
    caller: Caller,
    user_id: int | None = None,
    assigned_name: str | None = None,
    safe_prefix: str | None = None,
) -> dict:
    own = _assigned(caller, key)
    admin = _admin(caller)
    agent_admin = admin and key.kind == managed.AGENT_KIND
    return {
        "id": key.id,
        "membership_id": key.membership_id,
        "user_id": user_id,
        "identity": key.identity_label,
        "assigned_name": assigned_name or key.identity_label,
        "assigned_type": "agent" if key.kind == managed.AGENT_KIND else "human",
        "kind": key.kind,
        "name": key.name,
        "safe_prefix": safe_prefix if safe_prefix is not None else key.safe_prefix,
        "state": key.state,
        "created_by": key.created_by,
        "created_at": key.created_at.isoformat() if key.created_at else None,
        "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
        "disabled_at": key.disabled_at.isoformat() if key.disabled_at else None,
        "revoked_at": key.revoked_at.isoformat() if key.revoked_at else None,
        "deleted_at": key.deleted_at.isoformat() if key.deleted_at else None,
        "replacement_key_id": key.replacement_key_id,
        "can_rename": own or agent_admin,
        "can_disable": admin and key.state == managed.ACTIVE,
        "can_enable": admin and key.state == managed.DISABLED,
        "can_revoke": key.kind != managed.DEFAULT_KIND and admin and key.state != managed.REVOKED,
        "can_rotate": (
            (key.kind == managed.DEFAULT_KIND and own and key.state == managed.ACTIVE)
            or (key.kind == managed.ADDITIONAL_KIND and own and key.state != managed.REVOKED)
            or (key.kind == managed.AGENT_KIND and admin and key.state != managed.REVOKED)
        ),
        "can_hide": (own or agent_admin) and key.deleted_at is None,
    }


async def _ensure_team_records(org_id: int, db: AsyncSession) -> None:
    """Cover rows created by an old binary during a rolling deploy or by direct test setup."""
    memberships = (await db.execute(select(Membership).where(
        Membership.org_id == org_id,
    ))).scalars().all()
    users = {u.id: u for u in (await db.execute(select(User).where(
        User.id.in_([m.user_id for m in memberships]),
    ))).scalars().all()} if memberships else {}
    known_hashes = set((await db.execute(select(ApiKey.key_hash).where(
        ApiKey.org_id == org_id, ApiKey.key_hash.is_not(None),
    ))).scalars().all())
    for membership in memberships:
        user = users.get(membership.user_id)
        if user is None:
            continue
        await managed.ensure_default_key(db, membership, user)
        if membership.token_hash and membership.token_hash not in known_hashes:
            agent = managed.is_agent_email(user.email)
            db.add(ApiKey(
                org_id=org_id,
                membership_id=membership.id,
                identity_label=user.email,
                kind=managed.AGENT_KIND if agent else managed.LEGACY_KIND,
                name="Agent key" if agent else "Legacy key",
                key_hash=membership.token_hash,
                state=managed.ACTIVE,
                created_by=membership.created_by or user.email,
                created_at=membership.created_at,
            ))
            known_hashes.add(membership.token_hash)
    await db.flush()


@router.get("/orgs/{org_id}/api-keys")
async def list_api_keys(
    org_id: int,
    include_hidden: bool = Query(default=False),
    caller: Caller = Depends(require_member),
    db: AsyncSession = Depends(get_session),
) -> list[dict]:
    if caller.org_id != org_id:
        raise HTTPException(status_code=403, detail="use this team's credential")
    await _ensure_team_records(org_id, db)
    query = select(ApiKey).where(ApiKey.org_id == org_id)
    if not _admin(caller):
        query = query.where(ApiKey.membership_id == caller.membership.id)
    if not include_hidden:
        query = query.where(ApiKey.deleted_at.is_(None))
    rows = (await db.execute(query.order_by(ApiKey.identity_label, ApiKey.id))).scalars().all()
    membership_ids = [row.membership_id for row in rows if row.membership_id is not None]
    memberships = {
        row.id: row for row in (await db.execute(select(Membership).where(
            Membership.id.in_(membership_ids),
        ))).scalars().all()
    } if membership_ids else {}
    user_ids = [membership.user_id for membership in memberships.values()]
    users = {
        row.id: row for row in (await db.execute(select(User).where(
            User.id.in_(user_ids),
        ))).scalars().all()
    } if user_ids else {}
    org = await db.get(Org, org_id)
    await db.commit()
    views = []
    for row in rows:
        membership = memberships.get(row.membership_id)
        user = users.get(membership.user_id) if membership else None
        assigned_name = row.identity_label
        if row.kind == managed.AGENT_KIND and org is not None:
            assigned_name = _agent_display_name(row.identity_label, org.slug)
        safe_prefix = row.safe_prefix
        if row.kind == managed.DEFAULT_KIND and user is not None and org is not None:
            safe_prefix = identity_session.make_identity(
                user.id, user.token_version, org=org.slug,
                key_generation=row.default_generation,
                scope=identity_session.TEAM_SCOPE,
            )[:12]
        views.append(_view(
            row,
            caller,
            membership.user_id if membership else None,
            assigned_name,
            safe_prefix,
        ))
    return views


@router.post("/orgs/{org_id}/api-keys")
async def create_api_key(
    org_id: int,
    body: KeyNameIn,
    response: Response,
    caller: Caller = Depends(require_member),
    db: AsyncSession = Depends(get_session),
) -> dict:
    if caller.org_id != org_id:
        raise HTTPException(status_code=403, detail="use this team's credential")
    if caller.role == "viewer":
        raise HTTPException(status_code=403, detail="viewers cannot create additional keys")
    if managed.is_machine_email(caller.email):
        raise HTTPException(status_code=403, detail="machine identities cannot create human keys")
    secret, prefix, key_hash = managed.mint_secret()
    row = ApiKey(
        org_id=org_id,
        membership_id=caller.membership.id,
        identity_label=caller.email,
        kind=managed.ADDITIONAL_KIND,
        name=_name(body.name),
        safe_prefix=prefix,
        key_hash=key_hash,
        state=managed.ACTIVE,
        created_by=caller.email,
    )
    db.add(row)
    await db.flush()
    managed.audit_event(db, row, caller.email, "created")
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return {**_view(row, caller, caller.user.id), "secret": secret,
            "note": "copy this key now; treg stores only its hash"}


@router.patch("/orgs/{org_id}/api-keys/{key_id}")
async def rename_api_key(
    org_id: int,
    key_id: int,
    body: KeyNameIn,
    caller: Caller = Depends(require_member),
    db: AsyncSession = Depends(get_session),
) -> dict:
    row = await _key(org_id, key_id, db, caller)
    if not (_assigned(caller, row) or (_admin(caller) and row.kind == managed.AGENT_KIND)):
        raise HTTPException(status_code=403, detail="you cannot rename this key")
    row.name = _name(body.name)
    managed.audit_event(db, row, caller.email, "renamed")
    await db.commit()
    return _view(row, caller)


async def _set_state(
    *, org_id: int, key_id: int, target: str, caller: Caller, db: AsyncSession,
) -> dict:
    if not _admin(caller):
        raise HTTPException(status_code=403, detail="admin role in this org is required")
    row = await _key(org_id, key_id, db, caller)
    now = utcnow_naive()
    if target == managed.DISABLED:
        if row.state != managed.ACTIVE:
            raise HTTPException(status_code=409, detail="only an active key can be disabled")
        row.state = target
        row.disabled_at = now
        action = "default_key_disabled" if row.kind == managed.DEFAULT_KIND else "disabled"
    elif target == managed.ACTIVE:
        if row.state != managed.DISABLED:
            raise HTTPException(status_code=409, detail="only a disabled key can be enabled")
        row.state = target
        row.disabled_at = None
        action = "default_key_enabled" if row.kind == managed.DEFAULT_KIND else "enabled"
    else:
        if row.kind == managed.DEFAULT_KIND:
            raise HTTPException(
                status_code=409,
                detail="the Default key cannot be revoked; disable or rotate it",
            )
        if row.state == managed.REVOKED:
            raise HTTPException(status_code=409, detail="key is already revoked")
        membership = await db.get(Membership, row.membership_id) if row.membership_id else None
        if row.kind == managed.AGENT_KIND and membership is not None:
            user = await db.get(User, membership.user_id)
            await teams.delete_membership(db, membership, actor_email=caller.email)
            await db.flush()
            if user is not None and (await db.execute(select(Membership).where(
                Membership.user_id == user.id,
            ))).scalars().first() is None:
                await db.delete(user)
            await db.commit()
            return {"id": row.id, "state": managed.REVOKED, "agent_revoked": True}
        row.state = managed.REVOKED
        row.revoked_at = now
        action = "default_key_revoked" if row.kind == managed.DEFAULT_KIND else "revoked"
    managed.audit_event(db, row, caller.email, action)
    await db.commit()
    return _view(row, caller)


@router.post("/orgs/{org_id}/api-keys/{key_id}/disable")
async def disable_api_key(
    org_id: int, key_id: int, caller: Caller = Depends(require_member),
    db: AsyncSession = Depends(get_session),
) -> dict:
    return await _set_state(
        org_id=org_id, key_id=key_id, target=managed.DISABLED, caller=caller, db=db,
    )


@router.post("/orgs/{org_id}/api-keys/{key_id}/enable")
async def enable_api_key(
    org_id: int, key_id: int, caller: Caller = Depends(require_member),
    db: AsyncSession = Depends(get_session),
) -> dict:
    return await _set_state(
        org_id=org_id, key_id=key_id, target=managed.ACTIVE, caller=caller, db=db,
    )


@router.post("/orgs/{org_id}/api-keys/{key_id}/revoke")
async def revoke_api_key(
    org_id: int, key_id: int, caller: Caller = Depends(require_member),
    db: AsyncSession = Depends(get_session),
) -> dict:
    return await _set_state(
        org_id=org_id, key_id=key_id, target=managed.REVOKED, caller=caller, db=db,
    )


@router.post("/orgs/{org_id}/api-keys/{key_id}/rotate")
async def rotate_api_key(
    org_id: int, key_id: int, response: Response,
    caller: Caller = Depends(require_member),
    db: AsyncSession = Depends(get_session),
) -> dict:
    old = await _key(org_id, key_id, db, caller)
    if old.kind == managed.DEFAULT_KIND:
        if not _assigned(caller, old):
            raise HTTPException(status_code=403, detail="only the assigned human can rotate this key")
        if old.state != managed.ACTIVE:
            raise HTTPException(status_code=409, detail="only an active Default key can be rotated")
        membership = await db.get(Membership, old.membership_id) if old.membership_id else None
        user = await db.get(User, membership.user_id) if membership else None
        org = await db.get(Org, org_id)
        if membership is None or user is None or org is None:
            raise HTTPException(status_code=409, detail="membership removed or suspended")
        try:
            generation = await managed.rotate_default_generation(
                db, old, actor_email=caller.email,
            )
            await db.commit()
        except (managed.RotationConflict, IntegrityError):
            await db.rollback()
            raise HTTPException(status_code=409, detail="another rotation already replaced this key")
        secret = identity_session.make_identity(
            user.id, user.token_version, org=org.slug,
            key_generation=generation,
            scope=identity_session.TEAM_SCOPE,
        )
        response.headers["Cache-Control"] = "no-store"
        return {**_view(old, caller, user.id, safe_prefix=secret[:12]), "secret": secret,
                "note": "this team's previous Default token stopped working"}
    if old.kind == managed.AGENT_KIND:
        if not _admin(caller):
            raise HTTPException(status_code=403, detail="admin role in this org is required")
        if old.state == managed.REVOKED:
            raise HTTPException(status_code=409, detail="a revoked key cannot be rotated")
        membership = await db.get(Membership, old.membership_id) if old.membership_id else None
        user = await db.get(User, membership.user_id) if membership else None
        if membership is None or user is None:
            raise HTTPException(status_code=409, detail="membership removed or suspended")
        current = await managed.active_agent_key(db, membership.id)
        if current is None or current.id != old.id:
            raise HTTPException(status_code=409, detail="only the current agent key can be rotated")
        try:
            secret, new = await managed.rotate_agent_key(
                db, membership, user, actor_email=caller.email, expected_key_id=old.id,
            )
        except (managed.RotationConflict, IntegrityError):
            await db.rollback()
            raise HTTPException(status_code=409, detail="another rotation already replaced this key")
    elif old.kind == managed.ADDITIONAL_KIND:
        if not _assigned(caller, old):
            raise HTTPException(status_code=403, detail="only the assigned human can rotate this key")
        if old.state == managed.REVOKED:
            raise HTTPException(status_code=409, detail="a revoked key cannot be rotated")
        try:
            secret, new = await managed.rotate_human_key(
                db, old, actor_email=caller.email,
            )
        except (managed.RotationConflict, IntegrityError):
            await db.rollback()
            raise HTTPException(status_code=409, detail="another rotation already replaced this key")
    else:
        raise HTTPException(status_code=409, detail="legacy human keys cannot be rotated here")
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="another rotation already replaced this key")
    response.headers["Cache-Control"] = "no-store"
    return {**_view(new, caller), "secret": secret,
            "note": "copy this key now; the old key was revoked"}


@router.post("/orgs/{org_id}/api-keys/{key_id}/hide")
async def hide_api_key(
    org_id: int, key_id: int, caller: Caller = Depends(require_member),
    db: AsyncSession = Depends(get_session),
) -> dict:
    row = await _key(org_id, key_id, db, caller)
    if not (_assigned(caller, row) or (_admin(caller) and row.kind == managed.AGENT_KIND)):
        raise HTTPException(status_code=403, detail="you cannot hide this key")
    if row.state != managed.REVOKED:
        raise HTTPException(status_code=409, detail="only a revoked key can be hidden")
    if row.deleted_at is None:
        row.deleted_at = utcnow_naive()
        managed.audit_event(db, row, caller.email, "hidden")
    await db.commit()
    return {"id": row.id, "hidden": True}


@router.get("/orgs/{org_id}/api-keys/{key_id}/events")
async def list_api_key_events(
    org_id: int, key_id: int, caller: Caller = Depends(require_member),
    db: AsyncSession = Depends(get_session),
) -> list[dict]:
    row = await _key(org_id, key_id, db, caller)
    if not (_assigned(caller, row) or _admin(caller)):
        raise HTTPException(status_code=403, detail="you cannot view this key's audit trail")
    events = (await db.execute(select(ApiKeyEvent).where(
        ApiKeyEvent.org_id == org_id, ApiKeyEvent.key_id == key_id,
    ).order_by(ApiKeyEvent.id.desc()))).scalars().all()
    return [{
        "id": event.id,
        "action": event.action,
        "actor": event.actor_email,
        "identity": event.identity_label,
        "created_at": event.created_at.isoformat() if event.created_at else None,
    } for event in events]
