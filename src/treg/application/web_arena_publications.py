"""Content-free live Web Arena leaderboard totals."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import mean, median

from cryptography.fernet import InvalidToken
from sqlalchemy import delete
from sqlmodel import select

from ..config import get_settings
from ..domain import web_arena_scores as scores
from ..domain.catalog import store as catalog_store
from ..infra.db import session_maker
from ..models import WebArenaPublication, WebArenaRun
from ..timeutil import utcnow_naive as now
from . import arena, web_arena_calls

LIVE_REFRESH_SECONDS = 1800


async def published(kind: str) -> dict:
    async with session_maker() as db:
        row = (await db.execute(select(WebArenaPublication).where(WebArenaPublication.kind == kind)
             .order_by(WebArenaPublication.created_at.desc()).limit(1))).scalar_one_or_none()
    return row.payload if row else {"status": "warming", "task_results": {}}


async def _live_rows(observed_since: datetime | None = None):
    cutoff = now() - timedelta(days=30)
    if observed_since is not None:
        cutoff = max(cutoff, observed_since)
    async with session_maker() as db:
        rows = (await db.execute(select(WebArenaRun).where(WebArenaRun.mode.in_(["battle", "waterfall"]),
            WebArenaRun.state == "completed", WebArenaRun.created_at >= cutoff,
            WebArenaRun.expires_at > now()).order_by(WebArenaRun.created_at.desc()).limit(10_000))).scalars().all()
    return rows


async def live_now() -> dict:
    """Compute content-free totals directly for local development."""
    observed_since = await web_arena_calls.coverage_start()
    rows = await _live_rows(observed_since)
    traffic = await web_arena_calls.local_snapshot()
    readable = []
    unreadable = 0
    for row in rows:
        try:
            arena._unpack(row.payload)
        except InvalidToken:
            unreadable += 1
        else:
            readable.append(row)
    if not unreadable:
        return summarize_live(rows, catalog_store.load(), traffic, observed_since=observed_since)

    # A local preview may outlive its encryption key. Older ciphertext must not
    # hide newer, readable Battles; retain saved totals only for tasks with no
    # readable runs. Production refreshes still fail on an unreadable payload.
    snapshot = await published("live")
    if not readable:
        if snapshot.get("status") != "live":
            raise InvalidToken
        return {**snapshot, "source": "Last saved Battle totals; older local runs could not be read.",
                "stale": True}
    doc = summarize_live(readable, catalog_store.load(), traffic, observed_since=observed_since)
    saved_tasks = snapshot.get("task_results", {}) if snapshot.get("status") == "live" else {}
    stale_tasks = sorted(set(saved_tasks) - set(doc["task_results"]))
    for task in stale_tasks:
        doc["task_results"][task] = saved_tasks[task]
    return {**doc, "source": "Readable Battle totals; older local runs could not be read.",
            "partial": True, "stale_tasks": stale_tasks}


async def refresh_live():
    """Worker saves content-free totals for the hosted page to read as one small row."""
    collected = await web_arena_calls.collect()
    if not collected["caught_up"]:
        return {"skipped": True, "reason": "call backlog", **collected}
    observed_since = await web_arena_calls.coverage_start()
    rows = await _live_rows(observed_since)
    doc = summarize_live(rows, catalog_store.load(), await web_arena_calls.snapshot(),
                         observed_since=observed_since)
    async with session_maker() as db:
        row = await db.get(WebArenaPublication, "live:current")
        if row:
            row.payload = doc
            row.created_at = now()
        else:
            row = WebArenaPublication(id="live:current", kind="live", version="v1", payload=doc)
        db.add(row)
        await db.commit()
    return {"tasks": list(doc["task_results"]), "arena_runs": len(rows), **collected}


async def seed_recent():
    """Resume a bounded seed pass; swap live totals only after every endpoint is folded."""
    if get_settings().web_arena_enabled:
        raise ValueError("Disable Web Arena before replacing its initial observation totals.")
    await web_arena_calls.start_recent_seed()
    try:
        async with asyncio.timeout(web_arena_calls.SEED_MAX_SECONDS + 10):
            progress = await web_arena_calls.advance_recent_seed()
    except TimeoutError:
        return {"seeded": False, "reason": "time limit; progress saved", "retry": True,
                **await web_arena_calls.recent_seed_status()}
    if not progress["ready"]:
        return {"seeded": False, "reason": "more matching calls remain", "retry": True,
                **progress}
    async with session_maker() as db:
        seed = await web_arena_calls.finish_recent_seed(db)
        await db.execute(delete(WebArenaPublication).where(WebArenaPublication.id == "live:current"))
        await db.commit()
    publication = await refresh_live()
    return {"seeded": True, **seed, "publication": publication}


async def refresh_live_if_due():
    """Keep the scheduled pass cheap between full rolling-window refreshes."""
    snapshot = await published("live")
    if snapshot.get("status") == "live" and isinstance(snapshot.get("updated_at"), str):
        try:
            updated_at = datetime.fromisoformat(snapshot["updated_at"].removesuffix("Z"))
        except ValueError:
            pass
        else:
            if 0 <= (now() - updated_at).total_seconds() < LIVE_REFRESH_SECONDS:
                return {"skipped": True, "reason": "fresh", "updated_at": snapshot["updated_at"]}
    return await refresh_live()


def summarize_live(rows, catalog, traffic=None, *, observed_since: datetime | None = None):
    """Join direct-call facts to checked Arena quality, never double-counting Battle calls."""
    by_task: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(lambda: {
        "runs": 0, "success": 0, "times": [], "metric_values": [], "efficiency_values": [],
        "comparable": 0, "wins": 0}))
    seen_inputs = set()
    seen_quality: dict[str, set] = defaultdict(set)
    for row in rows:
        payload = arena._unpack(row.payload)
        attempts = payload.get("attempts") or []
        values = scores.winner_values(row.task, attempts) if getattr(row, "mode", "battle") == "battle" else {}
        best = max(values.values()) if values else None
        for a in attempts:
            if a.get("state") not in {"hit", "miss", "error", "timeout"}:
                continue
            key = (row.task, a["provider"], payload.get("input"),
                   payload.get("query", "") if row.task == "sitemap" else "")
            slot = by_task[row.task][a["provider"]]
            if key not in seen_inputs:
                seen_inputs.add(key)
                slot["runs"] += 1
                slot["success"] += a["state"] == "hit"
                if isinstance(a.get("duration_ms"), int):
                    slot["times"].append(a["duration_ms"])
            quality = a.get("quality") or {}
            metric = (quality.get("estimated_match") if row.task in {"search", "news", "papers", "youtube"} and quality.get("state") == "checked"
                      else quality.get("relative_coverage") if row.task == "fetch" and quality.get("state") == "checked"
                      else quality.get("coverage_percent") if row.task == "sitemap" else None)
            if (key not in seen_quality["metric"] and isinstance(metric, (int, float))
                    and not isinstance(metric, bool) and 0 <= metric <= 100):
                slot["metric_values"].append(metric)
                seen_quality["metric"].add(key)
            efficiency = quality.get("token_efficiency") if row.task == "fetch" and quality.get("state") == "checked" else None
            if (key not in seen_quality["efficiency"] and isinstance(efficiency, (int, float))
                    and not isinstance(efficiency, bool) and 0 <= efficiency <= 100):
                slot["efficiency_values"].append(efficiency)
                seen_quality["efficiency"].add(key)
            if key not in seen_quality["win"] and a["provider"] in values:
                slot["comparable"] += 1
                slot["wins"] += values[a["provider"]] == best
                seen_quality["win"].add(key)
    result = {}
    for task, providers in (traffic or {}).items():
        for provider in providers:
            by_task[task][provider]
    lineup = {(task, provider): endpoint_id
              for endpoint_id, (task, provider) in web_arena_calls.endpoint_tasks().items()}
    for task, providers in by_task.items():
        output = []
        for provider, stats in providers.items():
            current = catalog.by_id.get(lineup.get((task, provider)))
            cv = catalog.cost_view(current.get("cost"), provider) if current else None
            observed = (traffic or {}).get(task, {}).get(provider)
            n = observed["runs"] if observed is not None else stats["runs"]
            comparable = stats["comparable"]
            metric_values = stats["metric_values"]
            output.append({"provider": provider, "runs": n,
                "hit_samples": observed["hit_samples"] if observed is not None else n,
                "time_samples": observed["time_samples"] if observed is not None else len(stats["times"]),
                "success_rate": observed["success_rate"] if observed is not None else
                    round(100 * stats["success"] / n, 1) if n else None,
                "average_provider_ms": observed["average_provider_ms"] if observed is not None else
                    round(mean(stats["times"])) if stats["times"] else None,
                "median_provider_ms": observed["median_provider_ms"] if observed is not None else
                    round(median(stats["times"])) if stats["times"] else None,
                "metric_sample_count": len(metric_values),
                "metric_percent": round(mean(metric_values), 1) if len(metric_values) >= 20 else None,
                "token_efficiency_sample_count": len(stats["efficiency_values"]),
                "token_efficiency_percent": round(mean(stats["efficiency_values"]), 1)
                    if len(stats["efficiency_values"]) >= 20 else None,
                "quality_sample_count": comparable,
                "quality_win_rate": round(100 * stats["wins"] / comparable, 1) if comparable >= 20 else None,
                "current_catalog_price_usd": cv.get("usd") if cv else None,
                "price_unit": (current.get("cost") or {}).get("unit") if current else None})
        result[task] = sorted(output, key=lambda x: (-x["runs"], x["provider"]))
    doc = {"status": "live", "source": "Direct treg calls for hit rate and time; checked Web Arena runs for quality",
           "window_days": 30, "updated_at": now().isoformat() + "Z", "task_results": result,
           "observed_since": observed_since.isoformat() + "Z" if observed_since else None,
           "minimum_comparable_runs": 20, "minimum_hit_samples": web_arena_calls.MIN_HIT_SAMPLES,
           "filters": {"cached": "excluded", "treg_refusals": "excluded", "quality": "checked Arena runs"}}
    return doc
