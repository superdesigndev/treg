"""A keyless call paid by a wallet (docs/context/architecture/agent-payments.md, "At most the
quote"). The router marks the call before it runs; the call road keeps its charge within the quote
its 402 named."""

from __future__ import annotations

import contextvars
from dataclasses import dataclass


@dataclass(frozen=True)
class WalletCall:
    quote_micro: int   # what the 402 said the call costs at most, margin included
    paid: bool         # this request settled a payment; False: it draws credit the wallet holds


current: contextvars.ContextVar[WalletCall | None] = contextvars.ContextVar(
    "treg_wallet_call", default=None)
