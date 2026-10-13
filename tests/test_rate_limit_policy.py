"""Admission rules with no I/O: the policy schema, the global ceiling, the blocklist payload."""

from __future__ import annotations


import pytest
from pydantic import ValidationError

from treg.config import get_settings
from treg.domain.ratelimit import (
    BLOCKLIST_REASONS,
    GLOBAL_REASON,
    BlockEntry,
    Blocklist,
    Caller,
    KeyCache,
    LocalCeiling,
    RateLimitConfig,
)

KEY = "a" * 64


def _config(**update) -> RateLimitConfig:
    base = get_settings().rate_limit_defaults.model_dump(mode="json")
    base.update(update)
    return RateLimitConfig.model_validate(base)


@pytest.mark.parametrize("bad", [
    {"global_concurrency": 0},
    {"global_concurrency": -5},
    {"global_concurrency": 10_000_000},
    {"global_concurrency": True},
    {"global_concurrency": "50"},
    {"global_concurrency": 10, "global_paid_reserve": 10},
    {"global_concurrency": None, "global_paid_reserve": 1},
    {"global_paid_reserve": -1},
    {"concurrency_ttl_s": 5},
    {"concurrency_retry_after_s": 0},
    {"mode": "panic"},
    {"blocklist_mode": "log_only"},  # not a policy field: it lives on the blocklist's version row
    {"surprise": 1},
])
def test_write_time_ranges_reject_typos(bad):
    with pytest.raises(ValidationError):
        _config(**bad)


def test_a_document_must_be_complete():
    with pytest.raises(ValidationError):
        RateLimitConfig.model_validate({"mode": "shadow"})


def test_each_refusing_layer_has_its_own_reason():
    labels = [GLOBAL_REASON, *BLOCKLIST_REASONS.values()]
    assert len(set(labels)) == 3
    assert GLOBAL_REASON.startswith("treg_rl.") and all(r.startswith("treg_blocklist.") for r in BLOCKLIST_REASONS.values())


def test_paid_orgs_keep_a_reserved_share_of_the_global_ceiling():
    cfg = _config(global_concurrency=10, global_paid_reserve=3)
    assert cfg.global_cap("paid") == 10
    assert cfg.global_cap("unpaid") == cfg.global_cap("unknown") == 7
    assert _config(global_concurrency=None).global_cap("paid") is None


def test_local_ceiling_sheds_unpaid_first_and_takes_nothing_when_enforcing_a_refusal():
    cfg = _config(global_concurrency=3, global_paid_reserve=1)
    ceiling = LocalCeiling()
    admit = lambda m, tier: ceiling.admit(member=m, cap=cfg.global_cap(tier), ttl_ms=60_000,
                                          retry_ms=1_000, enforce=True, now_ms=0)
    assert admit("u1", "unpaid").admitted and admit("u2", "unpaid").admitted
    shed = admit("u3", "unpaid")
    assert not shed.admitted and not shed.ok and shed.retry_ms == 1_000
    assert len(ceiling) == 2
    assert admit("p1", "paid").admitted
    assert not admit("p2", "paid").admitted


def test_shadow_measures_past_the_ceiling_without_refusing():
    ceiling = LocalCeiling()
    v = [ceiling.admit(member=str(i), cap=1, ttl_ms=60_000, retry_ms=1_000, enforce=False, now_ms=0)
         for i in range(3)]
    assert all(x.admitted for x in v) and [x.ok for x in v] == [True, False, False]
    assert v[-1].in_flight == 3


def test_local_ceiling_entries_age_out():
    ceiling = LocalCeiling()
    ceiling.admit(member="leaked", cap=1, ttl_ms=1_000, retry_ms=1, enforce=True, now_ms=0)
    assert not ceiling.admit(member="x", cap=1, ttl_ms=1_000, retry_ms=1, enforce=True, now_ms=500).admitted
    assert ceiling.admit(member="y", cap=1, ttl_ms=1_000, retry_ms=1, enforce=True, now_ms=1_001).admitted


def test_key_cache_expires_and_is_bounded():
    cache = KeyCache(ttl_s=60, max_size=2)
    for key in ("a", "b", "c"):
        cache.put(Caller(key, 1, "o", False, 0), now=0)
    assert cache.get("a", now=1) is None and len(cache) == 2
    assert cache.get("b", now=59) is not None
    assert cache.get("b", now=61) is None


def test_expired_entries_stop_matching():
    bl = Blocklist.build(7, "2026-10-12T09:00:00Z", (
        BlockEntry("key_hash", KEY, "flood", 4_000_000_000, "ops"),
        BlockEntry("org_id", "42", "flood", 946_684_800, "ops"),  # expired in 2000
    ))
    assert bl.version == 7
    assert bl.match("key_hash", KEY, now=0).reason == "flood"
    assert bl.match("org_id", "42", now=2_000_000_000) is None
    assert bl.match("org_id", "43", now=0) is None
    assert bl.active(now=2_000_000_000) == 1
