"""Payment rails for agent payments (docs/context/architecture/agent-payments.md).

A rail turns an offer into challenge headers, checks a presented credential, and settles it.
The protocol SDKs (`pympp`, `x402`) are imported only inside the concrete rails, so the light
CLI and every other server path never load them. No rail opens a database session.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class Offer:
    """What one call costs a payer, and the route the payment is bound to."""

    amount_micro: int            # integer micro-USD; equals token base units (6 decimals)
    method: str
    path: str                    # the request path, `/call/<endpoint id>`
    query: str                   # the raw query string
    resource_url: str
    description: str


@dataclass(frozen=True)
class Verified:
    """A credential that proved key control over `payer` and authorizes `amount_micro`."""

    protocol: str                # mpp | x402
    network: str
    proof_id: str                # single-use: the database refuses a second use
    payer: str                   # lowercase 0x address
    amount_micro: int
    # False: the payer already moved the money (MPP push mode); treg cannot decline to settle.
    deferrable: bool = True
    credential: Any = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class Settlement:
    reference: str               # the settlement transaction
    receipt_headers: tuple[tuple[str, str], ...] = ()


class PaymentRejected(Exception):
    """A credential that cannot pay this offer. `code` is stable for callers to branch on."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class PaymentRail(Protocol):
    protocol: str
    # Lowercase request header names this rail reads; treg removes them before the relay.
    consumed_headers: tuple[str, ...]

    def presented(self, headers: Mapping[str, str]) -> str | None: ...

    async def challenge(self, offer: Offer) -> list[tuple[str, str]]: ...

    async def verify(self, credential: str, offer: Offer) -> Verified: ...

    async def settle(self, verified: Verified, offer: Offer) -> Settlement: ...


def micro_to_decimal(amount_micro: int) -> str:
    """Integer micro-USD as the exact decimal string the SDKs take (`50000` -> `0.050000`)."""
    if amount_micro <= 0:
        raise ValueError("amount must be positive")
    return f"{amount_micro // 1_000_000}.{amount_micro % 1_000_000:06d}"
