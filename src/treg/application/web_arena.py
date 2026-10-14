"""Private Web Arena runs. Each provider leg is an ordinary direct treg call."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from datetime import timedelta
from urllib.parse import urlencode

from httpx import QueryParams
from sqlalchemy import delete, update
from sqlmodel import select

from ..config import get_settings
from ..domain import money, web_arena as rules
from ..domain.catalog.routing.contracts import declared_miss
from ..domain.catalog.routing.plan import _used_keys, candidates_for, unscoped
from ..domain.catalog import store as catalog_store
from ..infra.db import session_maker
from ..models import LedgerEntry, User, WebArenaRun
from ..timeutil import utcnow_naive as now
from . import arena
from . import web_arena_quality
from .call import route, service
from .call.resolve import _marketplace_pricing
from .call.types import CallFailure, CallInput, CallerSnapshot

log = logging.getLogger(__name__)
_owners: dict[str, asyncio.Task] = {}
MAX_RESULT_BYTES = 256_000
APIFY_MAPS_RESULT_BYTES = 800_000
RUN_SECONDS = 180
FIXED_PAGE_SEARCH = {"branddev.web.search", "tinyfish.web.search", "tinyfish.web.search.news",
                     "crawl4ai.web.search"}
NEWS_ENDPOINTS = {"tinyfish.web.search.news", "search1api.web.news", "exa.web.search.news",
                  "anyapi.google.serp.news", "serper.google.serp.news", "cloro.google.serp.news",
                  "serpapi.x.google-news", "dataforseo.x.serp-google-news-live-advanced",
                  "litescrape.google.serp.news", "tavily.web.search.news"}
PAPER_ENDPOINTS = {"exa.web.search.publications", "tinyfish.web.search.publications",
                   "serper.google.serp.scholar"}
YOUTUBE_ENDPOINTS = {"justoneapi.x.youtube-search-v1", "serpapi.youtube.search.videos",
                     "tikhub.youtube.search.videos"}
MAPS_ENDPOINTS = {"apify.google.serp.maps", "dataforseo.x.serp-google-maps-live-advanced",
                  "serpapi.x.google-maps"}
UNBOUNDED_SITEMAP = {"search1api.web.sitemap"}


def _supports_result_limit(task: str, endpoint_id: str, adapter) -> bool:
    return task not in {"search", "news", "papers", "sitemap"} or "limit" in _used_keys(adapter) or (
        task in {"search", "news"} and endpoint_id in FIXED_PAGE_SEARCH) or (
        task == "news" and endpoint_id in NEWS_ENDPOINTS) or (
        task == "papers" and endpoint_id in {"tinyfish.web.search.publications", "serper.google.serp.scholar"}) or (
        task == "sitemap" and endpoint_id in UNBOUNDED_SITEMAP)


def _capabilities(task: str) -> tuple[str, ...]:
    if task == "news":
        return ("web.search.news", "google.serp.news")
    if task == "papers":
        return ("web.search.publications", "google.serp.scholar")
    return (rules.TASKS[task],)


def _in_lineup(task: str, endpoint_id: str) -> bool:
    return task not in {"news", "papers", "youtube", "maps"} or endpoint_id in {
        "news": NEWS_ENDPOINTS, "papers": PAPER_ENDPOINTS,
        "youtube": YOUTUBE_ENDPOINTS, "maps": MAPS_ENDPOINTS,
    }[task]


def enabled() -> bool:
    return get_settings().web_arena_enabled


def _check_enabled():
    if not enabled():
        raise rules.WebArenaError("Web Arena is not open yet.", 404)


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


async def _owned(db, run_id, caller):
    row = await db.get(WebArenaRun, run_id)
    if not row or row.org_id != caller.org_id or row.user_id != caller.user.id or row.expires_at < now():
        raise rules.WebArenaError("Run not found.", 404)
    return row


async def _prune(db):
    ids = list((await db.execute(select(WebArenaRun.id).where(WebArenaRun.expires_at < now()).limit(50))).scalars())
    if ids:
        await db.execute(delete(WebArenaRun).where(WebArenaRun.id.in_(ids)))


def tasks(*, _internal: bool = False):
    if not _internal:
        _check_enabled()
    cat = catalog_store.load()
    result = []
    for task, label in (("search", "Web Search"), ("news", "News Search"),
                        ("papers", "Paper Search"), ("youtube", "YouTube Search"),
                        ("maps", "Maps Search"), ("fetch", "Web Fetch"),
                        ("sitemap", "Sitemap"), ("brand", "Brand")):
        previews = []
        if task != "brand":
            identity = rules.input_for(task, "coffee shops in Austin TX" if task == "maps" else
                                       "example query" if task in {"search", "news", "papers", "youtube"}
                                       else "https://example.com")
            candidates = []
            for capability in _capabilities(task):
                contract = cat.contracts[capability]
                found, _ = candidates_for(contract, cat.for_capability(capability), cat.adapters, identity)
                candidates.extend((contract, ep, adapter, variant) for ep, adapter, variant in found)
            seen = set()
            for contract, ep, adapter, variant in candidates:
                provider = ep["provider"]
                if not _in_lineup(task, ep["id"]):
                    continue
                if task == "search" and provider == "valyu":
                    continue
                if (provider in seen or provider == "treg" or ep.get("async") or ".bulk" in ep["id"]
                        or unscoped(adapter, contract, identity)):
                    continue
                if not _supports_result_limit(task, ep["id"], adapter):
                    continue
                seen.add(provider)
                cost = cat.cost_view(ep.get("cost"), provider)
                estimate = None
                if cost and cost.get("usd") is not None:
                    upstream_query, body = adapter.to_upstream(identity, variant)
                    estimate = money.with_margin(_marketplace_pricing(provider, ep["id"], cost,
                        QueryParams(upstream_query), json.dumps(body).encode())[0])
                previews.append({"provider": provider, "endpoint_id": ep["id"],
                                 "catalog_price_usd": cost.get("usd") if cost else None,
                                 "catalog_estimate_micro": estimate,
                                 "price_unit": (ep.get("cost") or {}).get("unit"),
                                 "price_type": (ep.get("cost") or {}).get("type")})
        result.append({"id": task, "label": label, "enabled": task != "brand",
                       "provider_previews": sorted(previews, key=lambda p: p["provider"])})
    return result


async def quote(caller, *, task: str, value: str, query: str = "", mode: str = "battle", providers: list[str] | None = None,
                jev: bool = True):
    _check_enabled()
    if caller.org.demo or caller.org.public_demo:
        raise rules.WebArenaError("Sign in with a regular team to run Web Arena.", 403)
    if mode not in {"battle", "waterfall"}:
        raise rules.WebArenaError("Choose Battle or Waterfall.")
    if task in {"sitemap", "maps"}:
        jev = False
    identity = rules.input_for(task, value, query)
    # Check the comparison limit after planning. Search1API Sitemap is the explicit
    # unbounded exception; its returned links are capped before comparison.
    plans = [await route.build_plan({"id": "web-arena." + task, "capability": capability}, identity, caller,
                                    route.RouteOptions(strict_filters=False)) for capability in _capabilities(task)]
    cat = catalog_store.load()
    chosen, dropped, seen = [], [item for plan in plans for item in plan.dropped], set()
    requested = set(providers) if providers is not None else None
    if requested is not None and (not requested or len(requested) > 30):
        raise rules.WebArenaError("Select 1 to 30 providers.")
    for plan, c in ((plan, c) for plan in plans for c in plan.candidates):
        ep, adapter = c.endpoint, c.adapter
        provider = ep["provider"]
        if not _in_lineup(task, ep["id"]):
            continue
        if task == "search" and provider == "valyu":
            continue
        if provider in seen or provider == "treg" or ep.get("async") or ".bulk" in ep["id"]:
            continue
        if c.exhausted or c.note:
            dropped.append({"endpoint_id": ep["id"], "why": "direct provider capacity is unavailable"})
            continue
        if requested is not None and provider not in requested:
            continue
        # Counted providers send the limit upstream. TinyFish and Crawl4AI instead use their
        # first page, and the run compares only its first ten links.
        if not _supports_result_limit(task, ep["id"], adapter):
            dropped.append({"endpoint_id": ep["id"], "why": "cannot enforce the result limit"})
            continue
        upstream_query, body = adapter.to_upstream(plan.identity, c.variant)
        cv = cat.cost_view(ep.get("cost"), provider)
        if c.tier == "platform" and (not cv or cv.get("usd") is None):
            dropped.append({"endpoint_id": ep["id"], "why": "price unavailable"})
            continue
        estimate = money.with_margin(_marketplace_pricing(provider, ep["id"], cv, QueryParams(upstream_query),
            json.dumps(body).encode())[0]) if c.tier == "platform" else 0
        if estimate > 10_000_000:
            dropped.append({"endpoint_id": ep["id"], "why": "above the per-provider limit"})
            continue
        seen.add(provider)
        chosen.append({"id": uuid.uuid4().hex, "provider": provider, "endpoint_id": ep["id"],
                       "tier": c.tier, "estimate_micro": estimate, "query": upstream_query, "body": body,
                       "method": ep["method"], "state": "queued", "charged_micro": None,
                       "endpoint_hash": _hash(ep), "adapter_hash": _hash(adapter.__dict__),
                       "price_type": (ep.get("cost") or {}).get("type")})
    if requested is not None and requested - seen:
        raise rules.WebArenaError("Some selected providers cannot use this input. Refresh your choice.", 409)
    if not chosen:
        raise rules.WebArenaError("No eligible provider is available for this input.", 409)
    chosen.sort(key=lambda a: (a["estimate_micro"], a["provider"]))
    estimate = sum(a["estimate_micro"] for a in chosen)
    required = estimate if mode == "battle" else chosen[0]["estimate_micro"]
    limit_exceeded = mode == "battle" and estimate > 10_000_000
    payload = {"input": value.strip(), "query": query.strip() if task == "sitemap" else "",
               "identity": identity, "attempts": chosen, "jev": jev,
               "stop_reason": "", "dropped": dropped, "quality_state": "pending" if jev else "off"}
    async with session_maker() as db:
        await _prune(db)
        stale = list((await db.execute(select(WebArenaRun.id).where(WebArenaRun.user_id == caller.user.id,
            WebArenaRun.state == "quoted").order_by(WebArenaRun.created_at.desc(), WebArenaRun.id.desc())
            .offset(99))).scalars())
        if stale:
            await db.execute(delete(WebArenaRun).where(WebArenaRun.id.in_(stale)))
        balance = await money.balance_of(db, caller.org_id)
        row = WebArenaRun(id=uuid.uuid4().hex, org_id=caller.org_id, user_id=caller.user.id,
                          task=task, mode=mode, state="quoted", payload=arena._pack(payload),
                          deadline_at=now() + timedelta(minutes=5),
                          expires_at=now() + timedelta(days=rules.RETENTION_DAYS))
        db.add(row)
        await db.commit()
    return {"id": row.id, "task": task, "mode": mode, "providers": [
        {k: a[k] for k in ("provider", "endpoint_id", "tier", "estimate_micro", "price_type")} for a in chosen],
        "estimate_micro": estimate, "required_micro": required, "balance_micro": balance,
        "affordable": balance >= required and not limit_exceeded, "limit_exceeded": limit_exceeded,
        "dropped": dropped, "expires_at": row.deadline_at.isoformat() + "Z",
        "jev": jev, "jev_cost": "Covered by treg; not part of the provider quote."}


async def start(caller, run_id, client, client_ip):
    _check_enabled()
    async with session_maker() as db:
        await db.execute(update(User).where(User.id == caller.user.id).values(token_version=User.token_version))
        row = await _owned(db, run_id, caller)
        if row.state != "quoted":
            return {"id": row.id, "state": row.state}
        if row.deadline_at < now():
            raise rules.WebArenaError("The quote expired. Get a new quote.", 409)
        payload = arena._unpack(row.payload)
        cat = catalog_store.load()
        for a in payload["attempts"]:
            ep, ad = cat.by_id.get(a["endpoint_id"]), cat.adapters.get(a["endpoint_id"])
            if not ep or not ad or _hash(ep) != a["endpoint_hash"] or _hash(ad.__dict__) != a["adapter_hash"]:
                raise rules.WebArenaError("The catalog changed. Get a new quote.", 409)
        required = sum(a["estimate_micro"] for a in payload["attempts"]) if row.mode == "battle" else payload["attempts"][0]["estimate_micro"]
        if row.mode == "battle" and required > 10_000_000:
            raise rules.WebArenaError("This Battle exceeds the $10 run limit. Select fewer providers.", 409)
        if await money.balance_of(db, caller.org_id) < required:
            raise rules.WebArenaError("Not enough team credits.", 402)
        active = (await db.execute(select(WebArenaRun.id).where(WebArenaRun.user_id == caller.user.id,
            WebArenaRun.state == "running", WebArenaRun.deadline_at > now()).limit(3))).all()
        if len(active) >= 3:
            raise rules.WebArenaError("Finish an active run first.", 429)
        row.state = "running"
        row.created_at = now()
        row.deadline_at = now() + timedelta(seconds=RUN_SECONDS + 30)
        db.add(row)
        await db.commit()
        mode, task = row.mode, row.task
    worker = asyncio.create_task(_run(run_id, task, mode, payload, CallerSnapshot.capture(caller), client, client_ip))
    _owners[run_id] = worker
    worker.add_done_callback(lambda t: _owners.pop(run_id, None))
    return {"id": run_id, "state": "running"}


async def _save(run_id, payload, state=None):
    async with session_maker() as db:
        await db.execute(update(WebArenaRun).where(WebArenaRun.id == run_id)
                         .values(cancel_requested=WebArenaRun.cancel_requested))
        row = await db.get(WebArenaRun, run_id)
        if not row or row.state != "running":
            return False
        row.payload = arena._pack(payload)
        if state:
            row.state = "cancelled" if row.cancel_requested else state
        db.add(row)
        await db.commit()
        return not row.cancel_requested


async def _run(run_id, task, mode, payload, snapshot, client, client_ip):
    cat = catalog_store.load()
    contract = cat.contracts[rules.TASKS[task]]
    lock = asyncio.Lock()

    async def persist():
        async with lock:
            return await _save(run_id, payload)

    async def leg(a):
        response = None
        began = time.monotonic()
        try:
            current = await arena._fresh_caller(snapshot)
            a["state"] = "running"
            if not await persist():
                a["state"] = "cancelled"
                return
            data = json.dumps(a["body"]).encode() if a["method"] in {"POST", "PUT", "PATCH"} else b""
            headers = ((b"content-type", b"application/json"), (b"content-length", str(len(data)).encode()),
                       (b"x-treg-client", b"web-arena"), (b"cache-control", b"no-cache"),
                       (b"x-treg-route-max-cost", f"{a['estimate_micro'] / 1e6:.6f}".encode()))
            context = service.create_call_context(CallInput(method=a["method"], raw_rest=a["endpoint_id"],
                raw_headers=headers, query_items=tuple(a["query"].items()), raw_query=urlencode(a["query"]),
                body=route._Bytes(data), caller=current, client_ip=client_ip))
            a["call_ref"] = context.call_ref
            await persist()
            async with asyncio.timeout(RUN_SECONDS):
                response = await service.execute_call(context, client)
                buf = bytearray()
                oversized = False
                result_limit = (APIFY_MAPS_RESULT_BYTES if a["endpoint_id"] == "apify.google.serp.maps"
                                else MAX_RESULT_BYTES)
                async for chunk in response.body_stream:
                    if len(buf) + len(chunk) <= result_limit and not oversized:
                        buf.extend(chunk)
                    else:
                        oversized = True
                a["charged_micro"] = context.cost_micro if context.cost_micro is not None else int(route._header(response, "X-Treg-Cost-Micro") or 0)
                try:
                    doc = json.loads(buf) if not oversized else None
                except (ValueError, UnicodeDecodeError):
                    doc = None
                ep, ad = cat.by_id[a["endpoint_id"]], cat.adapters[a["endpoint_id"]]
                if declared_miss(ep, response.status, doc):
                    outcome, output = "miss", {}
                elif not 200 <= response.status < 300 or not isinstance(doc, (dict, list)):
                    outcome, output = "error", {}
                else:
                    output = ad.from_upstream(doc)
                    if (a["endpoint_id"] in {"tinyfish.web.search", "tinyfish.web.search.news",
                                               "crawl4ai.web.search"} or
                            task in {"news", "papers", "youtube", "maps"} or
                            a["endpoint_id"] in UNBOUNDED_SITEMAP) and isinstance(output.get("results"), list):
                        # Compare only the requested first page when upstream cannot accept a count.
                        output["results"] = output["results"][:payload["identity"]["limit"]]
                        output["count"] = len(output["results"])
                    if task in {"youtube", "maps"}:
                        key = "videos" if task == "youtube" else "places"
                        if isinstance(output.get(key), list):
                            if a["endpoint_id"] == "justoneapi.x.youtube-search-v1":
                                videos = []
                                for row in output[key]:
                                    if not isinstance(row, dict) or row.get("type", "video") != "video":
                                        continue
                                    video_id = row.get("video_id") or row.get("id")
                                    if not isinstance(video_id, str) or not video_id:
                                        continue
                                    video = dict(row, video_id=video_id)
                                    video.setdefault("url", "https://www.youtube.com/watch?v=" + video_id)
                                    videos.append(video)
                                    if len(videos) == 10:
                                        break
                                output[key] = videos
                            elif a["endpoint_id"] == "apify.google.serp.maps":
                                fields = ("place_id", "name", "title", "address", "rating", "google_maps_url")
                                output[key] = [{field: row[field] for field in fields if row.get(field) is not None}
                                               for row in output[key][:10] if isinstance(row, dict)]
                                output["count"] = len(output[key])
                            else:
                                output[key] = output[key][:10]
                    outcome = "miss" if ad.is_miss(doc) or any(output.get(k) in (None, "", [], {}) for k in contract.required_output) else "hit"
                a["state"] = outcome
                a["output"] = output if outcome == "hit" else {}
                a["status"] = response.status
                a["detail"] = "Response exceeded the size limit." if oversized else ""
                if task == "news" and a["endpoint_id"] == "tinyfish.web.search.news" and response.status == 429:
                    retry = route._header(response, "Retry-After")
                    a["detail"] = (f"Try again in about {retry} seconds." if retry and retry.isdecimal()
                                   and 0 < int(retry) <= 3600 else "Try again shortly.")
                if outcome == "hit" and task == "sitemap":
                    a["quality"] = rules.url_rows(rules.result_items(task, output), payload["input"])
                if outcome == "hit" and task == "fetch":
                    value = rules.fetch_text(output)
                    a["quality"] = {"tokens": len(re.findall(r"\w+|[^\w\s]", value, re.UNICODE)),
                                    "tokenizer": "word-or-symbol-v1", "relative_coverage": None,
                                    "tokens_per_kept_fact": None}
                if outcome == "hit" and not rules.valid_result(task, output, payload["input"]):
                    a["state"] = "miss"
        except CallFailure as exc:
            a.update(state="error", detail=exc.kind, status=exc.status_code,
                     charged_micro=context.cost_micro if "context" in locals() else None)
        except TimeoutError:
            a.update(state="timeout", detail="Provider deadline exceeded.", charged_micro=context.cost_micro if "context" in locals() else None)
        except asyncio.CancelledError:
            a.update(state="cancelled", detail="Run stopped.", charged_micro=context.cost_micro if "context" in locals() else None)
            raise
        except Exception:
            log.exception("Web Arena provider attempt failed")
            a.update(state="error", detail="Provider result could not be completed.", charged_micro=context.cost_micro if "context" in locals() else None)
        finally:
            if response is not None:
                await response.close()
            a["duration_ms"] = round((time.monotonic() - began) * 1000)
            await persist()
        # The provider card is saved before any optional Jev request. An unavailable
        # check does not turn the provider's answer into an error or a zero score.
        if task in {"search", "news", "papers", "youtube"} and payload["jev"] and a["state"] == "hit":
            try:
                a["quality"] = await web_arena_quality.search(payload["input"], a["output"], snapshot.user.id, task=task)
            except Exception:
                log.exception("Web Arena search check failed")
                a["quality"] = {"state": "unknown", "estimated_match": None,
                                "detail": "The quality check is unavailable."}
            await persist()

    try:
        if mode == "battle":
            sem = asyncio.Semaphore(4)
            async def bounded(a):
                async with sem:
                    await leg(a)
            async with asyncio.TaskGroup() as group:
                for a in payload["attempts"]:
                    group.create_task(bounded(a))
            payload["stop_reason"] = "All selected providers finished."
        else:
            quoted_spend = 0
            for a in payload["attempts"]:
                if not await persist():
                    break
                if quoted_spend + a["estimate_micro"] > 10_000_000:
                    payload["stop_reason"] = "Stopped at the $10 run limit."
                    break
                quoted_spend += a["estimate_micro"]
                await leg(a)
                # Cheapest first: the first provider that returns a result ends the Waterfall.
                # A quality check still scores that result; it never decides when to stop.
                if a["state"] == "hit":
                    payload["stop_reason"] = "Stopped at the first useful result."
                    break
                if a["state"] in {"timeout", "error"} and a.get("charged_micro") is None:
                    payload["stop_reason"] = "Stopped because the provider fee is not known yet."
                    break
            if not payload["stop_reason"]:
                payload["stop_reason"] = "No provider met the stop check."
            for a in payload["attempts"]:
                if a["state"] == "queued":
                    a.update(state="not_attempted", charged_micro=0)
        if task == "fetch" and payload["jev"]:
            try:
                await web_arena_quality.fetch(payload["attempts"], snapshot.user.id)
            except Exception:
                log.exception("Web Arena fetch check failed")
            await persist()
        payload["quality_state"] = "checked" if any((a.get("quality") or {}).get("state") == "checked" for a in payload["attempts"]) else "unknown" if payload["jev"] else "off"
        await _save(run_id, payload, "completed")
    except asyncio.CancelledError:
        for a in payload["attempts"]:
            if a["state"] == "queued":
                a.update(state="not_attempted", charged_micro=0)
        await _save(run_id, payload, "cancelled")
        raise
    except Exception:
        log.exception("Web Arena run failed")
        await _save(run_id, payload, "interrupted")


async def get_run(caller, run_id):
    _check_enabled()
    async with session_maker() as db:
        row = await _owned(db, run_id, caller)
        payload = arena._unpack(row.payload)
        if row.state == "running" and row.deadline_at < now():
            row.state = "interrupted"
            for a in payload["attempts"]:
                if a["state"] in {"queued", "running"}:
                    a["state"] = "not_attempted" if a["state"] == "queued" else "interrupted"
            row.payload = arena._pack(payload)
            db.add(row)
            await db.commit()
        if row.state in rules.TERMINAL:
            uncertain = [a for a in payload["attempts"] if a.get("charged_micro") is None]
            refs = [a["call_ref"] for a in uncertain if a.get("call_ref")]
            entries = (await db.execute(select(LedgerEntry).where(LedgerEntry.org_id == caller.org_id,
                LedgerEntry.call_id.in_(refs), LedgerEntry.kind.in_(["settle", "release"])))).scalars().all() if refs else []
            final = {e.call_id: -e.amount_micro if e.kind == "settle" else 0 for e in entries}
            changed = False
            for a in uncertain:
                if a.get("call_ref") in final or a["tier"] != "platform":
                    a["charged_micro"] = final.get(a.get("call_ref"), 0)
                    changed = True
            if changed:
                row.payload = arena._pack(payload)
                db.add(row)
                await db.commit()
        return {"id": row.id, "task": row.task, "mode": row.mode, "state": row.state,
                "created_at": row.created_at.isoformat() + "Z", **payload}


async def history(caller, limit=30):
    _check_enabled()
    async with session_maker() as db:
        await _prune(db)
        rows = (await db.execute(select(WebArenaRun).where(WebArenaRun.org_id == caller.org_id,
            WebArenaRun.user_id == caller.user.id, WebArenaRun.state != "quoted",
            WebArenaRun.expires_at > now()).order_by(WebArenaRun.created_at.desc()).limit(limit))).scalars().all()
        await db.commit()
    return [{"id": r.id, "task": r.task, "mode": r.mode, "state": r.state,
             "created_at": r.created_at.isoformat() + "Z"} for r in rows]


async def cancel(caller, run_id):
    _check_enabled()
    async with session_maker() as db:
        row = await _owned(db, run_id, caller)
        if row.state == "running":
            row.cancel_requested = True
            db.add(row)
            await db.commit()
    owner = _owners.get(run_id)
    if owner:
        owner.cancel()
    return {"id": run_id, "state": "stopping" if owner else row.state}


async def rate(caller, run_id, attempt_id, value):
    _check_enabled()
    if value not in {"up", "down"}:
        raise rules.WebArenaError("Choose up or down.")
    async with session_maker() as db:
        row = await _owned(db, run_id, caller)
        payload = arena._unpack(row.payload)
        attempt = next((a for a in payload["attempts"] if a["id"] == attempt_id and a["state"] in {"hit", "miss", "error"}), None)
        if not attempt:
            raise rules.WebArenaError("Choose a returned provider result.")
        attempt["rating"] = value
        row.payload = arena._pack(payload)
        db.add(row)
        await db.commit()
    return {"rating": value}
