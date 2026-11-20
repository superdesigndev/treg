"""Rules for durable resources created through a shared provider account."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..models import ProviderResource
from ..timeutil import utcnow_naive


ACTIVE = "active"
DELETED = "deleted"


class ResourceCollision(Exception):
    """An upstream id is already assigned to a different organization."""


async def list_for_org(
    db: AsyncSession, org_id: int, *, provider: str = "", resource_kind: str = "",
    include_deleted: bool = False,
) -> list[ProviderResource]:
    query = select(ProviderResource).where(ProviderResource.org_id == org_id)
    if provider:
        query = query.where(ProviderResource.provider == provider)
    if resource_kind:
        query = query.where(ProviderResource.resource_kind == resource_kind)
    if not include_deleted:
        query = query.where(ProviderResource.status == ACTIVE)
    return list((await db.execute(
        query.order_by(ProviderResource.created_at.desc(), ProviderResource.id.desc())
    )).scalars().all())


async def owned(
    db: AsyncSession, org_id: int, provider: str, resource_kind: str, upstream_id: str,
    *, include_deleted: bool = False,
) -> ProviderResource | None:
    query = select(ProviderResource).where(
        ProviderResource.org_id == org_id,
        ProviderResource.provider == provider,
        ProviderResource.resource_kind == resource_kind,
        ProviderResource.upstream_id == upstream_id,
    )
    if not include_deleted:
        query = query.where(ProviderResource.status == ACTIVE)
    return (await db.execute(query)).scalars().one_or_none()


async def assigned(
    db: AsyncSession, provider: str, resource_kind: str, upstream_id: str,
) -> ProviderResource | None:
    """Return any durable assignment for an upstream id, including deleted resources."""
    return (await db.execute(select(ProviderResource).where(
        ProviderResource.provider == provider,
        ProviderResource.resource_kind == resource_kind,
        ProviderResource.upstream_id == upstream_id,
    ))).scalars().one_or_none()


async def register(
    db: AsyncSession, *, org_id: int, provider: str, resource_kind: str,
    upstream_id: str, display_name: str, created_by: str, source_call_id: str,
    key_slot: str | None = None,
) -> ProviderResource:
    existing = (await db.execute(select(ProviderResource).where(
        ProviderResource.provider == provider,
        ProviderResource.resource_kind == resource_kind,
        ProviderResource.upstream_id == upstream_id,
    ))).scalars().one_or_none()
    if existing is not None:
        if existing.org_id != org_id:
            raise ResourceCollision(upstream_id)
        existing.status = ACTIVE
        existing.deleted_at = None
        existing.updated_at = utcnow_naive()
        if display_name.strip():
            existing.display_name = display_name.strip()[:200]
        return existing
    row = ProviderResource(
        org_id=org_id,
        provider=provider,
        resource_kind=resource_kind,
        upstream_id=upstream_id,
        display_name=display_name.strip()[:200],
        created_by=created_by,
        source_call_id=source_call_id,
        status=ACTIVE,
        key_slot=key_slot,
    )
    db.add(row)
    await db.flush()
    return row


def rename(row: ProviderResource, display_name: str) -> None:
    row.display_name = display_name.strip()[:200]
    row.updated_at = utcnow_naive()


def tombstone(row: ProviderResource) -> None:
    now = utcnow_naive()
    row.status = DELETED
    row.updated_at = now
    row.deleted_at = now


def view(row: ProviderResource) -> dict:
    return {
        "id": row.id,
        "provider": row.provider,
        "kind": row.resource_kind,
        "upstream_id": row.upstream_id,
        "display_name": row.display_name,
        "created_by": row.created_by,
        "source_call_id": row.source_call_id,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "deleted_at": row.deleted_at.isoformat() if row.deleted_at else None,
    }
