"""Team-scoped enterprise sign-in and directory lifecycle orchestration."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlmodel import delete, select

from ..domain.governance import teams
from ..domain.identity.access import _is_machine_email, _norm_email
from ..infra.db import session_maker
from ..models import (EnterpriseDirectoryUser, EnterpriseSSO, EnterpriseSSOAudit, EnterpriseSSOBlock, EnterpriseSSOEvent,
                      EnterpriseSSOIdentity, Membership, Org, User)
from . import signup


class EnterpriseSSOError(Exception):
    pass


# A failed invoice can be retried without silently weakening the customer's
# existing access policy. Stripe will later move an unrecovered subscription out
# of this set according to the configured retry policy.
PAID_STATUSES = frozenset({"active", "trialing", "past_due"})


@dataclass(frozen=True)
class SigninResult:
    user_id: int
    token_version: int
    org_id: int
    org_slug: str


def _audit(db, org_id: int, event: str, *, actor: int | None = None,
           subject: int | None = None, from_role: str | None = None,
           to_role: str | None = None) -> None:
    db.add(EnterpriseSSOAudit(org_id=org_id, event=event,
        actor_user_id=actor, subject_user_id=subject,
        from_role=from_role, to_role=to_role))


async def record_action(org_id: int, event: str, *, actor: int | None = None) -> None:
    if event not in {"sso.setup_link_generated", "sso.it_contact_invited", "sso.signin_failed"}:
        raise ValueError("invalid enterprise SSO audit action")
    async with session_maker() as db:
        if await db.get(EnterpriseSSO, org_id) is None:
            return
        _audit(db, org_id, event, actor=actor)
        await db.commit()


async def record_role_change(db, org_id: int, *, actor: int, subject: int,
                             from_role: str, to_role: str) -> None:
    """Join the existing membership transaction; one writer owns the audit table."""
    if from_role != to_role and await db.get(EnterpriseSSO, org_id) is not None:
        _audit(db, org_id, "sso.role_changed", actor=actor, subject=subject,
               from_role=from_role, to_role=to_role)


async def configured_team(org_id: int) -> tuple[Org, EnterpriseSSO]:
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        config = await db.get(EnterpriseSSO, org_id)
        if org is None or config is None or config.subscription_status not in PAID_STATUSES:
            raise EnterpriseSSOError("SSO is not active for this team")
        return org, config


async def bind_workos_org(org_id: int, workos_org_id: str) -> None:
    async with session_maker() as db:
        config = await db.get(EnterpriseSSO, org_id)
        if config is None or config.subscription_status not in PAID_STATUSES:
            raise EnterpriseSSOError("SSO is not active for this team")
        if config.workos_org_id and config.workos_org_id != workos_org_id:
            raise EnterpriseSSOError("team already linked to another SSO organization")
        config.workos_org_id = workos_org_id
        db.add(config)
        _audit(db, org_id, "sso.workos_org_bound")
        await db.commit()


async def finish_signin(org_id: int, profile: dict, *, organization: dict,
                        proven_user_id: int | None = None) -> SigninResult:
    """A WorkOS code exchange is the only caller; untrusted browser data never reaches here."""
    async with session_maker() as db:
        config = await db.get(EnterpriseSSO, org_id)
        org = await db.get(Org, org_id)
        if (config is None or org is None or org.suspended
                or config.subscription_status not in PAID_STATUSES or not config.workos_org_id):
            raise EnterpriseSSOError("SSO is not active for this team")
        if profile.get("organization_id") != config.workos_org_id:
            raise EnterpriseSSOError("SSO response belongs to another team")
        # WorkOS can be configured to allow out-of-domain profiles. Never treat such an
        # assertion as proof of ownership of a global treg email account.
        if (organization.get("id") != config.workos_org_id
                or organization.get("external_id") != str(org_id)
                or organization.get("allow_profiles_outside_organization") is True):
            raise EnterpriseSSOError("SSO organization is not trusted for account creation")
        connection = profile.get("connection_id")
        profile_id = profile.get("id")
        idp_id = profile.get("idp_id")
        email = profile.get("email")
        if not all(isinstance(value, str) and value for value in (connection, profile_id, idp_id, email)):
            raise EnterpriseSSOError("SSO response is missing identity fields")
        if config.connection_id and config.connection_id != connection:
            raise EnterpriseSSOError("SSO response came from another connection")
        email = _norm_email(email)
        if _is_machine_email(email):
            raise EnterpriseSSOError("machine identity cannot sign in")
        # A manual removal belongs to the team and treg account, not to one WorkOS
        # connection. Check before a replacement connection can create a new identity.
        blocked_user = (await db.execute(select(User.id).join(
            EnterpriseSSOBlock, EnterpriseSSOBlock.user_id == User.id).where(
            EnterpriseSSOBlock.org_id == org_id, User.email == email,
        ))).scalar_one_or_none()
        if blocked_user is not None:
            raise EnterpriseSSOError("this team has deactivated the identity")
        domains = organization.get("domains") or []
        email_domain = email.rsplit("@", 1)[-1]
        if not any(isinstance(item, dict) and item.get("state") == "verified"
                   and isinstance(item.get("domain"), str)
                   and item["domain"].lower() == email_domain for item in domains):
            raise EnterpriseSSOError("verify the company's email domain before SSO sign-in")
        directory_user = None
        if config.plan == "sso_directory" and config.directory_id:
            if not config.directory_active:
                raise EnterpriseSSOError("directory access is inactive")
            matches = (await db.execute(select(EnterpriseDirectoryUser).where(
                EnterpriseDirectoryUser.org_id == org_id,
                EnterpriseDirectoryUser.directory_id == config.directory_id,
                EnterpriseDirectoryUser.idp_id == idp_id,
            ))).scalars().all()
            if len(matches) > 1:
                raise EnterpriseSSOError("directory identity needs review")
            directory_user = matches[0] if matches else None
            if directory_user is None or not directory_user.active or directory_user.email != email:
                raise EnterpriseSSOError("this identity is not active in the company directory")
        identity = (await db.execute(select(EnterpriseSSOIdentity).where(
            EnterpriseSSOIdentity.org_id == org_id,
            EnterpriseSSOIdentity.profile_id == profile_id,
        ))).scalar_one_or_none()
        if identity is not None and (not identity.active or identity.manually_blocked):
            raise EnterpriseSSOError("this team has deactivated the identity")
        if identity is not None:
            user = await db.get(User, identity.user_id)
            if (user is None or user.email != email or identity.connection_id != connection
                    or identity.idp_id != idp_id):
                # Email changes need an explicit, reviewed account-link flow; silently switching
                # the global User row would also change access to every other team.
                raise EnterpriseSSOError("SSO identity changed; contact the team owner")
        else:
            try:
                existing_user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
                if existing_user is not None:
                    if (proven_user_id != existing_user.id
                            or existing_user.email_verified_at is None):
                        raise EnterpriseSSOError(
                            "sign in to your existing treg account normally, then retry company SSO")
                    if signup.blocked_email(email, "enterprise_sso"):
                        raise signup.BlockedEmailError
                    user = existing_user
                else:
                    created: set[int] = set()
                    user = await signup.find_or_create_user(db, email, door="enterprise_sso",
                        created=created, verified=True, reject_unverified_existing=True)
                    if user.id not in created:
                        raise EnterpriseSSOError(
                            "sign in to your existing treg account normally, then retry company SSO")
            except signup.UnverifiedExistingUserError as exc:
                raise EnterpriseSSOError(
                    "sign in to your existing treg account normally, then retry company SSO") from exc
            except (signup.MachineIdentityError, signup.BlockedEmailError) as exc:
                raise EnterpriseSSOError("this identity cannot sign in") from exc
            existing_link = (await db.execute(select(EnterpriseSSOIdentity).where(
                EnterpriseSSOIdentity.org_id == org_id,
                EnterpriseSSOIdentity.user_id == user.id,
            ))).scalar_one_or_none()
            if existing_link is not None:
                raise EnterpriseSSOError("this account is linked to another SSO identity")
            if (await db.execute(select(EnterpriseSSOIdentity.id).where(
                EnterpriseSSOIdentity.org_id == org_id,
                EnterpriseSSOIdentity.idp_id == idp_id,
            ))).first():
                raise EnterpriseSSOError("this IdP identity is already linked to another account")
            identity = EnterpriseSSOIdentity(
                org_id=org_id, user_id=user.id, profile_id=profile_id,
                idp_id=idp_id, connection_id=connection,
                directory_user_id=directory_user.directory_user_id if directory_user else None,
            )
            db.add(identity)
        if identity is not None and directory_user is not None:
            identity.directory_user_id = directory_user.directory_user_id
            db.add(identity)
        if user.suspended:
            raise EnterpriseSSOError("account suspended")
        _, created_membership = await teams.ensure_sso_membership(db, user, org_id)
        if created_membership:
            identity.membership_created_by_sso = True
            db.add(identity)
        if not config.connection_id:
            config.connection_id = connection
            db.add(config)
        _audit(db, org_id, "sso.signin", subject=user.id)
        await db.commit()
        return SigninResult(user.id, user.token_version, org.id, org.slug)


async def set_enforced(org_id: int, enabled: bool, actor_user_id: int | None = None) -> bool:
    async with session_maker() as db:
        config = await db.get(EnterpriseSSO, org_id)
        if config is None:
            raise EnterpriseSSOError("SSO is not configured for this team")
        if enabled and (config.subscription_status not in PAID_STATUSES or not config.connection_id):
            raise EnterpriseSSOError("test SSO sign-in before requiring it")
        config.enforced = enabled
        db.add(config)
        _audit(db, org_id, "sso.enforcement_enabled" if enabled else "sso.enforcement_disabled",
               actor=actor_user_id)
        await db.commit()
        return enabled


async def block_removed_member(db, org_id: int, user_id: int, actor_user_id: int) -> None:
    """Persist an owner's removal in the membership transaction."""
    identity = (await db.execute(select(EnterpriseSSOIdentity).where(
        EnterpriseSSOIdentity.org_id == org_id,
        EnterpriseSSOIdentity.user_id == user_id,
    ))).scalar_one_or_none()
    if identity is not None:
        identity.manually_blocked = True
        identity.active = False
        db.add(identity)
        if (await db.execute(select(EnterpriseSSOBlock.id).where(
                EnterpriseSSOBlock.org_id == org_id,
                EnterpriseSSOBlock.user_id == user_id,
        ))).first() is None:
            db.add(EnterpriseSSOBlock(org_id=org_id, user_id=user_id))
        _audit(db, org_id, "sso.manual_access_removed", actor=actor_user_id, subject=user_id)


