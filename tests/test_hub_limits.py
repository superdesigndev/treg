"""Hub runs in flight: the team cap, the tool cap, and the per-process fallback."""

from __future__ import annotations

from contextlib import AsyncExitStack

import pytest

from treg.application.hub import limits
from treg.config import get_settings
from treg.infra import provider_concurrency


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("TREG_HUB_TOOL_MAX_RUNS", "20")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    assert not limits._active, "a place was not given back"


async def test_the_fifth_run_of_a_team_is_refused_and_a_finished_one_frees_a_place():
    async with AsyncExitStack() as held:
        for n in range(limits.MAX_RUNS_PER_TEAM):
            await held.enter_async_context(limits.slot(7, f"t.tool{n}"))
        with pytest.raises(limits.HubBusy) as busy:
            async with limits.slot(7, "t.other"):
                pass
        assert busy.value.detail() == {
            "error": "hub_busy", "scope": "team", "max": limits.MAX_RUNS_PER_TEAM,
            "retry_after_s": limits.RETRY_AFTER_S, "message": busy.value.message}
        async with limits.slot(8, "t.other"):
            pass
    async with limits.slot(7, "t.other"):
        pass


async def test_one_tool_is_capped_across_teams_without_holding_the_refused_team_place(monkeypatch):
    monkeypatch.setenv("TREG_HUB_TOOL_MAX_RUNS", "2")
    get_settings.cache_clear()
    async with limits.slot(1, "treg.video-shots"), limits.slot(2, "treg.video-shots"):
        with pytest.raises(limits.HubBusy) as busy:
            async with limits.slot(3, "treg.video-shots"):
                pass
        assert busy.value.scope == "tool" and busy.value.cap == 2
        assert limits.active(3) == 0
        async with limits.slot(3, "treg.other-tool"):
            pass


async def test_a_tool_cap_of_zero_is_no_tool_cap(monkeypatch):
    monkeypatch.setenv("TREG_HUB_TOOL_MAX_RUNS", "0")
    get_settings.cache_clear()
    async with AsyncExitStack() as held:
        for team in range(30):
            await held.enter_async_context(limits.slot(team, "treg.video-shots"))


async def test_an_unanswering_store_falls_back_to_this_process(monkeypatch):
    asked: list[tuple[str, int, str]] = []

    async def no_answer(account, cap, *, namespace="provider-concurrency"):
        asked.append((account, cap, namespace))
        return None

    monkeypatch.setattr(provider_concurrency, "enabled", lambda: True)
    monkeypatch.setattr(provider_concurrency, "try_acquire", no_answer)
    async with AsyncExitStack() as held:
        for n in range(limits.MAX_RUNS_PER_TEAM):
            await held.enter_async_context(limits.slot(5, f"t.tool{n}"))
        with pytest.raises(limits.HubBusy):
            async with limits.slot(5, "t.more"):
                pass
    assert ("5", limits.MAX_RUNS_PER_TEAM, "hub-team") in asked
    assert ("t.tool0", 20, "hub-tool") in asked


async def test_a_busy_store_refuses_without_counting_locally(monkeypatch):
    async def busy(account, cap, *, namespace="provider-concurrency"):
        return provider_concurrency.BUSY

    monkeypatch.setattr(provider_concurrency, "enabled", lambda: True)
    monkeypatch.setattr(provider_concurrency, "try_acquire", busy)
    with pytest.raises(limits.HubBusy) as refused:
        async with limits.slot(5, "t.tool"):
            pass
    assert refused.value.scope == "team"


async def test_runs_the_shared_store_admitted_still_count_when_it_stops_answering(monkeypatch):
    released: list[str] = []

    class Lease:
        def __init__(self, account):
            self.account = account

        def release(self):
            released.append(self.account)

    answering = True

    async def shared(account, cap, *, namespace="provider-concurrency"):
        return Lease(account) if answering else None

    monkeypatch.setattr(provider_concurrency, "enabled", lambda: True)
    monkeypatch.setattr(provider_concurrency, "try_acquire", shared)
    async with AsyncExitStack() as held:
        for n in range(limits.MAX_RUNS_PER_TEAM):
            await held.enter_async_context(limits.slot(6, f"t.tool{n}"))
        assert limits.active(6) == limits.MAX_RUNS_PER_TEAM
        answering = False
        with pytest.raises(limits.HubBusy):
            async with limits.slot(6, "t.more"):
                pass
    assert released.count("6") == limits.MAX_RUNS_PER_TEAM
