"""/rankings: the boards built from the folded day buckets, their floors, and the served page."""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

from httpx import AsyncClient

from treg.application import rankings as rankings_app
from treg.domain.catalog import rankings, store
from treg.infra.db import session_maker
from sqlalchemy import select

from treg.models import EndpointDayStat, RankingsSnapshot
from treg.timeutil import utcnow_naive

LAST = date(2026, 10, 25)


def _sync_endpoints(n: int) -> list[dict]:
    """`n` browsable synchronous endpoints from distinct providers."""
    out, providers = [], set()
    for ep in store.load().endpoints:
        if store.browsable(ep) and not ep.get("async") and not ep.get("status") \
                and not ep.get("observed_from") and ep["provider"] not in providers:
            out.append(ep)
            providers.add(ep["provider"])
        if len(out) == n:
            return out
    raise AssertionError("the catalog has too few providers for this test")


def _day(back: int) -> str:
    return (LAST - timedelta(days=back)).isoformat()


def _rows(ep_id: str, per_day: dict[int, int], **kw) -> list[rankings.DayRow]:
    return [rankings.DayRow(endpoint_id=ep_id, day=_day(back), ok=ok, **kw) for back, ok in per_day.items()]


def _week(calls: int, *, back: int = 0) -> dict[int, int]:
    """`calls` spread over the seven days ending `back` days before LAST."""
    days = {back + i: calls // 7 for i in range(7)}
    days[back] += calls - sum(days.values())
    return days


def test_counts_windows_and_change():
    a, b = _sync_endpoints(2)
    rows = (_rows(a["id"], _week(2_000) | _week(1_000, back=7) | {20: 500})
            + _rows(b["id"], _week(700)))
    out = rankings.build(rows, store.load(), LAST)
    assert out["through"] == LAST.isoformat() and out["has_change"]
    week = {r["provider"]: r for r in out["tools"]["7d"]}
    assert week[a["provider"]]["calls"] == 2_000 and week[a["provider"]]["change"] == 100.0
    assert week[b["provider"]]["change"] == "new"
    month = {r["provider"]: r["calls"] for r in out["tools"]["30d"]}
    assert month[a["provider"]] == 3_500 and "change" not in out["tools"]["30d"][0]
    assert out["endpoints"]["7d"][0]["id"] == a["id"]


def test_today_and_unknown_endpoints_are_left_out():
    (a,) = _sync_endpoints(1)
    rows = (_rows(a["id"], {0: 10, -1: 999})
            + [rankings.DayRow(endpoint_id="nobody.knows.this", day=_day(0), ok=5_000)])
    out = rankings.build(rows, store.load(), LAST)
    assert [r["calls"] for r in out["tools"]["7d"]] == [10]


def test_no_change_without_a_full_week_before():
    (a,) = _sync_endpoints(1)
    out = rankings.build(_rows(a["id"], _week(5_000)), store.load(), LAST)
    assert not out["has_change"] and "change" not in out["tools"]["7d"][0]
    assert out["trending"] == {"providers": [], "endpoints": []}


def test_trending_floor_and_new_first():
    a, b, c = _sync_endpoints(3)
    rows = (_rows(a["id"], _week(3_000) | _week(1_000, back=7))     # tripled, over the floor
            + _rows(b["id"], _week(900) | _week(10, back=7))         # 90x, but under the floor
            + _rows(c["id"], _week(1_500) | {13: 0}))                # new this week
    out = rankings.build(rows, store.load(), LAST)
    ids = [r["id"] for r in out["trending"]["endpoints"]]
    assert ids == [c["id"], a["id"]]
    assert out["trending"]["endpoints"][1]["change"] == 200.0


def test_reliable_and_fastest_floors():
    a, b = _sync_endpoints(2)
    rows = (_rows(a["id"], {0: 95}, bad=5, latency_seen=40, latencies=[300] * 40)
            + _rows(b["id"], {0: 50}, bad=0, latency_seen=10, latencies=[50] * 10))
    out = rankings.build(rows, store.load(), LAST)
    assert [(r["provider"], r["ok_rate"]) for r in out["reliable"]] == [(a["provider"], 95.0)]
    assert [(r["id"], r["p50_ms"]) for r in out["fastest"]] == [(a["id"], 300)]


def test_async_jobs_are_not_judged_on_speed_or_reliability():
    ep = next(e for e in store.load().endpoints if store.browsable(e) and e.get("async")
              and not e.get("status") and not e.get("observed_from"))
    rows = _rows(ep["id"], {0: 500}, bad=0, latency_seen=500, latencies=[10] * 50)
    out = rankings.build(rows, store.load(), LAST)
    assert out["reliable"] == [] and out["fastest"] == []
    assert out["tools"]["7d"][0]["calls"] == 500


def test_by_category_shares_add_up():
    a, b = _sync_endpoints(2)
    out = rankings.build(_rows(a["id"], {0: 300}) + _rows(b["id"], {0: 100}), store.load(), LAST)
    for c in out["by_category"]:
        assert round(sum(p["share"] for p in c["providers"])) == 100


async def test_page_serves_the_boards_the_worker_saved(clients: AsyncClient):
    rankings_app.reset_cache()
    (a,) = _sync_endpoints(1)
    yesterday = (utcnow_naive().date() - timedelta(days=1)).isoformat()
    async with session_maker() as db:
        db.add(EndpointDayStat(endpoint_id=a["id"], day=yesterday, n=40, ok=40, latency_sample=[]))
        await db.commit()
    try:
        done = await rankings_app.refresh()
        assert done["through"] == yesterday and done["tools"] == 1
        r = await clients.get("/rankings")
        assert r.status_code == 200
        assert "{BASE}" not in r.text and "{DATA}" not in r.text
        (block,) = re.findall(r'<script type="application/json" id="rk-data">(.*?)</script>', r.text, re.S)
        data = json.loads(block)
        assert data["through"] == yesterday
        assert data["tools"]["7d"][0] == data["tools"]["7d"][0] | {"provider": a["provider"], "calls": 40}
        assert "endpoints" not in data   # the other boards load when the reader comes near them
        board = await clients.get("/rankings/data/endpoints")
        assert board.status_code == 200
        assert board.json()["endpoints"]["7d"][0]["id"] == a["id"]
        for name, key in (("trending", "trending"), ("categories", "by_category"),
                          ("reliable", "reliable"), ("fastest", "fastest"), ("headtohead", "head_to_head"), ("aimodels", "ai_models")):
            assert key in (await clients.get(f"/rankings/data/{name}")).json()
        assert (await clients.get("/rankings/data/nothing")).status_code == 404
        assert "/rankings<" in (await clients.get("/sitemap.xml")).text
        # search engines read the same first board as a ranked list
        (ld,) = [json.loads(b) for b in re.findall(r'<script type="application/ld\+json">(.*?)</script>', r.text, re.S)
                 if '"ItemList"' in b]
        assert ld["itemListElement"][0]["name"] == data["tools"]["7d"][0]["label"]
        assert ld["itemListElement"][0]["url"].endswith("/tools/" + a["provider"])
    finally:
        rankings_app.reset_cache()


async def test_before_the_first_worker_run_the_page_says_it_is_preparing(clients: AsyncClient):
    rankings_app.reset_cache()
    (a,) = _sync_endpoints(1)
    async with session_maker() as db:   # day stats alone are not read by the page
        db.add(EndpointDayStat(endpoint_id=a["id"], day=(utcnow_naive().date() - timedelta(days=1)).isoformat(),
                               n=40, ok=40, latency_sample=[]))
        await db.commit()
    try:
        r = await clients.get("/rankings")
        assert r.status_code == 200
        (block,) = re.findall(r'id="rk-data">(.*?)</script>', r.text, re.S)
        data = json.loads(block)
        assert data["pending"] is True and data["tools"] == {"7d": [], "30d": []}
        assert '"ItemList"' not in r.text
        assert (await clients.get("/rankings/data/fastest")).json() == {"fastest": []}
    finally:
        rankings_app.reset_cache()


async def test_a_new_run_replaces_the_saved_boards(clients: AsyncClient):   # clients: a fresh database
    (a, b) = _sync_endpoints(2)
    yesterday = (utcnow_naive().date() - timedelta(days=1)).isoformat()
    async with session_maker() as db:
        db.add(EndpointDayStat(endpoint_id=a["id"], day=yesterday, n=5, ok=5, latency_sample=[]))
        await db.commit()
    await rankings_app.refresh()
    async with session_maker() as db:
        db.add(EndpointDayStat(endpoint_id=b["id"], day=yesterday, n=9, ok=9, latency_sample=[]))
        await db.commit()
    await rankings_app.refresh()
    async with session_maker() as db:
        rows = (await db.execute(select(RankingsSnapshot))).scalars().all()
    assert len(rows) == 1
    assert [t["provider"] for t in rows[0].body["tools"]["7d"]] == [b["provider"], a["provider"]]


async def test_the_footers_link_to_the_rankings(clients: AsyncClient):
    for page in ("/rankings", "/showcase", "/aeo", "/jev", "/people-search"):
        assert 'href="/rankings"' in (await clients.get(page)).text, page


def test_the_chart_follows_each_window_and_its_leaders():
    a, b = _sync_endpoints(2)
    rows = _rows(a["id"], {0: 10, 20: 5_000}) + _rows(b["id"], {0: 90})
    out = rankings.build(rows, store.load(), LAST)
    week, month = out["chart"]["7d"], out["chart"]["30d"]
    assert len(week["days"]) == 7 and week["days"][-1] == LAST.isoformat()
    assert len(month["days"]) == 21   # only the days the buckets reach back to
    assert [s["provider"] for s in week["series"]] == [b["provider"], a["provider"]]
    assert [s["provider"] for s in month["series"]] == [a["provider"], b["provider"]]


def test_categories_are_ordered_by_this_weeks_calls():
    a, b = _sync_endpoints(2)
    out = rankings.build(_rows(a["id"], {0: 10}) + _rows(b["id"], {0: 900}), store.load(), LAST)
    cat = store.load().platforms
    busy = (cat.get(b["platform"]) or {}).get("category") or "Other"
    assert out["categories"][0] == busy
    assert [c["category"] for c in out["by_category"]] == out["categories"]


async def test_the_read_keeps_only_the_first_samples_of_each_day(clients: AsyncClient):
    """Reservoirs are most of the table: the page reads at most SAMPLES_PER_DAY values of each."""
    rankings_app.reset_cache()
    (a,) = _sync_endpoints(1)
    yesterday = (utcnow_naive().date() - timedelta(days=1)).isoformat()
    kept, dropped = [100] * rankings_app.SAMPLES_PER_DAY, [900] * 150
    async with session_maker() as db:
        db.add(EndpointDayStat(endpoint_id=a["id"], day=yesterday, n=200, ok=200,
                               latency_seen=200, latency_sample=kept + dropped))
        await db.commit()
    try:
        await rankings_app.refresh()
        fastest = (await clients.get("/rankings/data/fastest")).json()["fastest"]
        assert [(r["id"], r["p50_ms"]) for r in fastest] == [(a["id"], 100)]
    finally:
        rankings_app.reset_cache()


async def test_the_worker_saves_display_names(monkeypatch):
    """The cron is its own process: it must load the providers' display names before it builds."""
    from treg import worker
    from treg.application import rankings as app_rankings
    from treg.infra import db as infra_db

    monkeypatch.setattr(store, "_provider_label", lambda provider: provider)   # a fresh process
    seen = {}

    async def fake_refresh():
        seen["label"] = store.provider_label("pdl")
        return {}

    async def no_verify():
        return None

    monkeypatch.setattr(app_rankings, "refresh", fake_refresh)
    monkeypatch.setattr(infra_db, "verify_db", no_verify)
    assert await worker._rankings_refresh(None) == 0
    assert seen["label"] == "People Data Labs"


def test_head_to_head_compares_providers_within_one_job():
    by_cap: dict = {}
    for ep in store.load().endpoints:
        if store.browsable(ep) and not ep.get("async") and not ep.get("status") and ep.get("capability") \
                and not ep.get("observed_from"):
            by_cap.setdefault(ep["capability"], {}).setdefault(ep["provider"], ep)
    cap, eps = next((c, list(p.values())) for c, p in by_cap.items() if len(p) >= 3)
    fast, slow, thin = eps[:3]
    rows = (_rows(fast["id"], {0: 200}, bad=0, latency_seen=200, latencies=[150] * 50)
            + _rows(slow["id"], {0: 190}, bad=10, latency_seen=190, latencies=[900] * 50)
            + _rows(thin["id"], {0: 20}, bad=0, latency_seen=20, latencies=[50] * 20))   # under the floor
    (job,) = [j for j in rankings.build(rows, store.load(), LAST)["head_to_head"] if j["job"] == cap]
    assert [(p["provider"], p["ok_rate"], p["p50_ms"]) for p in job["providers"]] == [
        (fast["provider"], 100.0, 150), (slow["provider"], 95.0, 900)]
    assert job["providers"][1]["errors"] == 10 and job["title"]


async def test_a_board_missing_from_an_older_saved_row_is_empty(clients: AsyncClient):
    """After a deploy that adds a board, the saved row lacks it until the next cron run."""
    rankings_app.reset_cache()
    await rankings_app.refresh()
    async with session_maker() as db:
        row = await db.get(RankingsSnapshot, rankings_app.SNAPSHOT_ID)
        row.body = {k: v for k, v in row.body.items() if k != "head_to_head"}
        await db.commit()
    try:
        r = await clients.get("/rankings/data/headtohead")
        assert r.status_code == 200 and r.json() == {"head_to_head": []}
    finally:
        rankings_app.reset_cache()


def test_top_ai_models_adds_up_a_model_across_its_vendors():
    by_cap: dict = {}
    for ep in store.load().endpoints:
        if store.browsable(ep) and not ep.get("status") and ep["platform"] in rankings.AI_TYPES \
                and ep.get("capability") and not ep.get("observed_from"):
            by_cap.setdefault(ep["capability"], {}).setdefault(ep["provider"], ep)
    cap, (a, b) = next((c, list(p.values())[:2]) for c, p in by_cap.items() if len(p) >= 2)
    rows = _rows(a["id"], _week(700) | _week(100, back=7)) + _rows(b["id"], _week(300) | _week(100, back=7))
    board = rankings.build(rows, store.load(), LAST)["ai_models"]["7d"]
    (model,) = [m for m in board["models"] if m["model"] == cap.split(".")[1]]
    assert model["calls"] == 1_000 and model["change"] == 400.0
    assert model["vendors"][0] == store.provider_label(a["provider"]) and len(model["vendors"]) == 2
    assert set(board) == {"models"}


def test_a_model_has_one_key_and_a_readable_name_at_every_vendor():
    ep_slug = {"id": "x.slug", "provider": "replicate", "platform": "video-gen", "name": "minimax/hailuo-2.3-fast",
               "capability": "", "kind": "rest"}
    ep_said = dict(ep_slug, id="x.said", provider="openrouter", name="MiniMax: Hailuo 2.3 Fast")
    cat = store.load()
    for ep in (ep_slug, ep_said):
        cat.by_id[ep["id"]] = ep
    try:
        cat.endpoints.extend([ep_slug, ep_said])
        board = rankings.build(_rows("x.slug", {0: 30}) + _rows("x.said", {0: 70}), cat, LAST)["ai_models"]["7d"]
        (model,) = board["models"]
        assert model["calls"] == 100 and model["name"] == "Hailuo 2.3 Fast"
        assert sorted(model["vendors"]) == sorted([store.provider_label("openrouter"), store.provider_label("replicate")])
    finally:
        cat.endpoints.remove(ep_slug); cat.endpoints.remove(ep_said)
        cat.by_id.pop("x.slug"); cat.by_id.pop("x.said")


def test_a_model_that_moved_vendor_compares_with_all_its_earlier_calls():
    old = {"id": "x.old", "provider": "replicate", "platform": "image-gen", "name": "acme/pixel-1",
           "capability": "", "kind": "rest"}
    new = dict(old, id="x.new", provider="openrouter", name="Acme: Pixel 1")
    twin = dict(old, id="x.twin", name="acme/pixel-1")   # a second endpoint of the same model
    cat = store.load()
    added = [old, new, twin]
    for ep in added:
        cat.by_id[ep["id"]] = ep
    cat.endpoints.extend(added)
    try:
        rows = (_rows("x.old", _week(1_000, back=7)) + _rows("x.new", _week(1_500))
                + _rows("x.twin", _week(100)))
        board = rankings.build(rows, cat, LAST)["ai_models"]["7d"]
        (model,) = board["models"]
        assert model["calls"] == 1_600 and model["change"] == 60.0   # against all 1,000 the week before
    finally:
        for ep in added:
            cat.endpoints.remove(ep); cat.by_id.pop(ep["id"])


def test_a_decision_model_is_its_own_type_and_counts_in_all():
    (jev,) = [e for e in store.load().endpoints if e["platform"] == "ai-judge" and store.browsable(e)]
    board = rankings.build(_rows(jev["id"], {0: 50}), store.load(), LAST)["ai_models"]["7d"]
    (model,) = board["models"]
    assert model["type"] == "Decision" and model["calls"] == 50
