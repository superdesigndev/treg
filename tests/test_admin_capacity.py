"""GET /admin/capacity: the sweep's latest balance per account, for an alerting reader."""

from __future__ import annotations

from datetime import timedelta

from treg.config import get_settings
from treg.domain.capacity.policy import STALE_AFTER
from treg.infra.db import session_maker
from treg.models import CapacitySnapshot
from treg.timeutil import utcnow_naive

TOKEN = "c" * 40


async def _observe(*snaps: CapacitySnapshot) -> None:
    async with session_maker() as db:
        db.add_all(snaps)
        await db.commit()


async def test_the_read_token_opens_the_report_and_nothing_else(clients, monkeypatch):
    monkeypatch.setattr(get_settings(), "capacity_read_token", TOKEN)
    headers = {"X-Treg-Token": TOKEN}
    assert (await clients.get("/admin/capacity", headers=headers)).status_code == 200
    for path in ("/admin/stats", "/admin/orgs", "/admin/reconcile/spend", "/admin/kv"):
        assert (await clients.get(path, headers=headers)).status_code in (401, 403), path
    assert (await clients.get("/admin/capacity", headers={"X-Treg-Token": "c" * 39 + "d"})
            ).status_code in (401, 403)


async def test_a_short_read_token_is_never_honored(clients, monkeypatch):
    monkeypatch.setattr(get_settings(), "capacity_read_token", "short")
    response = await clients.get("/admin/capacity", headers={"X-Treg-Token": "short"})
    assert response.status_code in (401, 403)


async def test_an_ordinary_member_is_refused(clients):
    assert (await clients.get("/admin/capacity")).status_code in (401, 403)


async def test_report_reads_the_latest_observation_and_labels_it(clients, monkeypatch):
    monkeypatch.setattr(get_settings(), "capacity_read_token", TOKEN)
    now = utcnow_naive()
    await _observe(
        CapacitySnapshot(provider="serper", observed_at=now - timedelta(hours=2), remaining=0.0,
                         unit="credits"),
        CapacitySnapshot(provider="serper", observed_at=now, remaining=47880.0, unit="credits"),
        CapacitySnapshot(provider="lusha", observed_at=now, remaining=0.0, unit="credits"),
        CapacitySnapshot(provider="cloro", observed_at=now, confidence="stale",
                         error="ValueError: cloro returned no valid balance"),
        CapacitySnapshot(provider="tavily", observed_at=now - STALE_AFTER - timedelta(minutes=1),
                         remaining=137.0, unit="API credits"),
        CapacitySnapshot(provider="exa", observed_at=now, confidence="stale", error="no_balance_api"),
        CapacitySnapshot(provider="wiza", observed_at=now, confidence="stale", error="no_key"),
    )
    body = (await clients.get("/admin/capacity", headers={"X-Treg-Token": TOKEN})).json()
    rows = {r["provider"]: r for r in body["providers"]}

    assert rows["serper"]["status"] == "ok" and rows["serper"]["remaining"] == 47880.0
    assert rows["lusha"]["status"] == "issue" and rows["lusha"]["health"] == "exhausted"
    assert rows["cloro"]["status"] == "issue" and "no valid balance" in rows["cloro"]["error"]
    assert rows["tavily"]["status"] == "issue" and rows["tavily"]["health"] == "stale"
    assert rows["exa"]["status"] == "ok"
    assert rows["wiza"]["status"] == "skipped"
    assert rows["dataforseo"]["status"] == "issue" and rows["dataforseo"]["observed_at"] is None
    assert "overflow:orthogonal" in rows
    assert sum(body["counts"].values()) == len(body["providers"])
