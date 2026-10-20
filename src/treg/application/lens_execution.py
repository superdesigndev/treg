"""Native Lens execution: fixed reviewed tasks through the ordinary governed call pipeline.

The persistence use case owns the durable run claim. This module never retries a paid leg,
reserves a separate balance, or holds a DB session across provider I/O.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from .. import oauth_providers
from ..domain import lens as rules, money
from ..domain.catalog import store as catalog_store
from ..domain.governance import access as access_policy
from ..domain.identity import api_keys as managed_keys
from ..domain.identity.access import Caller
from ..infra.db import session_maker
from ..models import ApiKey, Membership, Org, User
from .call import resolve, route, service
from .call.types import (AuthorizationFailed, CallFailure, CallInput, CallerSnapshot,
                         IntakeFailed, ReservationFailed, ResolutionFailed)

QUOTE_SECONDS = 300
CALL_SECONDS = 90
MAX_PARALLEL = 4
NOTE = ("Reservation estimate in USD, including the current platform margin. Actual settled fees "
        "can vary with provider usage and cached answers. Your own connected keys are unmetered "
        "by treg; their provider charges are separate. Perplexity uses the Sonar model API; "
        "ChatGPT, Gemini and Google AI Mode use their web interfaces. Locale: United States, English.")


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _snapshot(caller) -> CallerSnapshot:
    snapshot = caller if isinstance(caller, CallerSnapshot) else CallerSnapshot.capture(caller)
    # A measurement must use the quoted provider, never an undisclosed capacity substitute.
    return replace(snapshot, org=replace(snapshot.org, platform_overflow_disabled=True))


def _require_access(caller) -> None:
    if caller.org.demo or caller.org.public_demo:
        raise rules.LensError("Use a regular team to run Lens.", 403)
    if (caller.role not in {"member", "admin", "owner"} or caller.membership.pinned_tags
            or caller.membership.tool_access is not None or caller.membership.project_access is not None):
        raise rules.LensError("Lens requires an unrestricted team member account.", 403)


async def _fresh_caller(snapshot) -> CallerSnapshot:
    """Recheck current identity between legs; a saved quote never resurrects revoked access."""
    async with session_maker() as db:
        member = await db.get(Membership, snapshot.membership.id)
        user = await db.get(User, snapshot.user.id)
        org = await db.get(Org, snapshot.org.id)
        if (not member or not user or not org or user.suspended or org.suspended
                or member.org_id != org.id or member.user_id != user.id):
            raise rules.LensError("Team access is no longer available.", 403)
        key = await db.get(ApiKey, snapshot.api_key_id) if snapshot.api_key_id else None
        if snapshot.api_key_id and (key is None or key.state != managed_keys.ACTIVE
                or key.membership_id != member.id or key.org_id != org.id
                or (key.kind == managed_keys.DEFAULT_KIND
                    and key.default_generation != snapshot.api_key_generation)):
            raise rules.LensError("This API key is no longer active. Sign in again.", 401)
        current = _snapshot(Caller(member, user, org, key))
    _require_access(current)
    return current


def _definitions(kind: str, input: dict, cat) -> list[dict]:
    if kind == "onboarding":
        return [{"id": "research", "endpoint_id": rules.ONBOARDING_ENDPOINT,
                 "body": rules.onboarding_body(input["url"]), "query": {}}]
    tasks = []
    for index, question in enumerate(input["questions"]):
        for engine in input["engines"]:
            endpoint_id = rules.ENDPOINTS[engine]
            if engine == "perplexity":
                adapter = cat.adapters.get(endpoint_id)
                if adapter is None:
                    raise rules.LensError("Perplexity's verified request adapter is unavailable.", 503)
                query, body = adapter.to_upstream({"prompt": question, "country": "US"}, ("prompt",))
            else:
                query = {}
                body = {"prompt": question, "include": {"markdown": True},
                        **({"gl": "US", "hl": "en"} if engine == "google" else {"country": "US"})}
            task = {"id": f"{index}:{engine}", "endpoint_id": endpoint_id, "engine": engine,
                    "question": question, "body": body, "query": query}
            if engine == "perplexity":
                task["adapter_hash"] = _hash(adapter.__dict__)
            tasks.append(task)
    return tasks


async def _price_task(caller, task: dict, cat) -> dict:
    ep = cat.by_id.get(task["endpoint_id"])
    if not ep or ep.get("method") != "POST" or ep.get("async"):
        raise rules.LensError("A verified synchronous Lens endpoint is unavailable.", 503)
    data = json.dumps(task["body"], separators=(",", ":")).encode()
    try:
        target = await resolve.resolve_marketplace_target(ep, method="POST",
            query=resolve.QueryValues(tuple(task["query"].items())), has_body=True,
            read_body=route._Bytes(data).read, caller=caller, resolve_call=resolve._resolve_call,
            request_headers={"content-type": "application/json"})
        # The full call authorizer consumes a daily-call slot. Quotes and preflight only read
        # ACLs/deny rules; execute_call alone owns slot consumption and the later money gates.
        async with session_maker() as db:
            access_policy._require_tool_use(caller, target.tool)
            for url in [target.upstream, *oauth_providers.legacy_aliases(target.upstream)]:
                await access_policy.enforce_deny(caller, url, "POST", db, target.tool.project_id)
    except access_policy.AccessPolicyError:
        raise rules.LensError("This request is not allowed by the team's tool policy.", 403) from None
    except CallFailure as exc:
        raise rules.LensError(_failure_detail(exc), exc.status_code) from None
    estimate = money.with_margin(target.estimate_micro) if target.tier == "platform" else 0
    if target.skip_direct or target.tier not in {"platform", "tool", "credential", "anonymous"}:
        raise rules.LensError("The quoted direct provider is unavailable. Try a fresh quote later.", 409)
    if type(estimate) is not int or estimate < 0:
        raise rules.LensError("A verified price is unavailable.", 503)
    if target.tier == "platform":
        cost = cat.cost_view(ep.get("cost"), ep["provider"])
        if not cost or cost.get("usd") is None:
            raise rules.LensError("A verified price is unavailable.", 503)
    return {**task, "method": "POST", "tier": target.tier, "estimate_micro": estimate,
            "endpoint_hash": _hash(ep), "price_hash": _hash(cat.cost_view(ep.get("cost"), ep["provider"]))}


async def quote(caller, kind: str, input: dict) -> dict:
    """Return a JSON-serializable server-owned plan. No HTTP, hold, or provider call."""
    caller = _snapshot(caller)
    _require_access(caller)
    input = rules.normalize_input(kind, input)
    cat = catalog_store.load()
    tasks = [await _price_task(caller, task, cat) for task in _definitions(kind, input, cat)]
    estimate = sum(t["estimate_micro"] for t in tasks)
    maximum = rules.MAX_RESEARCH_MICRO if kind == "onboarding" else rules.MAX_AUDIT_MICRO
    if estimate > maximum:
        raise rules.LensError("This quote exceeds the Lens run limit. Select fewer questions or engines.")
    return {"version": 1, "kind": kind, "input": input, "tasks": tasks,
            "org_id": caller.org_id, "user_id": caller.user.id,
            "estimate_micro": estimate, "currency": "USD", "note": NOTE,
            "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=QUOTE_SECONDS)).isoformat().replace("+00:00", "Z")}


def _failure_detail(exc: CallFailure) -> str:
    if exc.kind in {"credential_missing", "authorization_required", "target_not_found"}:
        return "A required provider connection is unavailable. Connect the provider or ask your team administrator."
    if exc.kind in {"provider_capacity", "provider_paused"}:
        return "The direct provider is temporarily unavailable. Try a fresh quote later."
    if exc.kind == "route_max_cost":
        return "The current reservation exceeds the accepted ceiling. Get a fresh quote."
    if exc.status_code in {401, 403}:
        return "This request is no longer authorized. Check your team permissions."
    if exc.status_code == 402:
        return "The team could not fund this request. Check its billing and spending limits."
    if exc.status_code == 429:
        return "The provider or team usage limit was reached. No automatic retry was made."
    if exc.kind.startswith("idempotency"):
        return "The original request is already in progress or conflicts with this run. Check activity before retrying."
    return "The provider could not complete this request. Successful answers are retained; no automatic retry was made."


async def _validate_plan(caller, plan: dict) -> None:
    try:
        expiry = datetime.fromisoformat(plan["expires_at"].replace("Z", "+00:00"))
        if (plan["version"] != 1 or expiry.tzinfo is None or expiry <= datetime.now(timezone.utc)
                or plan["org_id"] != caller.org_id or plan["user_id"] != caller.user.id):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise rules.LensError("This quote expired or is unavailable. Get a fresh quote.", 409) from None
    # Reconstruct every body from the normalized input. No client-supplied endpoint/body can enter.
    current = await quote(caller, plan.get("kind"), plan.get("input"))
    if (current["tasks"] != plan.get("tasks") or current["estimate_micro"] != plan.get("estimate_micro")
            or current["input"] != plan.get("input")):
        raise rules.LensError("The catalog, price or team connection changed. Get a fresh quote.", 409)


def _micro_header(response) -> int | None:
    value = route._header(response, "X-Treg-Cost-Micro")
    return int(value) if value and value.isascii() and value.isdecimal() and len(value) <= 18 else None


def _summarize(result: dict) -> None:
    attempts = result["attempts"]
    result["known_cost_micro"] = sum(a["charged_micro"] for a in attempts if a["charged_micro"] is not None)
    result["cost_known"] = all(a["charged_micro"] is not None for a in attempts)
    result["total_cost_micro"] = result["known_cost_micro"] if result["cost_known"] else None


def _bound_answer(answer: dict) -> dict:
    # Worst-case ASCII JSON remains bounded even for emoji, escaped text and long citation URLs.
    while len(json.dumps(answer, ensure_ascii=True).encode()) > 40 * 1024:
        answer["truncated"] = True
        if len(answer["answer"]) > 1000:
            answer["answer"] = answer["answer"][:max(1000, len(answer["answer"]) * 3 // 4)]
        elif answer["citations"]:
            answer["citations"].pop()
        else:
            break
    return answer


async def execute(caller, plan: dict, run_id: str, http, client_ip: str, *, persist=None) -> dict:
    """Execute one durably claimed plan. persist returns False when no more work is allowed.

    Each call reference and running marker is acknowledged before submit. A lost persist stops
    scheduling; the owner retains the ambiguous running marker for interrupted-run recovery.
    """
    snapshot = _snapshot(caller)
    current = await _fresh_caller(snapshot)
    await _validate_plan(current, plan)
    if not isinstance(run_id, str) or not run_id or len(run_id) > 128:
        raise rules.LensError("A valid claimed run is required.")
    result = {"kind": plan["kind"], "input": copy.deepcopy(plan["input"]), "results": [], "errors": [],
              "known_cost_micro": 0, "total_cost_micro": 0, "cost_known": True, "completed_at": None,
              "attempts": [{"id": t["id"], "endpoint_id": t["endpoint_id"], "state": "queued", "charged_micro": 0,
                  **{k: t[k] for k in ("engine", "question") if k in t}} for t in plan["tasks"]]}
    persist_lock = asyncio.Lock()
    stopped = asyncio.Event()

    async def save() -> bool:
        async with persist_lock:
            _summarize(result)
            if persist is not None:
                try:
                    accepted = await persist(copy.deepcopy(result))
                except BaseException:
                    stopped.set()
                    raise
                if accepted is False:
                    stopped.set()
                    return False
            return True

    async def leg(task, attempt):
        if stopped.is_set():
            attempt["state"] = "not_attempted"
            return
        context = None
        response = None
        try:
            caller_now = await _fresh_caller(snapshot)
            # Credentials/ACLs and the exact quoted tier/price are rechecked before every submit.
            refreshed = await _price_task(caller_now, task, catalog_store.load())
            if refreshed != task:
                raise rules.LensError("The price or team connection changed. Get a fresh quote.", 409)
            data = json.dumps(task["body"], separators=(",", ":")).encode()
            cap = task["estimate_micro"]
            key = "lens:" + hashlib.sha256(f"{run_id}:{task['id']}".encode()).hexdigest()
            headers = ((b"content-type", b"application/json"), (b"content-length", str(len(data)).encode()),
                (b"x-treg-client", b"lens"), (b"x-treg-meta", b"feature=lens"),
                (b"cache-control", b"no-cache"), (b"idempotency-key", key.encode()),
                (b"x-treg-route-max-cost", f"{cap // 1_000_000}.{cap % 1_000_000:06d}".encode()))
            context = service.create_call_context(CallInput(method=task["method"], raw_rest=task["endpoint_id"],
                raw_headers=headers, query_items=tuple(task["query"].items()), raw_query=urlencode(task["query"]),
                body=route._Bytes(data), caller=caller_now, client_ip=client_ip, catalog_only=True))
            attempt.update(state="running", call_ref=context.call_ref, charged_micro=None)
            if not await save() or stopped.is_set():
                attempt.update(state="not_attempted", charged_micro=0)
                return
            async with asyncio.timeout(CALL_SECONDS):
                response = await service.execute_call(context, http)
                attempt["status"] = response.status
                attempt["charged_micro"] = None if response.status == 202 else (
                    context.cost_micro if context.cost_micro is not None else _micro_header(response))
                if task["tier"] != "platform" and response.status != 202:
                    attempt["charged_micro"] = 0
                buf = bytearray()
                async for chunk in response.body_stream:
                    if len(buf) + len(chunk) > rules.MAX_RESPONSE_BYTES:
                        raise rules.LensError("The provider response exceeded the evidence limit.", 502)
                    buf.extend(chunk)
                if response.status == 202:
                    raise rules.LensError("The original request is still processing. Its charge is unsettled; check activity before retrying.", 202)
                if not 200 <= response.status < 300:
                    raise rules.LensError("The provider returned an error. Successful answers are retained.", 502)
                try:
                    document = json.loads(buf)
                except (ValueError, UnicodeDecodeError, RecursionError):
                    raise rules.LensError("The provider returned unreadable evidence.", 502) from None
                if plan["kind"] == "onboarding":
                    research = rules.normalize_research(document, plan["input"]["url"])
                    result["research"] = research
                    if not research["brand"] or not research["questions"]:
                        raise rules.LensError("Research returned incomplete suggestions. Review the evidence or set up manually.", 502)
                else:
                    answer = rules.normalize_answer(document, plan["input"], task["question"], task["engine"])
                    answer.update(id=context.call_ref, charged_micro=attempt["charged_micro"],
                        cost_known=attempt["charged_micro"] is not None, completed_at=_stamp())
                    result["results"].append(_bound_answer(answer))
                attempt["state"] = "completed"
                if attempt["charged_micro"] is None:
                    stopped.set()
        except asyncio.CancelledError:
            attempt.update(state="unknown", detail="This request was interrupted. Check activity before retrying.")
            if context is not None and context.cost_micro is not None:
                attempt["charged_micro"] = context.cost_micro
            stopped.set()
            raise
        except (rules.LensError, CallFailure, TimeoutError) as exc:
            if isinstance(exc, CallFailure):
                detail = _failure_detail(exc)
                if context is not None and context.cost_micro is not None:
                    attempt["charged_micro"] = context.cost_micro
                elif isinstance(exc, (IntakeFailed, ResolutionFailed, AuthorizationFailed, ReservationFailed)):
                    attempt["charged_micro"] = 0
            elif isinstance(exc, rules.LensError):
                detail = exc.detail
            else:
                detail = "The request timed out. Check activity before retrying; no retry was made."
                if context is not None and context.cost_micro is not None:
                    attempt["charged_micro"] = context.cost_micro
            attempt.update(state="unknown" if attempt["charged_micro"] is None else "error", detail=detail)
            if attempt["charged_micro"] is None:
                stopped.set()
            result["errors"].append({k: attempt[k] for k in ("engine", "question", "detail", "call_ref", "charged_micro") if k in attempt})
        except Exception:
            # Arbitrary exceptions can contain credentials or provider bodies; never expose them.
            detail = "The request could not be completed. Check activity before retrying."
            attempt.update(state="unknown", detail=detail)
            if context is not None and context.cost_micro is not None:
                attempt["charged_micro"] = context.cost_micro
            stopped.set()
            result["errors"].append({k: attempt[k] for k in ("engine", "question", "detail", "call_ref", "charged_micro") if k in attempt})
        finally:
            try:
                if response is not None:
                    await response.close()
            finally:
                await save()

    # Waves bound provider concurrency and ensure uncertain work stops every later wave.
    for offset in range(0, len(plan["tasks"]), MAX_PARALLEL):
        if stopped.is_set():
            break
        async with asyncio.TaskGroup() as group:
            for task, attempt in zip(plan["tasks"][offset:offset + MAX_PARALLEL], result["attempts"][offset:offset + MAX_PARALLEL]):
                group.create_task(leg(task, attempt))
    for attempt in result["attempts"]:
        if attempt["state"] == "queued":
            attempt.update(state="not_attempted", charged_micro=0,
                           detail="Not submitted because an earlier request needs review.")
    result["completed_at"] = _stamp()
    await save()
    return result
