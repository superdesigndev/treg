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

    def answer(self, status_code: int, refused_by: str | None) -> tuple[int, str | None]:
        """What the wallet gets for one of treg's own refusals, and its record. A payment client
        answers every 402 by paying again, so treg's own 402 after a payment is a 503; after a
        credit draw it is the `credit_insufficient` payment challenge, a step of the payment."""
        if status_code != 402:
            return status_code, refused_by
        return (503, refused_by) if self.paid else (402, "payment")


current: contextvars.ContextVar[WalletCall | None] = contextvars.ContextVar(
    "treg_wallet_call", default=None)
