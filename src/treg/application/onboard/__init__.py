"""First-run onboarding journeys and their transaction boundaries."""

import sys

from sqlalchemy.ext.asyncio import AsyncSession

from ... import models as _models
from ...infra.db import session_maker
from ...models import User


# This alias preserves demo.reset's relative model import; without it the package move breaks resolution.
sys.modules.setdefault(__name__ + ".models", _models)

from . import demo as demo_seed  # noqa: E402 - needs the sys.modules alias above


async def _user(user_id: int, db: AsyncSession) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise RuntimeError("authenticated user disappeared")
    return user


async def skip(*, user_id: int) -> dict:
    async with session_maker() as db:
        user = await _user(user_id, db)
        user.onboarded = True
        await db.commit()
        return {"onboarded": True}


async def reset(*, user_id: int) -> dict:
    async with session_maker() as db:
        return await demo_seed.reset(db, await _user(user_id, db))
