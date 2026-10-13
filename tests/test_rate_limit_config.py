"""The admission policy a process serves: env defaults, the `rl:config` refresh, last good, max age."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from pydantic import ValidationError

from treg import analytics
from treg.application.ratelimit import background, config_state, config_sync, limiter
from treg.config import Settings, get_settings
from treg.domain.ratelimit import RateLimitConfig
from treg.infra import kv
from tests.fake_rate_limit_store import install


@pytest.fixture(autouse=True)
def _fresh():
    get_settings.cache_clear()
    limiter.reset_for_tests()
    background.reset_for_tests()
    yield
    limiter.reset_for_tests()
    background.reset_for_tests()
    get_settings.cache_clear()


@pytest.fixture
def alerts(monkeypatch):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(analytics, "capture", lambda _id, event, props=None, **_: events.append((event, props)))
    return events


def _doc(version: int, **update) -> str:
    base = get_settings().rate_limit_defaults.model_dump(mode="json")
    base.update(update)
    return config_state.encode_document(version, RateLimitConfig.model_validate(base))


def _changes(caplog) -> list[dict]:
    return [json.loads(r.getMessage().split(" ", 1)[1]) for r in caplog.records
            if r.getMessage().startswith("rate_limit_config_changed ")]


def test_env_refuses_enforce():
    with pytest.raises(ValidationError, match="enforce is set only through the audited admin endpoint"):
        Settings(rate_limit_mode="enforce")


def test_the_blocklist_mode_is_not_part_of_the_policy():
    """It lives on the blocklistversion row; a policy document carrying it is refused."""
    doc = get_settings().rate_limit_defaults.model_dump(mode="json")
    assert "blocklist_mode" not in doc
    with pytest.raises(ValidationError):
        RateLimitConfig.model_validate({**doc, "blocklist_mode": "enforce_403"})


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_env_accepts_off_and_shadow(mode):
    assert Settings(rate_limit_mode=mode).rate_limit_defaults.mode == mode


def test_env_defaults_hardcode_no_capacity():
    defaults = Settings().rate_limit_defaults
    assert defaults.global_concurrency is None and defaults.global_paid_reserve == 0
    assert defaults.mode == "off"


@pytest.mark.parametrize("bad", [
    {"rate_limit_global_concurrency": 0},
    {"rate_limit_global_concurrency": 10, "rate_limit_global_paid_reserve": 10},
    {"rate_limit_global_concurrency": 10_000_000},
    {"rate_limit_concurrency_ttl_s": 5},
])
def test_env_defaults_are_range_checked_at_boot(bad):
    with pytest.raises(ValidationError):
        Settings(**bad)


async def _write(**update) -> int:
    """A policy row, as the admin write path stores it."""
    from treg.infra.db import session_maker
    from treg.models import RateLimitConfigVersion
    doc = get_settings().rate_limit_defaults.model_dump(mode="json")
    doc.update(update)
    async with session_maker() as db:
        row = RateLimitConfigVersion(actor="t", action="set", mode=doc.get("mode", "?"), config=doc, note="n")
        db.add(row)
        await db.flush()
        version = row.id
        await db.commit()
    return version


async def test_a_valid_policy_is_loaded_from_postgres_and_its_version_logged(clients, caplog):
    v = await _write(mode="shadow", global_concurrency=40, global_paid_reserve=10)
    caplog.set_level(logging.INFO, logger="treg.ratelimit")
    state = await config_sync.poll_once()
    assert (state.version, state.source, state.fallback, state.degraded) == (v, "db", False, False)
    assert state.config.global_cap("unpaid") == 30
    change = _changes(caplog)[-1]
    assert (change["version"], change["mode"]) == (v, "shadow")


async def test_the_version_is_logged_on_every_change_and_only_then(clients, caplog):
    caplog.set_level(logging.INFO, logger="treg.ratelimit")
    v1 = await _write(mode="shadow")
    await config_sync.poll_once()
    await config_sync.poll_once()
    v2 = await _write(mode="off")
    await config_sync.poll_once()
    loaded = [c for c in _changes(caplog) if c["source"] == "db"]
    assert [(c["version"], c["mode"], c["previous_version"]) for c in loaded] == [
        (v1, "shadow", 0), (v2, "off", v1)]


async def test_workers_never_serve_the_redis_copy(clients, monkeypatch):
    """Postgres is the source: a stale (or newer, foreign) document in Redis changes nothing."""
    store = install(monkeypatch)
    v = await _write(mode="shadow")
    store.docs[kv.RL_CONFIG_KEY] = _doc(v + 50, mode="enforce", global_concurrency=1)
    state = await config_sync.poll_once()
    assert (state.version, state.config.mode) == (v, "shadow")
    assert not [k for k in store.calls if k.startswith("rl_get")]


@pytest.mark.parametrize("bad", [
    {"mode": "loud"}, {"global_concurrency": 0}, {"concurrency_ttl_s": 30}, {"surprise": 1},
])
async def test_a_bad_policy_row_keeps_the_last_good_policy(clients, caplog, bad):
    good = await _write(global_concurrency=40)
    await config_sync.poll_once()
    await _write(**bad)
    caplog.set_level(logging.WARNING, logger="treg.ratelimit")
    state = await config_sync.poll_once()
    assert (state.version, state.config.global_concurrency) == (good, 40)
    assert any(r.getMessage().startswith("rate_limit_config_kept") for r in caplog.records)


async def test_an_empty_table_means_the_env_defaults_without_an_alert(clients, alerts):
    state = await config_sync.poll_once()
    assert (state.version, state.source, state.fallback) == (0, "env", False)
    assert not alerts


def _db_down(monkeypatch):
    def down():
        raise ConnectionError("database unreachable")
    monkeypatch.setattr(config_sync, "ratelimit_session_maker", down)


async def test_with_nothing_good_and_postgres_down_the_env_defaults_serve_and_alert(clients, monkeypatch, caplog, alerts):
    _db_down(monkeypatch)
    caplog.set_level(logging.ERROR, logger="treg.ratelimit")
    state = await config_sync.poll_once()
    assert (state.version, state.source, state.fallback) == (0, "env", True)
    assert state.config == get_settings().rate_limit_defaults
    assert state.config.mode in ("off", "shadow")
    assert any(r.getMessage().startswith("rate_limit_config_env_fallback") and r.levelno == logging.ERROR
               for r in caplog.records)
    assert [e for e, _ in alerts] == ["ratelimit_config_env_fallback"]
    _, gauge = await background.emit()
    assert gauge["config_fallback"] is True and gauge["config_source"] == "env"


async def test_postgres_down_keeps_the_cached_policy(clients, monkeypatch, alerts):
    v = await _write(global_concurrency=40)
    await config_sync.poll_once()
    _db_down(monkeypatch)
    state = await config_sync.poll_once()
    assert (state.version, state.config.global_concurrency, state.fallback) == (v, 40, False)
    assert config_state.counters()["config_read_errors"] == 1
    assert not alerts


async def test_past_the_max_age_an_enforcing_last_good_drops_to_shadow_and_alerts(clients, monkeypatch, alerts):
    clock = [1_000.0]
    monkeypatch.setattr(config_state, "_clock", lambda: clock[0])
    v = await _write(mode="enforce", global_concurrency=40)
    assert (await config_sync.poll_once()).config.mode == "enforce"
    real = config_sync.ratelimit_session_maker
    _db_down(monkeypatch)
    clock[0] += get_settings().rate_limit_config_max_age_s - 1
    assert (await config_sync.poll_once()).config.mode == "enforce"  # still inside the max age
    clock[0] += 2
    state = await config_sync.poll_once()
    assert (state.version, state.config.mode, state.degraded) == (v, "shadow", True)
    assert state.config.global_concurrency == 40
    assert [e for e, _ in alerts].count("ratelimit_config_stale") == 1
    await config_sync.poll_once()
    assert [e for e, _ in alerts].count("ratelimit_config_stale") == 1  # alerts once
    monkeypatch.setattr(config_sync, "ratelimit_session_maker", real)
    assert (await config_sync.poll_once()).config.mode == "enforce"  # a fresh read restores it


async def test_the_config_loop_reads_postgres_on_its_interval(clients, monkeypatch):
    monkeypatch.setenv("TREG_RATE_LIMIT_CONFIG_REFRESH_S", "7")
    get_settings.cache_clear()
    v = await _write()
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(background.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await background.config_loop()
    assert sleeps == [7.0, 7.0, 7.0]
    assert config_state.current().version == v


async def test_a_slow_store_is_a_miss_within_the_budget(monkeypatch):
    """Slow but not failing: past `rate_limit_store_timeout_ms` the call is a miss (Unavailable),
    so the ceiling falls back to the per-process one and a read keeps its last good copy."""
    import time

    class SlowClient:
        async def eval(self, *a):
            await asyncio.sleep(0.5)
            return [1, 1, 1]

        async def get(self, key):
            await asyncio.sleep(0.5)

    store = kv.RedisStore.__new__(kv.RedisStore)
    store._client = SlowClient()
    started = time.monotonic()
    with pytest.raises(kv.Unavailable):
        await store.rl_admit_key(kv.RL_GLOBAL_KEY, "m", 5, 60_000, False)
    assert time.monotonic() - started < 0.2  # the 30 ms default budget, not the store's pace
    with pytest.raises(kv.Unavailable):
        await store.rl_get(kv.RL_CONFIG_KEY)
