"""Tool request reads — the admin half of the "catalog doesn't have X" reports."""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..models import ToolRequest


async def recent(
    db: AsyncSession, *, limit: int, before: int | None = None,
    status: str | None = None, source: str | None = None,
) -> list[ToolRequest]:
    query = select(ToolRequest)
    if before is not None:
        query = query.where(ToolRequest.id < before)
    if status is not None:
        query = query.where(ToolRequest.status == status)
    if source is not None:
        query = query.where(ToolRequest.source == source)
    return list((await db.execute(query.order_by(ToolRequest.id.desc()).limit(limit))).scalars())
