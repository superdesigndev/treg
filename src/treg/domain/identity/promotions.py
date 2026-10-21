"""Identity eligibility for the one-time signup gift. Money is moved by the application."""
import logging
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from hashlib import sha256
from ipaddress import IPv6Address, ip_address, ip_network

import idna
from sqlalchemy import delete, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession
from tldextract import TLDExtract

from ...config import get_settings
from ...models import SignupPromoBucket, SignupPromoClaim, User
from ...timeutil import utcnow_naive
from .access import _norm_email

# Providers that deliver every dot variant of a local part to one mailbox.
_DOTLESS_DOMAINS = {"gmail.com": "gmail.com", "googlemail.com": "gmail.com"}

_domain_parts = TLDExtract(suffix_list_urls=(), cache_dir=None, include_psl_private_domains=False)


def _domain(email: str) -> str:
    domain = _norm_email(email).rpartition("@")[2]
    return idna.encode(domain, uts46=True, std3_rules=True).decode("ascii").lower().rstrip(".")


@lru_cache(maxsize=64)
def _shared_domains(value: str) -> frozenset[str]:
    return frozenset(_domain("@" + item.strip()) for item in value.split(",") if item.strip())


def _grant_limits(email: str, client_ip: str) -> list[tuple[str, int]]:
    settings = get_settings()
    if "%" in client_ip:
        raise ValueError("scoped address")
    address = ip_address(client_ip)
    if isinstance(address, IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    network = str(ip_network(f"{address}/{64 if address.version == 6 else 32}", strict=False))
    limits = [("ip:" + sha256(network.encode()).hexdigest(), settings.promo_grant_ip_limit)]
    domain = _domain(email)
    if domain not in _shared_domains(settings.promo_shared_email_domains):
        parts = _domain_parts(domain)
        root = parts.top_domain_under_public_suffix
        if not parts.suffix and "." in domain:
            root = ".".join(domain.split(".")[-2:])
        if not root:
            raise ValueError("no registrable domain")
        limits.append(("domain:" + sha256(root.encode()).hexdigest(), settings.promo_grant_domain_limit))
    if settings.promo_grant_global_limit:
        limits.append(("global", settings.promo_grant_global_limit))
    return sorted(limits)


class _CreditLimited(Exception):
    pass


async def _admit_grant(db: AsyncSession, email: str, client_ip: str, now: datetime) -> None:
    try:
        limits = _grant_limits(email, client_ip)
    except (ValueError, idna.IDNAError) as exc:
        raise _CreditLimited from exc
    window = get_settings().promo_grant_window_s
    expires = now + timedelta(seconds=window)
    insert = postgresql.insert if db.get_bind().dialect.name == "postgresql" else sqlite.insert
    for key, limit in limits:
        stmt = insert(SignupPromoBucket).values(key=key, hits=[], expires_at=expires)
        hits = (await db.execute(stmt.on_conflict_do_update(
            index_elements=[SignupPromoBucket.key], set_={"key": stmt.excluded.key},
        ).returning(SignupPromoBucket.hits))).scalar_one()
        admitted_at = utcnow_naive()
        timestamp = admitted_at.replace(tzinfo=timezone.utc).timestamp()
        recent = [hit for hit in hits if hit > timestamp - window]
        if len(recent) >= limit:
            raise _CreditLimited
        await db.execute(update(SignupPromoBucket).where(SignupPromoBucket.key == key).values(
            hits=[*recent, timestamp], expires_at=admitted_at + timedelta(seconds=window),
        ))


def promo_inbox(email: str) -> str:
    """The mailbox an address delivers to, as far as the signup gift is concerned.

    Starts from the login identity (`_norm_email`: lowercased, trailing domain dots dropped), then
    canonicalizes IDNA domains, drops `+tag` subaddressing and ignores Gmail's dots, so aliases
    of one inbox share a single gift. Each alias remains its own login; only the gift is shared.
    """
    local = _norm_email(email).rpartition("@")[0]
    domain = _domain(email)
    local = local.split("+", 1)[0]
    if domain in _DOTLESS_DOMAINS:
        local, domain = local.replace(".", ""), _DOTLESS_DOMAINS[domain]
    return f"{local}@{domain}"


def _eligible(user_id: int) -> tuple:
    return (
        User.id == user_id,
        User.email_verified_at.is_not(None),
        User.signup_promo_available.is_(True),
        User.suspended.is_(False),
        User.demo.is_(False),
    )


async def signup_promo_email(db: AsyncSession, user_id: int) -> str | None:
    """The address of a user who could claim the gift right now, else None. Read-only: the claim
    below stays the only guard, so a concurrent claim between the two is still refused there."""
    return (await db.execute(select(User.email).where(*_eligible(user_id)))).scalar_one_or_none()


async def claim_signup_promo(db: AsyncSession, user_id: int, *, client_ip: str = "") -> bool:
    """Stage a single claim per user and per inbox, across all teams, processes and retries.

    The caller commits with the grant. A user whose inbox was already claimed through another
    alias loses its own eligibility too, so it never gets a second try.
    """
    now = utcnow_naive()
    # SQLite's legacy transaction mode needs DML before SAVEPOINT, or RELEASE commits the claim.
    await db.execute(delete(SignupPromoBucket).where(SignupPromoBucket.expires_at <= now))
    try:
        async with db.begin_nested():
            result = await db.execute(
                update(User).where(*_eligible(user_id)).values(signup_promo_available=False).returning(User.email)
            )
            email = result.scalar_one_or_none()
            if email is None:
                return False
            insert = postgresql.insert if db.get_bind().dialect.name == "postgresql" else sqlite.insert
            claimed = await db.execute(
                insert(SignupPromoClaim)
                .values(inbox=promo_inbox(email), user_id=user_id, created_at=now)
                .on_conflict_do_nothing(index_elements=[SignupPromoClaim.inbox])
            )
            if claimed.rowcount != 1:
                return False
            await _admit_grant(db, email, client_ip, now)
        return True
    except _CreditLimited:
        logging.getLogger("treg").info("event=signup_credit_limited user=%s", user_id)
        return False