async def blocked_members(org_id: int) -> list[dict]:
    async with session_maker() as db:
        rows = (await db.execute(select(EnterpriseSSOBlock, User).join(
            User, User.id == EnterpriseSSOBlock.user_id).where(
            EnterpriseSSOBlock.org_id == org_id,
        ))).all()
        return [{"user_id": user.id, "email": user.email} for _, user in rows]


async def erase_user_links(db, user_id: int) -> None:
    """Erase SSO account links and access decisions with the global user."""
    await db.execute(delete(EnterpriseSSOBlock).where(EnterpriseSSOBlock.user_id == user_id))
    await db.execute(delete(EnterpriseSSOIdentity).where(EnterpriseSSOIdentity.user_id == user_id))


async def unblock_member(org_id: int, user_id: int, actor_user_id: int) -> None:
    async with session_maker() as db:
        block = (await db.execute(select(EnterpriseSSOBlock).where(
            EnterpriseSSOBlock.org_id == org_id,
            EnterpriseSSOBlock.user_id == user_id,
        ))).scalar_one_or_none()
        if block is None:
            raise EnterpriseSSOError("blocked SSO member not found")
        identity = (await db.execute(select(EnterpriseSSOIdentity).where(
            EnterpriseSSOIdentity.org_id == org_id,
            EnterpriseSSOIdentity.user_id == user_id,
        ))).scalar_one_or_none()
        config = await db.get(EnterpriseSSO, org_id)
        if identity is not None:
            identity.manually_blocked = False
        if identity is not None and config and config.plan == "sso_directory" and config.directory_id:
            matches = (await db.execute(select(EnterpriseDirectoryUser).where(
                EnterpriseDirectoryUser.org_id == org_id,
                EnterpriseDirectoryUser.directory_id == config.directory_id,
                EnterpriseDirectoryUser.idp_id == identity.idp_id,
            ))).scalars().all()
            record = matches[0] if len(matches) == 1 else None
            user = await db.get(User, user_id)
            identity.active = bool(config.directory_active and record and record.active
                                   and user and record.email == user.email)
        elif identity is not None:
            identity.active = True
        if identity is not None:
            db.add(identity)
        await db.delete(block)
        _audit(db, org_id, "sso.manual_access_restored", actor=actor_user_id, subject=user_id)
        await db.commit()


