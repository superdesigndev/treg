"""Calls in flight per provider in this process, so one provider that stops answering cannot take
every connection of the shared upstream client from the others.

`acquire` counts a call in; `release` counts it out. In mode `log` a provider past the cap is logged
(at most once per `_LOG_EVERY_S` per provider) and the call proceeds; in mode `enforce` the call is
refused with `UpstreamBusy` before anything is sent. The count is per process and DB-free, like the
rate limiter next to it.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger("treg.upstream_inflight")

RETRY_AFTER_S = 2
_LOG_EVERY_S = 10.0


class UpstreamBusy(Exception):
    def __init__(self, provider: str, active: int, cap: int) -> None:
        super().__init__(f"{provider}: {active} calls in flight, cap {cap}")
        self.provider, self.active, self.cap = provider, active, cap


class InFlight:
    def __init__(self) -> None:
        self._active: dict[str, int] = {}
        self._logged: dict[str, float] = {}

    def acquire(self, provider: str, cap: int, mode: str) -> "InFlightSlot | None":
        if mode not in ("log", "enforce") or cap <= 0 or not provider:
            return None
        active = self._active.get(provider, 0)
        if active >= cap:
            now = time.monotonic()
            if now - self._logged.get(provider, float("-inf")) >= _LOG_EVERY_S:
                self._logged[provider] = now
                log.warning("provider over its in-flight cap provider=%s active=%d cap=%d mode=%s",
                            provider, active, cap, mode)
            if mode == "enforce":
                raise UpstreamBusy(provider, active, cap)
        self._active[provider] = active + 1
        return InFlightSlot(self, provider)

    def _release(self, provider: str) -> None:
        left = self._active.get(provider, 0) - 1
        if left > 0:
            self._active[provider] = left
        else:
            self._active.pop(provider, None)

    def active(self, provider: str) -> int:
        return self._active.get(provider, 0)

    def reset(self) -> None:
        self._active.clear()
        self._logged.clear()


class InFlightSlot:
    def __init__(self, owner: InFlight, provider: str) -> None:
        self._owner, self._provider, self._held = owner, provider, True

    def release(self) -> None:
        if self._held:
            self._held = False
            self._owner._release(self._provider)


inflight = InFlight()
