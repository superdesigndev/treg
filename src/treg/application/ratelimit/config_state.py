"""The process's admission policy: one in-memory value, replaced only by `config_sync`.

The request path reads `current()` and nothing else; this module never touches the database or
the shared store. `config_sync` reads the newest `RateLimitConfigVersion` row from Postgres (the
only source; the Redis copy is a cache nothing serves from) and calls `install`, `no_policy` or
`fail`. A bad or unreadable policy keeps the last good one, for at most
`rate_limit_config_max_age_s`; past that an `enforce` policy is served as `shadow` and the process
alerts. With no good policy and an unreadable database the env defaults serve (their mode is `off`
or `shadow` by construction) and the process alerts. An empty table is not a fault: nothing was
ever written, so the env defaults are the policy.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, replace
from typing import Literal

from pydantic import ValidationError

from ... import analytics
from ...config import get_settings
from ...domain.ratelimit import RateLimitConfig

log = logging.getLogger("treg.ratelimit")

Source = Literal["env", "db"]
_clock = time.monotonic


@dataclass(frozen=True)
class ConfigState:
    config: RateLimitConfig
    version: int  # 0 = the env defaults
    source: Source
    # True while the env defaults serve because Postgres could not be read.
    fallback: bool
    # True while serving a last good policy older than the max age, demoted to shadow.
    degraded: bool = False


class InvalidConfig(ValueError):
    """A policy document that must not be served or stored."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def validate(raw: object) -> RateLimitConfig:
    """Schema, ranges and the one cross-setting rule: a concurrency entry must outlive a request."""
    try:
        config = RateLimitConfig.model_validate(raw)
    except ValidationError as exc:
        raise InvalidConfig([
            f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors()
        ]) from None
    timeout = get_settings().call_timeout_s
    if config.concurrency_ttl_s < timeout:
        raise InvalidConfig([f"concurrency_ttl_s: must be at least call_timeout_s ({timeout})"])
    return config


def parse_document(raw: str) -> tuple[int, RateLimitConfig]:
    """`rl:config` is `{"version": <positive int>, "config": {...}}`."""
    try:
        doc = json.loads(raw)
    except ValueError:
        raise InvalidConfig(["not JSON"]) from None
    if not isinstance(doc, dict):
        raise InvalidConfig(["not an object"])
    version = doc.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise InvalidConfig(["version: must be a positive integer"])
    return version, validate(doc.get("config"))


def encode_document(version: int, config: RateLimitConfig, **meta) -> str:
    return json.dumps({"version": version, "config": config.model_dump(mode="json"), **meta},
                      separators=(",", ":"), sort_keys=True)


def _env_state(*, fallback: bool = False) -> ConfigState:
    return ConfigState(get_settings().rate_limit_defaults, 0, "env", fallback=fallback)


_state: ConfigState | None = None
_last_good: ConfigState | None = None
_last_good_at = 0.0  # monotonic time of the last successful read
_last_problem: str | None = None
_alerts = {"config_fallback_alerts": 0, "config_stale_alerts": 0, "config_read_errors": 0}


def current() -> ConfigState:
    global _state
    if _state is None:
        _state = _env_state()  # until the first refresh; only a refresh that finds nothing alerts
    return _state


def age_s() -> float | None:
    return None if _last_good is None else _clock() - _last_good_at


def counters() -> dict:
    return dict(_alerts)


def _swap(new: ConfigState) -> None:
    global _state
    old = _state
    _state = new
    if old is None or (old.version, old.source, old.fallback, old.degraded, old.config.mode) != (
            new.version, new.source, new.fallback, new.degraded, new.config.mode):
        log.info("rate_limit_config_changed %s", json.dumps({
            "event": "rate_limit_config_changed", "version": new.version, "source": new.source,
            "mode": new.config.mode,
            "previous_version": old.version if old else None, "fallback": new.fallback,
            "degraded": new.degraded,
        }, separators=(",", ":")))


def _alert(kind: str, event: str, **fields) -> None:
    _alerts[kind] += 1
    log.error("%s %s", event, json.dumps({"event": event, **fields}, separators=(",", ":")))
    analytics.capture(analytics.SERVER_DISTINCT_ID, event.replace("rate_limit_", "ratelimit_"), fields)


def _keep(why: str) -> None:
    """Nothing usable was read: keep the last good policy (demoted past its max age), or serve the
    env defaults loudly."""
    global _last_problem
    first = why != _last_problem
    _last_problem = why
    if _last_good is not None:
        stale = _clock() - _last_good_at > get_settings().rate_limit_config_max_age_s
        if not stale:
            if first:
                log.warning("rate_limit_config_kept %s", json.dumps({
                    "event": "rate_limit_config_kept", "reason": why, "version": _last_good.version,
                }, separators=(",", ":")))
            _swap(_last_good)
            return
        demoted = _last_good.config
        if demoted.mode == "enforce":
            demoted = demoted.model_copy(update={"mode": "shadow"})
        if not current().degraded:
            _alert("config_stale_alerts", "rate_limit_config_stale", reason=why,
                   version=_last_good.version, age_s=round(_clock() - _last_good_at),
                   mode=demoted.mode)
        _swap(replace(_last_good, config=demoted, degraded=True))
        return
    fresh = _env_state(fallback=True)
    if first:
        _alert("config_fallback_alerts", "rate_limit_config_env_fallback", reason=why,
               mode=fresh.config.mode)
    _swap(fresh)


def install(version: int, raw: object) -> ConfigState:
    """The newest Postgres row. Validated; a bad one keeps the last good policy and alerts."""
    global _last_good, _last_good_at, _last_problem
    current()
    if _last_good is not None and _last_good.version == version:
        _last_good_at = _clock()  # unchanged, and read fresh just now
        _last_problem = None
        _swap(_last_good)
        return _state
    try:
        config = validate(raw)
    except InvalidConfig as exc:
        _keep(f"invalid version {version}: {exc}")
        return _state
    _last_good = ConfigState(config, version, "db", fallback=False)
    _last_good_at = _clock()
    _last_problem = None
    _swap(_last_good)
    return _state


def no_policy() -> ConfigState:
    """The table is empty: nothing was ever written, so the env defaults are the policy."""
    global _last_problem
    current()
    _last_problem = None
    _swap(_env_state())
    return _state


def fail(why: str) -> ConfigState:
    _alerts["config_read_errors"] += 1
    current()
    _keep(why)
    return _state


def reset_for_tests() -> None:
    global _state, _last_good, _last_good_at, _last_problem
    _state = _last_good = _last_problem = None
    _last_good_at = 0.0
    for k in _alerts:
        _alerts[k] = 0