async def apply_directory_event(event: dict) -> bool:
    """Deactivate only the matching team membership; never globally suspend a user."""
    event_id = event.get("id")
    kind = event.get("event")
    data = event.get("data")
    if not isinstance(event_id, str) or not isinstance(data, dict):
        raise EnterpriseSSOError("invalid directory event")
    try:
        event_ms = int(datetime.fromisoformat(event["created_at"].replace("Z", "+00:00")).timestamp() * 1000)
    except (KeyError, AttributeError, TypeError, ValueError):
        event_ms = 0
    if kind not in {"dsync.activated", "dsync.deleted", "dsync.user.created", "dsync.user.updated", "dsync.user.deleted"}:
        return False
    workos_org = data.get("organization_id")
    directory_id = data.get("id") if kind in {"dsync.activated", "dsync.deleted"} else data.get("directory_id")
    if not all(isinstance(v, str) and v for v in (workos_org, directory_id)):
        raise EnterpriseSSOError("directory event is missing organization")
    async with session_maker() as db:
        if await db.get(EnterpriseSSOEvent, event_id) is not None:
            return False
        config = (await db.execute(select(EnterpriseSSO).where(
            EnterpriseSSO.workos_org_id == workos_org,
        ))).scalar_one_or_none()
        if config is None or config.plan != "sso_directory" or config.subscription_status not in PAID_STATUSES:
            return False
        if config.directory_id and config.directory_id != directory_id and config.directory_active:
            raise EnterpriseSSOError("directory event belongs to another connection")
        if kind in {"dsync.activated", "dsync.deleted"} and event_ms and event_ms <= config.directory_event_ms:
            db.add(EnterpriseSSOEvent(id=event_id))
            await db.commit()
            return False
        if kind == "dsync.activated":
            config.directory_id = directory_id
            config.directory_active = True
            config.directory_event_ms = event_ms
        elif kind == "dsync.deleted":
            config.directory_active = False
            config.directory_event_ms = event_ms
            identities = (await db.execute(select(EnterpriseSSOIdentity).where(
                EnterpriseSSOIdentity.org_id == config.org_id,
            ))).scalars().all()
            for identity in identities:
                await _deactivate_identity(db, identity)
                identity.directory_user_id = None
                db.add(identity)
        else:
            # A user event can arrive before activation. An older event must not
            # undo a later directory deletion.
            if not config.directory_active and (not config.directory_event_ms
                    or (event_ms and event_ms > config.directory_event_ms)):
                config.directory_id = directory_id
                config.directory_active = True
                config.directory_event_ms = event_ms
            directory_user_id = data.get("id")
            email = data.get("email")
            idp_id = data.get("idp_id")
            if not all(isinstance(v, str) and v for v in (directory_user_id, email, idp_id)):
                raise EnterpriseSSOError("directory event is missing identity fields")
            email = _norm_email(email)
            record = (await db.execute(select(EnterpriseDirectoryUser).where(
                EnterpriseDirectoryUser.org_id == config.org_id,
                EnterpriseDirectoryUser.directory_user_id == directory_user_id,
            ))).scalar_one_or_none()
            if record is not None and event_ms and event_ms <= record.last_event_ms:
                db.add(EnterpriseSSOEvent(id=event_id))
                await db.commit()
                return False
            record = record or EnterpriseDirectoryUser(org_id=config.org_id,
                directory_user_id=directory_user_id, directory_id=directory_id,
                idp_id=idp_id, email=email)
            record.directory_id = directory_id
            record.idp_id = idp_id
            record.email = email
            record.active = kind != "dsync.user.deleted" and data.get("state") == "active"
            record.last_event_ms = event_ms
            db.add(record)
            if not config.directory_id:
                config.directory_id = directory_id
            identity = (await db.execute(select(EnterpriseSSOIdentity).where(
                EnterpriseSSOIdentity.org_id == config.org_id,
                EnterpriseSSOIdentity.idp_id == idp_id,
            ))).scalar_one_or_none()
            if identity is not None:
                if identity.directory_user_id and identity.directory_user_id != directory_user_id:
                    raise EnterpriseSSOError("directory identity mismatch")
                identity.directory_user_id = directory_user_id
                linked_user = await db.get(User, identity.user_id)
                if not record.active or linked_user is None or linked_user.email != email:
                    await _deactivate_identity(db, identity)
                elif not identity.manually_blocked:
                    identity.active = True
                    db.add(identity)
        db.add(config)
        db.add(EnterpriseSSOEvent(id=event_id))
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()  # concurrent webhook redelivery
            return False
        return True


