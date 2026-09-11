"""Identity eligibility for the one-time signup gift. Money is moved by the application."""
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from ...models import User


async def claim_signup_promo(db: AsyncSession, user_id: int) -> bool:
    """Stage a single claim across all teams, processes and retries. Caller commits with the grant."""
    result = await db.execute(
        update(User).where(
            User.id == user_id,
            User.email_verified_at.is_not(None),
            User.signup_promo_available.is_(True),
            User.suspended.is_(False),
            User.demo.is_(False),
        ).values(signup_promo_available=False)
    )
    return result.rowcount == 1
