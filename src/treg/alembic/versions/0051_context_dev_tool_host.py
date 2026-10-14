"""Move connected Brand.dev tools to Context.dev's current API host.

Revision ID: 0051
Revises: 0050

Only tools auto-provisioned from a branddev connection are changed. A manually registered tool may
intentionally use the legacy host and must keep its caller-chosen URL.

Rollback floor: a downgrade also moves connections created on the new host back to the legacy host.
Callers using URL passthrough must use the host stored on their connected tool after either change.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0051"
down_revision: str | Sequence[str] | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True

OLD_BASE = "https://api.brand.dev/v1"
NEW_BASE = "https://api.context.dev/v1"

tool = sa.table(
    "tool",
    sa.column("id", sa.Integer),
    sa.column("org_id", sa.Integer),
    sa.column("name", sa.String),
    sa.column("base_url", sa.String),
    sa.column("host", sa.String),
    sa.column("bindings", sa.JSON),
)
secret = sa.table(
    "secret",
    sa.column("id", sa.Integer),
    sa.column("org_id", sa.Integer),
    sa.column("name", sa.String),
    sa.column("provider", sa.String),
)


def _move_connected_tools(source: str, target: str) -> None:
    bind = op.get_bind()
    candidates = bind.execute(
        sa.select(tool.c.id, tool.c.bindings, secret.c.id.label("secret_id"))
        .join(secret, sa.and_(tool.c.org_id == secret.c.org_id, tool.c.name == secret.c.name))
        .where(tool.c.base_url == source, secret.c.provider == "branddev")
    ).mappings()
    for row in candidates:
        if not any(
            isinstance(binding, dict) and binding.get("secret_id") == row["secret_id"]
            for binding in (row["bindings"] or [])
        ):
            continue
        bind.execute(
            sa.update(tool).where(tool.c.id == row["id"])
            .values(base_url=target, host=sa.engine.make_url(target).host)
        )


def upgrade() -> None:
    _move_connected_tools(OLD_BASE, NEW_BASE)


def downgrade() -> None:
    _move_connected_tools(NEW_BASE, OLD_BASE)