async def _deactivate_identity(db, identity: EnterpriseSSOIdentity) -> None:
    if identity.active:
        _audit(db, identity.org_id, "sso.directory_access_removed", subject=identity.user_id)
    identity.active = False
    membership = (await db.execute(select(Membership).where(
        Membership.org_id == identity.org_id,
        Membership.user_id == identity.user_id,
    ))).scalar_one_or_none()
    if membership is not None and membership.role != "owner":
        await teams.delete_membership(db, membership)
    db.add(identity)


async def deactivate_directory_from_api(org_id: int, workos_org_id: str) -> int:
    """Remove stale access when a complete WorkOS directory list has no active entry."""
    async with session_maker() as db:
        config = await db.get(EnterpriseSSO, org_id)
        if config is None or config.workos_org_id != workos_org_id:
            raise EnterpriseSSOError("directory belongs to another team")
        if not config.directory_active:
            return 0
        config.directory_active = False
        db.add(config)
        identities = (await db.execute(select(EnterpriseSSOIdentity).where(
            EnterpriseSSOIdentity.org_id == org_id,
        ))).scalars().all()
        for identity in identities:
            await _deactivate_identity(db, identity)
            identity.directory_user_id = None
            db.add(identity)
        await db.commit()
        return len(identities)


async def reconcile_directory(org_id: int, directory_id: str, users: list[dict],
                              observed_at_ms: int, *, verified_workos_org_id: str | None = None) -> dict:
    """Apply a complete WorkOS roster; newer webhook updates win over this snapshot."""
    roster: dict[str, tuple[str, str, bool]] = {}
    for item in users:
        if not isinstance(item, dict) or item.get("directory_id") != directory_id:
            raise EnterpriseSSOError("incomplete or wrong directory roster")
        user_id, idp_id, email = item.get("id"), item.get("idp_id"), item.get("email")
        if not all(isinstance(v, str) and v for v in (user_id, idp_id, email)) or user_id in roster:
            raise EnterpriseSSOError("invalid or duplicate directory user")
        roster[user_id] = (idp_id, _norm_email(email), item.get("state") == "active")
    if len({idp_id for idp_id, _, _ in roster.values()}) != len(roster):
        raise EnterpriseSSOError("duplicate IdP identity in directory roster")
    async with session_maker() as db:
        config = await db.get(EnterpriseSSO, org_id)
        if (config is None or config.plan != "sso_directory" or
                config.subscription_status not in PAID_STATUSES):
            raise EnterpriseSSOError("directory is no longer active for this team")
        if verified_workos_org_id is None:
            if config.directory_id != directory_id or not config.directory_active:
                raise EnterpriseSSOError("directory is no longer active for this team")
        else:
            if config.workos_org_id != verified_workos_org_id or (
                    config.directory_active and config.directory_id != directory_id):
                raise EnterpriseSSOError("directory belongs to another team")
            replacing_directory = bool(config.directory_id and config.directory_id != directory_id)
            config.directory_id = directory_id
            config.directory_active = True
            db.add(config)
        if any(item.get("organization_id") not in (None, config.workos_org_id) for item in users):
            raise EnterpriseSSOError("roster belongs to another organization")
        records = (await db.execute(select(EnterpriseDirectoryUser).where(
            EnterpriseDirectoryUser.org_id == org_id,
            EnterpriseDirectoryUser.directory_id == directory_id,
        ))).scalars().all()
        existing = {row.directory_user_id: row for row in records}
        identity_rows = (await db.execute(select(EnterpriseSSOIdentity).where(
            EnterpriseSSOIdentity.org_id == org_id,
        ))).scalars().all()
        identities = {row.idp_id: row for row in identity_rows}
        if len(identities) != len(identity_rows):
            raise EnterpriseSSOError("duplicate SSO identity needs review")
        if verified_workos_org_id is not None and replacing_directory:
            for identity in identity_rows:
                await _deactivate_identity(db, identity)
                identity.directory_user_id = None
                db.add(identity)
        removed = 0
        for user_id, (idp_id, email, active) in roster.items():
            record = existing.pop(user_id, None)
            if record is not None and record.last_event_ms > observed_at_ms:
                continue
            record = record or EnterpriseDirectoryUser(org_id=org_id,
                directory_user_id=user_id, directory_id=directory_id,
                idp_id=idp_id, email=email)
            record.idp_id, record.email, record.active = idp_id, email, active
            record.last_event_ms = observed_at_ms
            db.add(record)
            identity = identities.get(idp_id)
            if identity is not None:
                if identity.directory_user_id and identity.directory_user_id != user_id:
                    raise EnterpriseSSOError("directory identity mismatch")
                identity.directory_user_id = user_id
                linked_user = await db.get(User, identity.user_id)
                if not active or linked_user is None or linked_user.email != email:
                    await _deactivate_identity(db, identity)
                    removed += 1
                elif not identity.manually_blocked:
                    identity.active = True
                    db.add(identity)
        for record in existing.values():
            if record.last_event_ms > observed_at_ms:
                continue
            record.active = False
            record.last_event_ms = observed_at_ms
            db.add(record)
            identity = identities.get(record.idp_id)
            if identity is not None and identity.active:
                await _deactivate_identity(db, identity)
                removed += 1
        # People who signed in before Directory Sync was connected have no roster
        # row yet. A complete snapshot must remove them too when absent.
        live_idps = {idp_id for idp_id, _, active in roster.values() if active}
        for identity in identity_rows:
            if identity.idp_id in live_idps or not identity.active:
                continue
            record = next((row for row in records if row.idp_id == identity.idp_id), None)
            if record is not None and record.last_event_ms > observed_at_ms:
                continue
            await _deactivate_identity(db, identity)
            removed += 1
        await db.commit()
    return {"roster_size": len(roster), "memberships_removed": removed}


async def remove_sso_created_memberships(db, org_id: int) -> None:
    """End SSO-created memberships at subscription end, preserving pre-existing members."""
    identities = (await db.execute(select(EnterpriseSSOIdentity).where(
        EnterpriseSSOIdentity.org_id == org_id,
    ))).scalars().all()
    for identity in identities:
        membership = (await db.execute(select(Membership).where(
            Membership.org_id == org_id, Membership.user_id == identity.user_id,
        ))).scalar_one_or_none()
        if membership is not None and membership.role != "owner" and identity.membership_created_by_sso:
            await teams.delete_membership(db, membership)
