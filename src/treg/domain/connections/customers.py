"""Which calls and listings see a connection that belongs to one of the team's customers."""

from __future__ import annotations

from sqlalchemy import or_

from ...models import Secret


def visible_to(customer: str):
    """SQL predicate: the team's own connections, plus `customer`'s when one is named."""
    if customer:
        return or_(Secret.customer.is_(None), Secret.customer == customer)  # type: ignore[union-attr]
    return Secret.customer.is_(None)  # type: ignore[union-attr]


def is_visible(secret: Secret, customer: str) -> bool:
    return secret.customer is None or secret.customer == customer


def pinned_customer(pinned_tags: dict | None, primary_dim: str) -> str | None:
    """The customer a pinned token is limited to, or None for a token not pinned on that dimension."""
    value = (pinned_tags or {}).get(primary_dim)
    return str(value) if value else None
