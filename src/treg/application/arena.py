"""Paid enrichment comparisons. Each leg is an ordinary governed, metered call.

Runs are claimed once in the database. An interrupted owner is never retried automatically:
that would risk buying the same data twice after an ambiguous upstream completion.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time
import uuid
from dataclasses import replace
from datetime import timedelta
from urllib.parse import urlencode

from sqlalchemy import delete, or_, update
from sqlmodel import select

from .. import analytics, crypto
from ..domain import arena as rules, money
from ..domain.catalog import store as catalog_store
from ..domain.catalog.routing.paths import country_name
from ..domain.identity.access import Caller
from ..infra.db import session_maker
from ..models import ArenaEvaluation, ArenaRun, LedgerEntry, Membership, Org, User
from ..timeutil import utcnow_naive as now
from .call import route, service
from .call.resolve import _marketplace_pricing
from .call.types import CallInput, CallerSnapshot, CallFailure

log = logging.getLogger(__name__)
RUN_SECONDS = 240
_owners: dict[str, asyncio.Task] = {}


def _pack(value: dict) -> str:
    return crypto.encrypt(json.dumps(value, ensure_ascii=True, separators=(",", ":")))


def _unpack(value: str) -> dict:
    return json.loads(crypto.decrypt(value))


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def public_tasks() -> list[dict]:
    """Public catalog estimates by input shape; no team reads or upstream requests."""
    cat = catalog_store.load()
    tasks = []
    # Shape probes only: canonical derivations and adapter constants affect eligibility/pricing.
    # These values are neither returned to the page nor dispatched to a provider.
    probe = {"full_name": "Example Person", "domain": "example.com", "name": "Example",
             "phone": "+14155550100", "email": "person@example.com", "linkedin_url": "https://www.linkedin.com/in/example",
             "q": "software engineers", "title": "engineer", "country": "US", "company_domain": "example.com"}
    for t in rules.TASKS.values():
        contract = cat.contracts[rules.catalog_capability(t.capability)]
        previews = []
        for variant in t.variants:
            identity, _ = route.canonical_identity(contract, {k: probe[k] for k in variant})
            candidates, _ = route.candidates_for(contract, cat.for_capability(contract.capability), cat.adapters, identity)
            providers = {}
            for ep, adapter, accepted in candidates:
                if not rules.supports_discovery(t.capability, {k: probe[k] for k in variant}, accepted) or route.ignored_filters(adapter, contract, identity):
                    continue
                if ep["id"] in rules.EXCLUDED or ep.get("async") or ".bulk" in ep["id"]:
                    continue
                provider = ep["provider"]
                cv = cat.cost_view(ep.get("cost"), provider)
                query, body = adapter.to_upstream(identity, accepted)
                estimate = money.with_margin(_marketplace_pricing(
                    provider, ep["id"], cv, query, json.dumps(body).encode())[0]) if cv and cv.get("usd") is not None else None
                item = {"provider": provider, "endpoint_id": ep["id"], "estimate_micro": estimate,
                        "price_type": (ep.get("cost") or {}).get("type"), "tier": "catalog"}
                previous = providers.get(provider)
                if previous is None or (estimate is not None and (previous["estimate_micro"] is None or estimate < previous["estimate_micro"])):
                    providers[provider] = item
            previews.append(sorted(providers.values(), key=lambda p: (p["estimate_micro"] is None, p["estimate_micro"] or 0, p["provider"])))
        tasks.append({"id": t.capability, "label": t.label, "description": t.description,
                      "variants": t.variants, "examples": rules.example_inputs(t), "fields": list(contract.output),
                      "discovery": t.capability in rules.DISCOVERY_TASKS,
                      "max_entries": rules.DISCOVERY_ENTRIES if t.capability in rules.DISCOVERY_TASKS else rules.MAX_ENTRIES,
                      "result_limit": rules.DISCOVERY_LIMIT if t.capability in rules.DISCOVERY_TASKS else None,
                      "providers": sorted({p["provider"] for rows in previews for p in rows}),
                      "provider_previews": previews})
    return tasks


def _track(run_id, task):
    key = run_id if run_id not in _owners else f"{run_id}:{uuid.uuid4().hex}"
    _owners[key] = task
    task.add_done_callback(lambda t: _owners.pop(key, None) if _owners.get(key) is t else None)


async def _locked_owned(db, run_id, caller):
    await db.execute(update(ArenaRun).where(ArenaRun.id == run_id,
        ArenaRun.org_id == caller.org_id, ArenaRun.user_id == caller.user.id)
        .values(cancel_requested=ArenaRun.cancel_requested))
    return await _owned(db, run_id, caller)


def _attempt(payload, attempt_id):
    return next((a for a in payload["attempts"] if a["id"] == attempt_id), None)


def _uncalled(row, payload, attempt_id):
    if row.state not in rules.TERMINAL | {"running"} or (row.state == "running" and (row.cancel_requested or row.deadline_at < now())):
        raise rules.ArenaError("This run is stopping or unavailable.", 409)
    attempt = _attempt(payload, attempt_id)
    if not attempt or attempt["state"] not in {"not_attempted", "skipped"} or attempt.get("call_ref") or attempt.get("manual"):
        raise rules.ArenaError("This service has already been attempted or is unavailable.", 409)
    return attempt


def _returned(row, payload, attempt_id, *, action: str):
    if row.state not in rules.TERMINAL:
        raise rules.ArenaError(f"Wait for the run to finish before {action} a result.", 409)
    attempt = _attempt(payload, attempt_id)
    if not attempt or attempt["state"] not in {"hit", "miss", "error", "timeout"}:
        raise rules.ArenaError("Choose a returned result.")
    return attempt


async def manual_plan(caller, run_id, attempt_id):
    async with session_maker() as db:
        row = await _owned(db, run_id, caller)
        payload = _unpack(row.payload)
        original = _uncalled(row, payload, attempt_id)
        capability = row.capability
        identity = payload.get("identities", [payload["identity"]])[original.get("entry_index", 0)]
    cat = catalog_store.load()
    plan = await route.build_plan({"id": "arena." + capability, "capability": rules.catalog_capability(capability)}, identity, caller,
                                  route.RouteOptions(strict_filters=True))
    candidate = next((c for c in plan.candidates if c.endpoint["id"] == original["endpoint_id"]), None)
    if candidate is None:
        raise rules.ArenaError("This service cannot use this query with your current access.", 409)
    ep, provider = candidate.endpoint, candidate.endpoint["provider"]
    query, body = candidate.adapter.to_upstream(plan.identity, candidate.variant)
    cv = cat.cost_view(ep.get("cost"), provider)
    if candidate.tier == "platform" and (not cv or cv.get("usd") is None):
        raise rules.ArenaError("Price unavailable for this service.", 409)
    estimate = money.with_margin(_marketplace_pricing(provider, ep["id"], cv, query,
        json.dumps(body).encode())[0]) if candidate.tier == "platform" else 0
    if estimate > 10_000_000:
        raise rules.ArenaError("This service exceeds the Arena per-call limit.")
    required = estimate + (payload.get("auto_verify") or {}).get("estimate_micro", 0)
    quote = {"required_micro": required, "id": uuid.uuid4().hex, "expires_at": (now() + timedelta(minutes=5)).isoformat(),
             "estimate_micro": estimate, "tier": candidate.tier, "query": query, "body": body,
             "method": ep["method"], "price_type": (ep.get("cost") or {}).get("type"),
             "endpoint_hash": _hash(ep), "adapter_hash": _hash(candidate.adapter.__dict__)}
    async with session_maker() as db:
        row = await _locked_owned(db, run_id, caller)
        payload = _unpack(row.payload)
        attempt = _uncalled(row, payload, attempt_id)
        attempt["manual_quote"] = quote
        row.payload = _pack(payload)
        balance = await money.balance_of(db, caller.org_id)
        db.add(row)
        await db.commit()
    return {"id": quote["id"], "estimate_micro": estimate, "required_micro": required, "balance_micro": balance,
            "affordable": required == 0 or balance >= required, "expires_at": quote["expires_at"] + "Z"}


async def start_manual(caller, run_id, attempt_id, quote_id, client, client_ip):
    from datetime import datetime
    async with session_maker() as db:
        row = await _locked_owned(db, run_id, caller)
        payload = _unpack(row.payload)
        attempt = _uncalled(row, payload, attempt_id)
        quote = attempt.get("manual_quote") or {}
        if quote.get("id") != quote_id or datetime.fromisoformat(quote["expires_at"]) < now():
            raise rules.ArenaError("Price expired. Try again for a fresh price.", 409)
        cat = catalog_store.load()
        ep, ad = cat.by_id.get(attempt["endpoint_id"]), cat.adapters.get(attempt["endpoint_id"])
        if not ep or not ad or _hash(ep) != quote["endpoint_hash"] or _hash(ad.__dict__) != quote["adapter_hash"]:
            raise rules.ArenaError("The catalog changed. Refresh this service's price.", 409)
        required = quote.get("required_micro", quote["estimate_micro"])
        if required > 0 and await money.balance_of(db, caller.org_id) < required:
            raise rules.ArenaError("Not enough team credits.", 402)
        active = (await db.execute(select(ArenaRun.id).where(ArenaRun.user_id == caller.user.id,
            ArenaRun.state == "running", ArenaRun.deadline_at > now()).limit(3))).all()
        if row.state != "running" and len(active) >= 3:
            raise rules.ArenaError("Finish or cancel an active Arena run first.", 429)
        if sum(a.get("manual", False) and a["state"] in {"queued", "running"} for a in payload["attempts"]) >= 4:
            raise rules.ArenaError("Four additional services are already running. Wait for one to finish.", 429)
        attempt.update({k: quote[k] for k in ("estimate_micro", "tier", "query", "body", "method",
                                             "price_type", "endpoint_hash", "adapter_hash")})
        attempt.pop("manual_quote", None)
        attempt.update(state="queued", manual=True, detail="Manually requested additional service.")
        payload["stop_reason"] = ""
        row.payload = _pack(payload)
        row.state = "running"
        row.cancel_requested = False
        row.deadline_at = max(row.deadline_at, now() + timedelta(seconds=RUN_SECONDS + 30))
        mode, capability = row.mode, row.capability
        db.add(row)
        await db.commit()
    _track(run_id, asyncio.create_task(_run(run_id, mode, capability, payload,
        CallerSnapshot.capture(caller), client, client_ip, only_attempt_id=attempt_id)))
    return {"id": run_id, "state": "running"}


def _verification_source(row, payload, attempt_id):
    if row.capability not in rules.VERIFICATION_TASKS or row.state not in rules.TERMINAL | {"running"}:
        raise rules.ArenaError("Choose a found email or phone number.", 409)
    if row.state == "running" and (row.cancel_requested or row.deadline_at < now()):
        raise rules.ArenaError("This run is stopping.", 409)
    a = _attempt(payload, attempt_id)
    if not a or a["state"] != "hit" or a.get("verification") or (payload.get("auto_verify") and row.state == "running"):
        raise rules.ArenaError("This result is unavailable or verification has already been requested.", 409)
    capability, _ = rules.VERIFICATION_TASKS[row.capability]
    identity = rules.verification_identity(capability, a.get("output", {}))
    return a, capability, identity


async def _verification_attempt(cat, caller, capability, identity):
    candidates, _, _ = await _plan_entry(cat, caller, capability, identity, None)
    if capability != "people.email.verify":
        return min(candidates, key=lambda a: (a["estimate_micro"], a["provider"]))
    endpoint = cat.by_id.get("treg.people.email.verify")
    if not endpoint:
        raise rules.ArenaError("Email verification routing is unavailable.", 409)
    # Admit the full fallback ceiling, not just the first (possibly free) provider.
    candidates.sort(key=lambda a: (a["estimate_micro"], a["provider"]))
    return {"id": uuid.uuid4().hex, "provider": "treg", "endpoint_id": endpoint["id"],
            "routed": True, "tier": "routed", "method": "POST", "query": {}, "body": identity,
            "state": "queued", "output": {}, "charged_micro": 0,
            "estimate_micro": sum(a["estimate_micro"] for a in candidates),
            "providers": [a["provider"] for a in candidates],
            "endpoint_hash": _hash(endpoint),
            "children": [{k: a[k] for k in ("endpoint_id", "endpoint_hash", "adapter_hash")} for a in candidates]}


def _verification_catalog_matches(cat, attempt):
    ep = cat.by_id.get(attempt["endpoint_id"])
    if not ep or _hash(ep) != attempt["endpoint_hash"]:
        return False
    if attempt.get("routed"):
        return all(_verification_catalog_matches(cat, child) for child in attempt["children"])
    ad = cat.adapters.get(attempt["endpoint_id"])
    return bool(ad and _hash(ad.__dict__) == attempt["adapter_hash"])


async def verification_plan(caller, run_id, attempt_id):
    async with session_maker() as db:
        row = await _owned(db, run_id, caller)
        payload = _unpack(row.payload)
        _, capability, identity = _verification_source(row, payload, attempt_id)
    v = await _verification_attempt(catalog_store.load(), caller, capability, identity)
    if v["estimate_micro"] > 10_000_000:
        raise rules.ArenaError("Verification exceeds the per-call limit.")
    q = {"id":uuid.uuid4().hex, "expires_at":(now()+timedelta(minutes=5)).isoformat(), "attempt":{**v, "capability":capability}}
    async with session_maker() as db:
        row = await _locked_owned(db, run_id, caller)
        payload = _unpack(row.payload)
        a, _, _ = _verification_source(row, payload, attempt_id)
        a["verification_quote"] = q
        balance = await money.balance_of(db, caller.org_id)
        row.payload = _pack(payload)
        db.add(row)
        await db.commit()
    return {"id":q["id"], "estimate_micro":v["estimate_micro"], "provider":v["provider"], "balance_micro":balance,
            "affordable":v["estimate_micro"] == 0 or balance >= v["estimate_micro"], "expires_at":q["expires_at"]+"Z"}


async def start_verification(caller, run_id, attempt_id, quote_id, client, client_ip):
    from datetime import datetime
    async with session_maker() as db:
        row = await _locked_owned(db, run_id, caller)
        payload = _unpack(row.payload)
        a, capability, _ = _verification_source(row, payload, attempt_id)
        q = a.get("verification_quote") or {}
        if q.get("id") != quote_id or datetime.fromisoformat(q["expires_at"]) < now():
            raise rules.ArenaError("Verification price expired. Try again.", 409)
        v, cat = q["attempt"], catalog_store.load()
        if not _verification_catalog_matches(cat, v):
            raise rules.ArenaError("The catalog changed. Refresh verification pricing.", 409)
        if v["estimate_micro"] > 0 and await money.balance_of(db, caller.org_id) < v["estimate_micro"]:
            raise rules.ArenaError("Not enough team credits.", 402)
        active = (await db.execute(select(ArenaRun.id).where(ArenaRun.user_id == caller.user.id, ArenaRun.state == "running", ArenaRun.deadline_at > now()).limit(3))).all()
        if row.state != "running" and len(active) >= 3:
            raise rules.ArenaError("Finish or cancel an active Arena run first.", 429)
        if sum(x.get("verification", {}).get("state") in {"queued", "running"} for x in payload["attempts"]) >= 4:
            raise rules.ArenaError("Four verifications are already running. Wait for one to finish.", 429)
        a["verification"] = v
        a.pop("verification_quote", None)
        row.payload = _pack(payload)
        row.state = "running"
        row.cancel_requested = False
        row.deadline_at = max(row.deadline_at, now()+timedelta(seconds=RUN_SECONDS+30))
        mode = row.mode
        db.add(row)
        await db.commit()
    _track(run_id, asyncio.create_task(_run(run_id, mode, capability, payload, CallerSnapshot.capture(caller), client, client_ip, verification_of=attempt_id)))
    return {"id":run_id, "state":"running"}


async def rate_result(caller, run_id, attempt_id, *, value):
    if value not in {"up", "down"}:
        raise rules.ArenaError("Choose thumbs up or thumbs down.")
    async with session_maker() as db:
        row = await _locked_owned(db, run_id, caller)
        payload = _unpack(row.payload)
        attempt = _returned(row, payload, attempt_id, action="rating")
        previous = attempt.get("rating") or {}
        if previous.get("value") != value:
            timestamp = now().isoformat() + "Z"
            attempt["rating"] = {"value": value, "created_at": previous.get("created_at", timestamp),
                "updated_at": timestamp, "feedback_context": "attributed"}
            row.payload = _pack(payload)
            db.add(row)
            await db.commit()
        return {"rating": attempt["rating"]}


async def report_result(caller, run_id, attempt_id, *, reason, comment):
    if reason not in {"", "wrong_person", "wrong_company", "incorrect_data", "outdated_data", "other"}:
        raise rules.ArenaError("Choose an issue type.")
    if not reason and not comment.strip():
        raise rules.ArenaError("Add a reason or a note.")
    async with session_maker() as db:
        row = await _locked_owned(db, run_id, caller)
        payload = _unpack(row.payload)
        attempt = _returned(row, payload, attempt_id, action="reporting")
        if (attempt.get("rating") or {}).get("value") == "up":
            raise rules.ArenaError("Choose thumbs down before adding issue details.", 409)
        if not attempt.get("report"):
            attempt["report"] = {"reason": reason, "comment": comment.strip(),
                "created_at": now().isoformat() + "Z", "feedback_context": "attributed"}
            row.payload = _pack(payload)
            db.add(row)
            await db.commit()
        return {"report": attempt["report"]}


async def _owned(db, run_id: str, caller) -> ArenaRun:
    row = await db.get(ArenaRun, run_id)
    if row is None or row.org_id != caller.org_id or row.user_id != caller.user.id or row.expires_at < now():
        raise rules.ArenaError("Run not found.", 404)
    return row


async def prune(db) -> None:
    """Bounded lazy retention on Arena use; no call/identity payload enters analytics."""
    ids = list((await db.execute(select(ArenaRun.id).where(ArenaRun.expires_at < now()).limit(50))).scalars())
    if ids:
        await db.execute(delete(ArenaEvaluation).where(ArenaEvaluation.run_id.in_(ids)))
        await db.execute(delete(ArenaRun).where(ArenaRun.id.in_(ids)))


async def _plan_entry(cat, caller, capability, identity, providers):
    # Arena also offers single-provider tasks; public routed endpoints require two providers.
    plan = await route.build_plan({"id": "arena." + capability, "capability": rules.catalog_capability(capability)}, identity, caller,
                                  route.RouteOptions(strict_filters=True))
    chosen, dropped, seen = [], list(plan.dropped), set()
    requested = set(providers) if providers is not None else None
    if requested is not None and not requested:
        raise rules.ArenaError("Select at least one service.")
    for c in plan.candidates:
        ep, p = c.endpoint, c.endpoint["provider"]
        if not rules.supports_discovery(capability, identity, c.variant):
            continue
        if requested is not None and p not in requested:
            continue
        if ep["id"] in rules.EXCLUDED or ep.get("async") or ".bulk" in ep["id"] or p in seen:
            continue
        query, body = c.adapter.to_upstream(plan.identity, c.variant)
        cv = cat.cost_view(ep.get("cost"), p)
        if c.tier == "platform" and (not cv or cv.get("usd") is None):
            dropped.append({"endpoint_id": ep["id"], "why": "price unavailable"})
            continue
        # Exact adapter request, including modifiers: a catalog base price is not a quote.
        estimate = money.with_margin(_marketplace_pricing(
            p, ep["id"], cv, query, json.dumps(body).encode())[0]) if c.tier == "platform" else 0
        seen.add(p)
        chosen.append({"id": uuid.uuid4().hex, "provider": p, "endpoint_id": ep["id"],
                       "tier": c.tier, "estimate_micro": estimate, "query": query, "body": body,
                       "method": ep["method"], "state": "queued", "output": {},
                       "charged_micro": 0, "order": len(chosen), "adapter_hash": _hash(c.adapter.__dict__),
                       "endpoint_hash": _hash(ep), "price_type": (ep.get("cost") or {}).get("type")})
    if requested is not None and requested - seen:
        raise rules.ArenaError("Some selected services cannot use this input right now. Refresh the service selection.")
    if not chosen:
        raise rules.ArenaError("No service can use this input with your team's current access.")
    return chosen, dropped, list(plan.contract.output)


def _run_seconds(payload):
    return min(3600, RUN_SECONDS * len(payload.get("identities", [payload["identity"]])))


async def quote(caller, *, capability: str, mode: str, providers: list[str] | None,
                max_cost_micro: int, identity: dict | None = None,
                identities: list[dict] | None = None, auto_verify: bool = False) -> dict:
    entries = rules.validate_entries(capability, identity, identities)
    if any(entry.get("country") and country_name(entry["country"]) is None for entry in entries):
        raise rules.ArenaError("Choose a recognized two-letter country code, such as US or GB.")
    if caller.org.demo or caller.org.public_demo:
        raise rules.ArenaError("Sign in with a regular team to run Arena.", 403)
    if mode not in {"compare", "waterfall"} or not 0 <= max_cost_micro <= 10_000_000:
        raise rules.ArenaError("Choose a mode and a budget between $0 and $10.")
    cat = catalog_store.load()
    verification = None
    if auto_verify:
        if capability not in rules.VERIFICATION_TASKS:
            raise rules.ArenaError("Auto verification is only available for finding emails or phone numbers.")
        verify_task, field = rules.VERIFICATION_TASKS[capability]
        probe = {field: "person@example.com" if field == "email" else "+14155550100"}
        v = await _verification_attempt(cat, caller, verify_task, probe)
        verification = {**{k:v[k] for k in ("provider", "endpoint_id", "estimate_micro")}, "capability":verify_task, "field":field}
        if v.get("routed"):
            verification.update({k:v[k] for k in ("routed", "providers", "children", "endpoint_hash")})
    chosen, dropped, fields, cohort = [], [], [], None
    display = {}
    for index, entry in enumerate(entries):
        candidates, omitted, fields = await _plan_entry(cat, caller, capability, entry, providers)
        current = {a["provider"] for a in candidates}
        if cohort is not None and current != cohort:
            raise rules.ArenaError(f"Entry {index + 1} supports different vendors. Select vendors available for every entry.")
        if cohort is None:
            cohort = current
            order = list(range(len(candidates)))
            secrets.SystemRandom().shuffle(order)
            display = {a["provider"]: n for a, n in zip(candidates, order)}
        if mode == "waterfall":
            candidates.sort(key=lambda a: a["estimate_micro"])
        for order, a in enumerate(candidates):
            a.update(entry_index=index, order=order, display_order=display[a["provider"]])
        chosen.extend(candidates)
        dropped.extend(omitted)
    verification_total = (verification or {}).get("estimate_micro", 0) * (len(chosen) if mode == "compare" else len(entries))
    total = sum(a["estimate_micro"] for a in chosen) + verification_total
    required = rules.required_credit(chosen, mode) + verification_total
    if mode == "compare" and total > max_cost_micro:
        raise rules.ArenaError(f"This Battle is estimated at ${total / 1e6:.4f}, above the ${max_cost_micro / 1e6:g} run limit. Please select fewer entries or vendors.")
    if mode == "waterfall" and required > max_cost_micro:
        raise rules.ArenaError(f"The first waterfall step for all entries exceeds the ${max_cost_micro / 1e6:g} run limit. Use fewer entries or vendors.")
    snapshot = {"identity": entries[0], "identities": entries, "attempts": chosen, "max_cost_micro": max_cost_micro,
                "version": rules.VERSION, "fields": fields, "stop_reason": "", "auto_verify":verification, "verification_required_micro":verification_total}
    async with session_maker() as db:
        await db.execute(update(User).where(User.id == caller.user.id).values(token_version=User.token_version))
        await prune(db)
        # Automatic price previews are not executions. Bound unused quote storage by
        # retiring the oldest drafts, without blocking edits or touching run history.
        stale = (await db.execute(select(ArenaRun.id).where(
            ArenaRun.user_id == caller.user.id, ArenaRun.state == "quoted")
            .order_by(ArenaRun.created_at.desc(), ArenaRun.id.desc()).offset(99))).scalars().all()
        if stale:
            await db.execute(delete(ArenaRun).where(ArenaRun.id.in_(stale), ArenaRun.state == "quoted"))
        balance = await money.balance_of(db, caller.org_id)
        row = ArenaRun(id=uuid.uuid4().hex, org_id=caller.org_id, user_id=caller.user.id,
                       request_key=uuid.uuid4().hex, fingerprint=_hash(snapshot), capability=capability,
                       mode=mode, state="quoted", payload=_pack(snapshot),
                       deadline_at=now() + timedelta(minutes=5), expires_at=now() + timedelta(days=rules.RETENTION_DAYS))
        db.add(row)
        await db.commit()
    provider_quotes = {}
    for a in chosen:
        if a["provider"] not in provider_quotes:
            provider_quotes[a["provider"]] = {k: a[k] for k in ("provider", "endpoint_id", "tier", "price_type")}
            provider_quotes[a["provider"]]["estimate_micro"] = 0
        provider_quotes[a["provider"]]["estimate_micro"] += a["estimate_micro"]
    return {"id": row.id, "mode": mode, "capability": capability, "org_id": caller.org_id,
            "entry_count": len(entries), "auto_verify":verification, "verification_estimate_micro":verification_total,
            "org": caller.org.slug, "balance_micro": balance, "estimate_micro": total,
            "required_micro": required, "max_cost_micro": max_cost_micro,
            "affordable": required == 0 or balance >= required, "dropped": dropped,
            "providers": list(provider_quotes.values()),
            "expires_at": row.deadline_at.isoformat() + "Z"}


async def start(caller, run_id: str, client, client_ip: str) -> dict:
    async with session_maker() as db:
        # Serialize admission for one user across distinct drafts and teams.
        await db.execute(update(User).where(User.id == caller.user.id).values(token_version=User.token_version))
        row = await _owned(db, run_id, caller)
        if row.state != "quoted":
            return {"id": row.id, "state": row.state}
        if row.deadline_at < now():
            raise rules.ArenaError("This quote expired. Submit again for a fresh quote.", 409)
        recent = (await db.execute(select(ArenaRun.id).where(
            ArenaRun.user_id == caller.user.id, ArenaRun.state != "quoted",
            ArenaRun.created_at > now() - timedelta(hours=1)).limit(100))).all()
        if len(recent) >= 100:
            raise rules.ArenaError("You've started 100 Arena runs in the past hour. Try again when an earlier run leaves that window; price previews don't count.", 429)
        payload = _unpack(row.payload)
        cat = catalog_store.load()
        for a in payload["attempts"]:
            ep, ad = cat.by_id.get(a["endpoint_id"]), cat.adapters.get(a["endpoint_id"])
            if not ep or not ad or _hash(ep) != a["endpoint_hash"] or _hash(ad.__dict__) != a["adapter_hash"]:
                raise rules.ArenaError("The catalog changed. Refresh your quote before running.", 409)
        required = rules.required_credit(payload["attempts"], row.mode) + payload.get("verification_required_micro", 0)
        if required > 0 and await money.balance_of(db, caller.org_id) < required:
            raise rules.ArenaError("Not enough team credits. Add credits or choose fewer services.", 402)
        active = (await db.execute(select(ArenaRun.id).where(
            ArenaRun.user_id == caller.user.id, ArenaRun.state == "running", ArenaRun.deadline_at > now()).limit(3))).all()
        if len(active) >= 3:
            raise rules.ArenaError("Finish or cancel an active Arena run first.", 429)
        claim = await db.execute(update(ArenaRun).where(ArenaRun.id == run_id, ArenaRun.state == "quoted").values(
            state="running", created_at=now(), deadline_at=now() + timedelta(seconds=_run_seconds(payload) + 30)))
        await db.commit()
        mode, capability = row.mode, row.capability
    if claim.rowcount:
        analytics.capture(caller.email, "arena_run_started", {
            "run_id": run_id, "capability": capability, "mode": mode, "client": "enrich-arena",
            "entry_count": len(payload.get("identities", [payload["identity"]])),
        }, groups={"team": caller.org.slug})
        task = asyncio.create_task(_run(run_id, mode, capability, payload, CallerSnapshot.capture(caller), client, client_ip))
        _track(run_id, task)
    return {"id": run_id, "state": "running"}


async def _fresh_caller(snapshot):
    """Recheck membership and policy between legs; never resurrect revoked access from a quote."""
    async with session_maker() as db:
        member = await db.get(Membership, snapshot.membership.id)
        user = await db.get(User, snapshot.user.id)
        org = await db.get(Org, snapshot.org.id)
        if not member or not user or not org or user.suspended or org.suspended:
            raise rules.ArenaError("Team access is no longer available.", 403)
        current = CallerSnapshot.capture(Caller(member, user, org))
    # A comparison measures a direct service. Avoid aggregator substitutions and their different
    # prices; this is a per-run execution choice, never a change to the team's settings.
    return replace(current, org=replace(current.org, platform_overflow_disabled=True))


async def _save(run_id, payload, state=None, *, only_attempt_id=None, verification_of=None):
    # Each worker owns only its attempts. Lock and merge against the latest payload so
    # concurrent manual calls, quotes and feedback cannot overwrite each other.
    async with session_maker() as db:
        await db.execute(update(ArenaRun).where(ArenaRun.id == run_id)
                         .values(cancel_requested=ArenaRun.cancel_requested))
        row = await db.get(ArenaRun, run_id)
        if row is None or row.state != "running":
            return
        current = _unpack(row.payload)
        updates = {a["id"]: a for a in payload["attempts"]}
        for index, saved in enumerate(current["attempts"]):
            if verification_of:
                if saved["id"] == verification_of:
                    saved["verification"] = updates[saved["id"]]["verification"]
                continue
            owned = saved["id"] == only_attempt_id if only_attempt_id else not saved.get("manual")
            if not owned or saved["id"] not in updates:
                continue
            merged = dict(updates[saved["id"]])
            for field in ("manual_quote", "verification_quote", "rating", "report"):
                if field in saved:
                    merged[field] = saved[field]
            if saved.get("verification") and not merged.get("verification", {}).get("automatic"):
                merged["verification"] = saved["verification"]
            current["attempts"][index] = merged
        for result in current["attempts"]:
            if not result.get("report") and rules.verification_rejected_email(result, row.capability):
                verification = result["verification"]
                result["report"] = {
                    "reason": "incorrect_data",
                    "comment": f"Email verification by {verification['provider']} returned an invalid mailbox verdict.",
                    "created_at": now().isoformat() + "Z",
                    "feedback_context": "automated_verification",
                    "verification_id": verification["id"],
                }
        if state:
            pending = any(a["state"] in {"queued", "running"} or a.get("verification", {}).get("state") in {"queued", "running"} for a in current["attempts"])
            if not pending:
                row.state = "cancelled" if row.cancel_requested else state
                current["stop_reason"] = "Cancelled by you." if row.cancel_requested else payload.get("stop_reason", "")
        row.payload = _pack(current)
        db.add(row)
        await db.commit()


async def _run(run_id, mode, capability, payload, caller, client, client_ip, only_attempt_id=None, verification_of=None):
    lock = asyncio.Lock()
    started = time.monotonic()
    offset = max((a.get("started_ms", 0) + (a.get("duration_ms") or 0) for a in payload["attempts"]), default=0) if only_attempt_id else 0
    cat = catalog_store.load()
    contract = cat.contracts[rules.catalog_capability(capability)]
    state = "completed"
    is_batch = len(payload.get("identities", [payload["identity"]])) > 1
    raw_limit = rules.MAX_BATCH_RAW_BYTES // len(payload["attempts"]) if is_batch else rules.MAX_RESULT_BYTES

    async def persist():
        async with lock:
            await _save(run_id, payload, only_attempt_id=only_attempt_id, verification_of=verification_of)

    async def attempt(a, verification_task=None):
        ep, ad = cat.by_id[a["endpoint_id"]], cat.adapters.get(a["endpoint_id"])
        current = await _fresh_caller(caller)
        a.update(state="running", started_ms=offset + round((time.monotonic() - started) * 1000), charged_micro=None)
        await persist()  # A dispatch is durably visible before any upstream work.
        began = time.monotonic()
        data = json.dumps(a["body"]).encode() if a["method"] in {"POST", "PUT", "PATCH"} else b""
        headers = ((b"content-type", b"application/json"), (b"content-length", str(len(data)).encode()),
                   (b"x-treg-client", b"enrich-arena"), (b"cache-control", b"no-cache"),
                   (b"x-treg-route-max-cost", f"{a['estimate_micro'] / 1e6:.6f}".encode()))
        if a.get("routed"):
            excluded = sorted({e["provider"] for e in cat.by_id.values()
                               if e.get("capability") == verification_task and e["provider"] not in a["providers"]})
            headers += ((b"x-treg-route-waterfall", b"1"),
                        (b"x-treg-route-prefer", ",".join(a["providers"]).encode()),
                        (b"x-treg-route-exclude", ",".join(excluded).encode()))
        context = service.create_call_context(CallInput(method=a["method"], raw_rest=a["endpoint_id"],
            raw_headers=headers, query_items=tuple(a["query"].items()), raw_query=urlencode(a["query"]),
            body=route._Bytes(data), caller=current, client_ip=client_ip))
        a["call_ref"] = context.call_ref
        await persist()
        response = None
        try:
            async with asyncio.timeout(90):
                response = await service.execute_call(context, client)
                buf = bytearray()
                oversized = False
                async for chunk in response.body_stream:
                    if len(buf) + len(chunk) <= rules.MAX_RESULT_BYTES and not oversized:
                        buf.extend(chunk)
                    else:
                        oversized = True  # Drain for the ordinary settlement/close lifecycle.
                a["charged_micro"] = context.cost_micro if context.cost_micro is not None else int(route._header(response, "X-Treg-Cost-Micro") or 0)
                a["cached"] = bool(route._header(response, "X-Treg-Cache") in {"hit", "stale"})
                try:
                    doc = json.loads(buf) if not oversized else None
                except (ValueError, UnicodeDecodeError):
                    doc = None
                if a.get("routed"):
                    meta = doc.get("_treg", {}) if isinstance(doc, dict) else {}
                    outcome = "hit" if response.status < 400 and meta.get("outcome") == "hit" else "miss" if meta.get("outcome") == "miss" else "error"
                    output = doc.get("output", {}) if outcome == "hit" else {}
                    a["tried"] = meta.get("tried", [])
                    if meta.get("provider"):
                        a["provider"] = meta["provider"]
                    if meta.get("served_by"):
                        a["served_by"] = meta["served_by"]
                else:
                    outcome, output = rules.classify(cat.contracts[verification_task] if verification_task else contract, ad, ep, response.status, doc)
                raw_omitted = is_batch and len(json.dumps(doc, ensure_ascii=True)) > raw_limit
                a.update(state=outcome, output=output,
                         raw=None if raw_omitted else doc, raw_omitted=doc is not None and raw_omitted,
                         detail=("Response exceeded the Arena size limit." if oversized else
                                 "No matching result." if outcome == "miss" else
                                 f"Service returned HTTP {response.status}." if outcome == "error" else "Task data found."),
                         status=response.status)
        except CallFailure as exc:
            a.update(state="error", detail=exc.kind, failure_kind=exc.kind, status=exc.status_code,
                     charged_micro=context.cost_micro if context.cost_micro is not None else None if a.get("routed") else 0)
            if a.get("routed") and isinstance(exc.detail, dict):
                a["tried"] = exc.detail.get("tried", [])
                a["raw"] = exc.detail
        except TimeoutError:
            a.update(state="timeout", detail="Service deadline exceeded.", charged_micro=context.cost_micro)
        except asyncio.CancelledError:
            a.update(state="cancelled", detail="Run stopped.", charged_micro=context.cost_micro)
            raise
        except Exception:
            a.update(state="error", detail="Result could not be completed.", charged_micro=context.cost_micro)
            log.exception("Arena attempt failed: %s", run_id)
        finally:
            if response is not None:
                await response.close()
            a["duration_ms"] = round((time.monotonic() - began) * 1000)
            await persist()
        policy = payload.get("auto_verify")
        if not verification_task and a["state"] == "hit" and policy and not a.get("verification"):
            try:
                current = await _fresh_caller(caller)
                identity = rules.verification_identity(policy["capability"], a["output"])
                if policy.get("routed"):
                    if not _verification_catalog_matches(cat, policy):
                        raise rules.ArenaError("Verification catalog changed; refresh the quote.")
                    v = {**policy, "id":uuid.uuid4().hex, "tier":"routed", "method":"POST",
                         "query":{}, "body":identity, "state":"queued", "output":{}, "charged_micro":0}
                else:
                    candidates, _, _ = await _plan_entry(cat, current, policy["capability"], identity, [policy["provider"]])
                    v = candidates[0]
                if v["endpoint_id"] != policy["endpoint_id"] or v["estimate_micro"] > policy["estimate_micro"]:
                    raise rules.ArenaError("Verification price or endpoint changed; no verification call was made.")
                a["verification"] = {**v, "capability": policy["capability"], "automatic": True}
                await persist()
                await attempt(a["verification"], policy["capability"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not a.get("verification"):
                    detail = str(exc) if isinstance(exc, rules.ArenaError) else "Verification unavailable; the found value is preserved."
                    a["verification"] = {"state":"error", "not_started":True, "automatic":True, "capability":policy["capability"], "provider":policy["provider"], "charged_micro":0, "output":{}, "detail":detail}
                    await persist()

    async def work():
        if verification_of:
            parent = next(a for a in payload["attempts"] if a["id"] == verification_of)
            await attempt(parent["verification"], capability)
            payload["stop_reason"] = "Verification completed. Found values are preserved."
        elif only_attempt_id:
            await attempt(next(a for a in payload["attempts"] if a["id"] == only_attempt_id))
            payload["stop_reason"] = "Additional service completed. Earlier results are preserved."
        elif mode == "compare":
            # All ceilings were admitted together at quote/start, not checked independently by
            # racing child tasks. Runtime balance/price/policy gates still apply to every leg.
            sem = asyncio.Semaphore(4)
            async def leg(a):
                async with sem:
                    await attempt(a)
            async with asyncio.TaskGroup() as group:
                for a in payload["attempts"]:
                    group.create_task(leg(a))
            payload["stop_reason"] = "All selected services completed."
        else:
            spent, stopped, errors, rejected = 0, {}, {}, {}
            # Finish each price level across the list before advancing unresolved entries.
            # Every entry has its own hit/error stop; the spending ceiling is shared.
            for a in sorted(payload["attempts"], key=lambda a: (a["order"], a.get("entry_index", 0))):
                entry = a.get("entry_index", 0)
                if entry in stopped:
                    a.update(state="not_attempted", detail=stopped[entry])
                    continue
                if spent + a["estimate_micro"] + (payload.get("auto_verify") or {}).get("estimate_micro", 0) > payload["max_cost_micro"]:
                    a.update(state="skipped", detail="Would exceed the run budget.")
                    continue
                rejected_providers = rejected.setdefault(entry, set())
                if rejected_providers and (a["provider"] in rejected_providers or
                    (a["tier"] == "platform" and a["price_type"] not in {"per_success", "free"} and a["estimate_micro"] > route.CHEAP_RETRY_MICRO)):
                    a.update(state="skipped", detail="Not retried on a paid service after a rejected request.")
                    continue
                await attempt(a)
                # An uncertain fee consumes its ceiling for subsequent admission.
                spent += a["charged_micro"] if a.get("charged_micro") is not None else a["estimate_micro"]
                v = a.get("verification") or {}
                spent += v.get("charged_micro") if v.get("charged_micro") is not None else v.get("estimate_micro", 0)
                if a["state"] == "hit":
                    stopped[entry] = "Stopped at the first result containing the task's required data."
                    continue
                if a.get("failure_kind") in route._GLOBAL_REFUSALS:
                    payload["stop_reason"] = "Stopped by the team's balance or usage policy."
                    break
                if a["state"] != "miss":
                    errors[entry] = errors.get(entry, 0) + 1
                    if a.get("status") in route._CALLER_FAULT and not (a["tier"] == "platform" and a.get("status") in {401, 403}):
                        rejected_providers.add(a["provider"])
                    if errors[entry] > route.MAX_ERROR_FALLBACKS:
                        stopped[entry] = "Stopped after the bounded error fallback."
            if not payload["stop_reason"]:
                count = len(payload.get("identities", [payload["identity"]]))
                hits = len({a.get("entry_index", 0) for a in payload["attempts"] if a["state"] == "hit"})
                payload["stop_reason"] = (stopped.get(0, "No remaining service within the run budget returned the required data.") if count == 1 else
                    f"{hits} of {count} entries found. Each entry stops at its first result; later vendors receive unresolved entries.")

    stop_watching = asyncio.Event()

    async def watch_cancel():
        while True:
            try:
                await asyncio.wait_for(stop_watching.wait(), 0.75)
                return
            except TimeoutError:
                pass
            async with session_maker() as db:
                row = await db.get(ArenaRun, run_id)
                if row is None or row.cancel_requested:
                    return

    worker = asyncio.create_task(work())
    watcher = asyncio.create_task(watch_cancel())
    try:
        done, _ = await asyncio.wait({worker, watcher}, timeout=RUN_SECONDS if only_attempt_id or verification_of else _run_seconds(payload), return_when=asyncio.FIRST_COMPLETED)
        if worker in done:
            await worker
        else:
            state = "cancelled" if watcher in done else "interrupted"
            payload["stop_reason"] = "Cancelled by you." if state == "cancelled" else "Run deadline reached."
    except asyncio.CancelledError:
        state = "interrupted"
        payload["stop_reason"] = "Execution interrupted; paid calls are not automatically retried."
    except Exception:
        state = "failed"
        payload["stop_reason"] = "The run could not finish. Completed results are kept."
        log.exception("Arena run failed: %s", run_id)
    finally:
        worker.cancel()
        # Finish an in-flight poll before saving. Cancelling SQLite cursor execution can leave
        # a read lock alive after session cleanup, blocking this commit and later schema resets.
        stop_watching.set()
        await asyncio.gather(worker, watcher, return_exceptions=True)
        for a in payload["attempts"]:
            v = a.get("verification")
            if v and v["state"] in {"queued", "running"}:
                v.update(state="cancelled" if state == "cancelled" else "interrupted", detail=payload["stop_reason"])
            if a["state"] == "queued":
                a.update(state="not_attempted", detail=payload["stop_reason"])
        await _save(run_id, payload, state, only_attempt_id=only_attempt_id, verification_of=verification_of)
        if not only_attempt_id and not verification_of:
            analytics.capture(caller.email, "arena_run_completed", {
                "run_id": run_id, "capability": capability, "mode": mode, "client": "enrich-arena",
                "state": state, "entry_count": len(payload.get("identities", [payload["identity"]])),
                "returned_data": any(a["state"] == "hit" for a in payload["attempts"]),
                "successful_call": any(a["state"] in {"hit", "miss"} for a in payload["attempts"]),
            }, groups={"team": caller.org.slug})


async def get_run(caller, run_id):
    async with session_maker() as db:
        row = await _locked_owned(db, run_id, caller)
        payload = _unpack(row.payload)
        if row.state == "running" and row.deadline_at < now():
            # No re-dispatch after process loss. Keep unknown fees unknown until reconciled.
            row.state = "interrupted"
            payload["stop_reason"] = "Execution was interrupted. No calls will be retried automatically."
            for a in payload["attempts"]:
                v = a.get("verification")
                if v and v["state"] in {"queued", "running"}:
                    v["state"] = "interrupted"
                if a["state"] in {"queued", "running"}:
                    a["state"] = "not_attempted" if a["state"] == "queued" else "interrupted"
            row.payload = _pack(payload)
            db.add(row)
            await db.commit()
        if row.state in rules.TERMINAL:
            uncertain = [a for parent in payload["attempts"] for a in (parent, parent.get("verification")) if a and a.get("charged_micro") is None]
            refs = [a["call_ref"] for a in uncertain if a.get("call_ref")]
            entries = (await db.execute(select(LedgerEntry).where(LedgerEntry.org_id == caller.org_id,
                LedgerEntry.call_id.in_(refs), LedgerEntry.kind.in_(["settle", "release"])))).scalars().all() if refs else []
            final_costs = {e.call_id: -e.amount_micro if e.kind == "settle" else 0 for e in entries}
            changed = False
            for a in uncertain:
                if a.get("call_ref") in final_costs or a["tier"] != "platform":
                    a["charged_micro"] = final_costs.get(a.get("call_ref"), 0)
                    changed = True
            if changed:
                row.payload = _pack(payload)
                db.add(row)
                await db.commit()
        evaluation = (await db.execute(select(ArenaEvaluation).where(ArenaEvaluation.run_id == row.id))).scalar_one_or_none()
        return {"id": row.id, "mode": row.mode, "capability": row.capability, "state": row.state,
                "revealed": True, "auto_verify":payload.get("auto_verify"), "created_at": row.created_at.isoformat() + "Z",
                "identity": payload["identity"], "identities": payload.get("identities", [payload["identity"]]), "fields": payload["fields"],
                "vote": ({"kind": evaluation.kind, **_unpack(evaluation.payload).get("vote", {})} if evaluation else None),
                **rules.present(payload, mode=row.mode, state=row.state, capability=row.capability)}


async def history(caller, *, before: str | None = None, limit: int = 30):
    async with session_maker() as db:
        await prune(db)
        query = select(ArenaRun).where(ArenaRun.org_id == caller.org_id,
            ArenaRun.user_id == caller.user.id, ArenaRun.state != "quoted", ArenaRun.expires_at > now())
        if before is not None:
            anchor = (await db.execute(query.where(ArenaRun.id == before))).scalar_one_or_none()
            if anchor is None:
                raise rules.ArenaError("History cursor is no longer available. Refresh the page.", 404)
            query = query.where(or_(ArenaRun.created_at < anchor.created_at,
                (ArenaRun.created_at == anchor.created_at) & (ArenaRun.id < anchor.id)))
        rows = (await db.execute(query.order_by(ArenaRun.created_at.desc(), ArenaRun.id.desc())
            .limit(limit))).scalars().all()
        result = [{"id": r.id, "capability": r.capability, "mode": r.mode, "state": r.state,
                   "identity": _unpack(r.payload)["identity"],
                   "entry_count": len(_unpack(r.payload).get("identities", [None])),
                   "created_at": r.created_at.isoformat() + "Z"} for r in rows]
        await db.commit()
        return result


async def cancel(caller, run_id):
    async with session_maker() as db:
        row = await _owned(db, run_id, caller)
        if row.state == "running":
            await db.execute(update(ArenaRun).where(ArenaRun.id == run_id).values(cancel_requested=True))
            await db.commit()
    return {"id": run_id}


async def evaluate(caller, run_id, *, kind: str, selected: list[str], reasons: list[str], comment: str):
    # Lock the run, rather than an absent evaluation row, to serialize concurrent votes.
    async with session_maker() as db:
        await db.execute(update(ArenaRun).where(ArenaRun.id == run_id, ArenaRun.org_id == caller.org_id,
            ArenaRun.user_id == caller.user.id).values(cancel_requested=ArenaRun.cancel_requested))
        row = await _owned(db, run_id, caller)
        if row.mode != "compare" or row.state not in rules.TERMINAL:
            raise rules.ArenaError("Finish a comparison before voting.", 409)
        existing = (await db.execute(select(ArenaEvaluation).where(ArenaEvaluation.run_id == row.id))).scalar_one_or_none()
        if existing is not None:
            # Showing vendors does not submit a vote; an explicit vote is immutable on retries.
            return {"id": run_id, "revealed": True}
        payload = _unpack(row.payload)
        rules.validate_vote(kind, selected, reasons, payload["attempts"])
        vote = {"selected": selected, "reasons": reasons, "comment": comment[:1000]}
        db.add(ArenaEvaluation(id=uuid.uuid4().hex, org_id=caller.org_id, run_id=row.id,
            user_id=caller.user.id, kind=kind, payload=_pack({"vote": vote, "version": rules.VERSION,
                "feedback_context": "attributed",
                "fingerprint": row.fingerprint, "exposure": [{k: a[k] for k in
                    ("id", "endpoint_id", "display_order", "state", "adapter_hash")} for a in payload["attempts"]]})))
        row.revealed_at = now()
        db.add(row)
        await db.commit()
    return {"id": run_id, "revealed": True}


async def shutdown():
    tasks = list(_owners.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
