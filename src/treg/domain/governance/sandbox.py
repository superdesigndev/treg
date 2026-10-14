"""Resource limits for anonymous landing sandbox organizations."""

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select


class SandboxLimitError(Exception):
    def __init__(self, cap: int, noun: str):
        self.cap = cap
        self.noun = noun
        super().__init__(noun)


async def enforce_sandbox_cap(
    *, sandbox: bool, org_id: int, model, cap: int, noun: str, db: AsyncSession,
) -> None:
    if not sandbox:
        return
    n = (await db.execute(select(func.count()).select_from(model).where(model.org_id == org_id))).scalar_one()
    if n >= cap:
        raise SandboxLimitError(cap, noun)
