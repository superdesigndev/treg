"""Identity eligibility for the one-time signup gift. Money is moved by the application."""
from sqlalchemy import select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from ...models import SignupPromoClaim, User
from ...timeutil import utcnow_naive
from .access import _norm_email

# Providers that deliver every dot variant of a local part to one mailbox.
_DOTLESS_DOMAINS = {"gmail.com": "gmail.com", "googlemail.com": "gmail.com"}


def promo_inbox(email: str) -> str:
    """The mailbox an address delivers to, as far as the signup gift is concerned.

    Starts from the login identity (`_norm_email`: lowercased, trailing domain dots dropped), then
    drops `+tag` subaddressing for every domain and ignores Gmail's dots, so aliases of one inbox
    share a single gift. Each alias remains its own login; only the gift is shared.
    """
    local, _, domain = _norm_email(email).rpartition("@")
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


async def claim_signup_promo(db: AsyncSession, user_id: int) -> bool:
    """Stage a single claim per user and per inbox, across all teams, processes and retries.

    The caller commits with the grant. A user whose inbox was already claimed through another
    alias loses its own eligibility too, so it never gets a second try.
    """
    result = await db.execute(
        update(User).where(*_eligible(user_id)).values(signup_promo_available=False).returning(User.email)
    )
    email = result.scalar_one_or_none()
    if email is None:
        return False
    insert = postgresql.insert if db.get_bind().dialect.name == "postgresql" else sqlite.insert
    claimed = await db.execute(
        insert(SignupPromoClaim)
        .values(inbox=promo_inbox(email), user_id=user_id, created_at=utcnow_naive())
        .on_conflict_do_nothing(index_elements=[SignupPromoClaim.inbox])
    )
    return claimed.rowcount == 1
