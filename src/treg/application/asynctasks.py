"""Orchestrate deferred settlement for metered asynchronous provider tasks."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import timedelta

import httpx
from urllib.parse import quote
from sqlalchemy import select

from .. import archive, oauth_providers
from ..domain import asynctasks
from ..domain import money as ledger
from ..domain.catalog import store as catalog_store
from ..domain.money import settlement
from ..infra.db import session_maker
from ..infra.upstream.relay import relay
from ..models import AsyncResourceRecord, AsyncTaskRecord, Hold, Tool
from ..timeutil import utcnow_naive
from .call.resolve import _host_of, _platform_bindings
from .call.types import UpstreamRequest


log = logging.getLogger("treg.asynctasks")
DEFAULT_LIMIT = 50
GLOBAL_CONCURRENCY = 8
PROVIDER_CONCURRENCY = 2
# Terminal task JSON is small; a provider that streams more than this is not answering a poll.
MAX_POLL_BODY_BYTES = 2 * 1024 * 1024


def _json_value(value: object) -> object:
    """Detach catalog objects from YAML scalar types before storing them in a JSON column."""
    return json.loads(json.dumps(value, default=lambda item: item.isoformat()))


async def defer_submission(mk, body: bytes, org_id: int) -> int:
    """Persist the pending task before allowing the request path to leave its hold open."""
    now = utcnow_naive()
    # The request path has already established that this body is JSON and carries the task id
    # (`_submission_accepted`); a failure here is a programming error and surfaces as one.
    extracted = asynctasks.extract_submission(mk.async_descriptor or {}, json.loads(body))
    task_id, poll_url, error = extracted.task_id, extracted.poll_url, ""
    due = now + timedelta(seconds=60)
    async with session_maker() as db:
        hold = await db.get(Hold, mk.call_id)
        if hold is None:
            raise RuntimeError("async submission hold disappeared before persistence")
        row = AsyncTaskRecord(
            call_id=str(mk.call_id), org_id=org_id, provider=mk.provider,
            endpoint_id=mk.endpoint_id, task_id=task_id, poll_url=poll_url,
            reserved_micro=hold.amount_micro, descriptor=_json_value(mk.async_descriptor or {}),
            settlement_basis=_json_value(mk.settlement_basis),
            created_at=now, next_check_at=due, error=error,
        )
        db.add(row)
        if task_id is not None:
            poll = (row.descriptor or {}).get("poll") or {}
            if poll.get("endpoint"):
                await _remember_resource(db, row, f"poll:{poll['endpoint']}", task_id)
            result = (row.descriptor or {}).get("result") or {}
            if (result.get("fetch")
                    and (result.get("fetch_param") or {}).get("value_from")
                    == (row.descriptor or {}).get("id_from")):
                await _remember_resource(db, row, f"fetch:{result['fetch']}", task_id)
        await db.commit()
    mk.call_id = None
    return int(hold.amount_micro)


def _result_id(descriptor: dict, document: object) -> str | None:
    fetch = asynctasks.artifact(descriptor, document).get("fetch")
    return str(fetch["value"]) if fetch and fetch.get("value") not in (None, "") else None


def _dotted(document: object, path: str) -> object:
    value = document
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


async def _remember_resource(db, row: AsyncTaskRecord, kind: str, resource_id: str) -> None:
    exists = (await db.execute(select(AsyncResourceRecord.id).where(
        AsyncResourceRecord.org_id == row.org_id,
        AsyncResourceRecord.provider == row.provider,
        AsyncResourceRecord.resource_kind == kind,
        AsyncResourceRecord.resource_id == resource_id,
    ))).scalar_one_or_none()
    if exists is None:
        db.add(AsyncResourceRecord(
            org_id=row.org_id, provider=row.provider, resource_kind=kind,
            resource_id=resource_id, source_call_id=row.call_id,
        ))


async def remember_platform_resources(
    org_id: int, provider: str, call_id: str, rule: dict, body: bytes,
) -> int:
    """Persist opaque ids a successful call created on treg's shared provider account."""
    try:
        document = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 0
    resources = {
        (str(item.get("kind") or ""), str(value))
        for item in rule.get("produces") or []
        if isinstance(item, dict)
        and (value := _dotted(document, str(item.get("path") or ""))) not in (None, "")
    }
    if not resources:
        return 0
    added = 0
    async with session_maker() as db:
        for kind, resource_id in resources:
            exists = (await db.execute(select(AsyncResourceRecord.id).where(
                AsyncResourceRecord.org_id == org_id,
                AsyncResourceRecord.provider == provider,
                AsyncResourceRecord.resource_kind == kind,
                AsyncResourceRecord.resource_id == resource_id,
            ))).scalar_one_or_none()
            if exists is None:
                db.add(AsyncResourceRecord(
                    org_id=org_id, provider=provider, resource_kind=kind,
                    resource_id=resource_id, source_call_id=call_id,
                ))
                added += 1
        await db.commit()
    return added


async def remember_result_from_poll(call_id: str, body: bytes) -> bool:
    """Learn a fetch-mode result id from a caller's already-authorized platform poll.

    The CLI may observe success before the minute worker does. Persisting the id here lets its next
    retrieval pass the same org boundary; malformed/nonterminal responses simply teach nothing.
    """
    try:
        document = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    async with session_maker() as db:
        row = await db.get(AsyncTaskRecord, call_id, with_for_update=True)
        if row is None or asynctasks.classify_terminal(row.descriptor, document) != "success":
            return False
        result_id = _result_id(row.descriptor, document)
        if result_id is None:
            return False
        row.result_id = result_id
        fetch = (row.descriptor or {}).get("result") or {}
        if fetch.get("fetch"):
            await _remember_resource(db, row, f"fetch:{fetch['fetch']}", result_id)
        await db.commit()
        return True


async def views_for(org_id: int, call_ids: list[str]) -> dict[str, dict]:
    """The task's own account of each metered async call, keyed by call id, for activity displays.

    The audit row froze the reserve as "charged" at submission; this is where the display learns
    what actually happened (settled amount, refund, 24-hour fallback) and what the caller bought.
    Terminal JSON comes from the archive; a settled task whose recording was shed still reports its
    money truthfully, only without a link.
    """
    if not call_ids:
        return {}
    async with session_maker() as db:
        rows = (await db.execute(
            select(AsyncTaskRecord).where(
                AsyncTaskRecord.org_id == org_id,
                AsyncTaskRecord.call_id.in_(list(call_ids))))).scalars().all()
    if not rows:
        return {}
    documents = await archive.load_terminal_responses(
        [(row.call_id, row.endpoint_id) for row in rows if row.status == asynctasks.SETTLED])
    views: dict[str, dict] = {}
    for row in rows:
        view = {
            "status": row.status, "task_id": row.task_id,
            "reserved_micro": row.reserved_micro, "settled_micro": row.settled_micro,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "completed_at": row.completed_at.isoformat() if row.completed_at else None,
            "error": row.error or None,
            "result_url": None, "fetch_command": None,
            "ttl_note": ((row.descriptor.get("result") or {}).get("ttl_note") or None),
        }
        body = documents.get(row.call_id)
        if body is not None:
            try:
                found = asynctasks.artifact(row.descriptor, json.loads(body))
            except ValueError:
                found = {}
            view["result_url"] = found.get("result_url")
            fetch = found.get("fetch")
            if fetch:
                view["fetch_command"] = asynctasks.fetch_command(fetch)
        views[row.call_id] = view
    return views


@dataclass(frozen=True)
class TickResult:
    claimed: int = 0
    settled: int = 0
    released: int = 0
    backed_off: int = 0
    timed_out: int = 0


async def _claim_due(limit: int, now) -> list[str]:
    async with session_maker() as db:
        rows = (await db.execute(
            select(AsyncTaskRecord)
            .where(AsyncTaskRecord.status == asynctasks.PENDING,
                   AsyncTaskRecord.next_check_at <= now)
            .order_by(AsyncTaskRecord.next_check_at, AsyncTaskRecord.call_id)
            .with_for_update(skip_locked=True).limit(limit)
        )).scalars().all()
        for row in rows:
            row.attempts += 1
            row.next_check_at = now + timedelta(seconds=60)
        await db.commit()
        return [row.call_id for row in rows]


def _poll_target(row: AsyncTaskRecord) -> tuple[str, str, list[tuple[str, str]]]:
    poll = row.descriptor.get("poll") or {}
    if row.poll_url:
        return "GET", row.poll_url, []
    endpoint_id = poll.get("endpoint")
    endpoint = catalog_store.load().by_id.get(endpoint_id)
    if not endpoint:
        raise RuntimeError(f"poll endpoint {endpoint_id!r} is not catalogued")
    provider = oauth_providers.get(row.provider)
    if provider is None or not provider.base_url:
        raise RuntimeError(f"provider {row.provider!r} is not relayable")
    url = provider.base_url.rstrip("/") + "/" + endpoint["path"].lstrip("/")
    query: list[tuple[str, str]] = []
    param = poll.get("param") or {}
    if param:
        # The declared location decides, never a coincidental placeholder; a path value is
        # percent-encoded so a provider-controlled task id cannot add URL syntax.
        name, value = str(param.get("name") or ""), str(row.task_id)
        if param.get("in") == "pathParams":
            marker = "{" + name + "}"
            if marker not in url:
                raise RuntimeError(f"poll endpoint {endpoint_id!r} has no {marker} placeholder")
            url = url.replace(marker, quote(value, safe=""))
        else:
            query.append((name, value))
    return str(endpoint.get("method") or "GET"), url, query


async def _poll(row: AsyncTaskRecord, client: httpx.AsyncClient) -> tuple[int, bytes]:
    provider = oauth_providers.get(row.provider)
    if provider is None:
        raise RuntimeError(f"provider {row.provider!r} is not registered")
    method, url, query = _poll_target(row)
    # The query travels as `query_items`: the relay composes the upstream URL from those (it
    # forwards a URL's own query string nowhere), so a query-parameter poll (MiniMax v1
    # `?task_id=`) appended to the URL reached the provider empty - "invalid params" until the
    # 24-hour deadline. Path-parameter polls never showed it. Live 2026-09-02.
    tool = Tool(org_id=row.org_id, name=row.endpoint_id, owner="treg-worker",
                base_url=provider.base_url, host=_host_of(provider.base_url),
                bindings=_platform_bindings(provider))

    async def empty():
        if False:
            yield b""

    request = UpstreamRequest(method=method, raw_headers=(), query_items=tuple(query),
                              body_stream=empty, has_body=False)
    response = await relay(request, url, tool, [], client, force_identity=True)
    try:
        chunks, size = [], 0
        async for chunk in response.body_stream:
            size += len(chunk)
            if size > MAX_POLL_BODY_BYTES:
                raise ValueError(f"poll response exceeds {MAX_POLL_BODY_BYTES} bytes")
            chunks.append(chunk)
        return response.status, b"".join(chunks)
    finally:
        await response.close()


async def _finish(call_id: str, outcome: str, document: object | None, now) -> str:
    async with session_maker() as db:
        row = await db.get(AsyncTaskRecord, call_id, with_for_update=True)
        if row is None or row.status != asynctasks.PENDING:
            return "noop"
        if outcome in ("success", "failure", "timed_out") and await db.get(Hold, call_id) is None:
            # The request path already closed this hold (cancelled at the commit boundary, or
            # reaped): there is no money left to move, and a row "settled" at zero would lie.
            row.status = asynctasks.RELEASED
            row.settled_micro = 0
            row.error = "hold was already closed before the terminal state"
            row.completed_at = now
            await db.commit()
            log.warning("async task %s reached %s but its hold was already closed", call_id, outcome)
            return row.status
        if outcome == "success":
            evidence = {"terminal": document}
            row.result_id = _result_id(row.descriptor, document)
            fetch = (row.descriptor or {}).get("result") or {}
            if row.result_id is not None and fetch.get("fetch"):
                await _remember_resource(db, row, f"fetch:{fetch['fetch']}", row.result_id)
            raw = settlement.settle(row.settlement_basis, evidence)
            unobserved = (row.settlement_basis["amount"]["kind"] == "usage"
                          and settlement.usage_evidence(row.settlement_basis, evidence) is None)
            if unobserved:
                row.error = "usage field missing from the terminal response; settled at the reserve"
                log.error("ASYNC USAGE UNOBSERVED: call %s on %s succeeded but %s carried no usage "
                          "figure; settled at the reserve - check the provider's response shape",
                          row.call_id, row.provider, row.endpoint_id)
            row.settled_micro = await ledger.settle_in_transaction(db, row.call_id, raw, meta={
                "provider": row.provider, "cost_source": row.settlement_basis["amount"]["kind"],
                "async_task": True, **({"reconcile_review": True} if unobserved else {}),
            })
            row.status = asynctasks.SETTLED
        elif outcome == "failure":
            await ledger.release_in_transaction(db, row.call_id, reason="async_task_failed",
                                                meta={"provider": row.provider, "async_task": True})
            row.settled_micro = 0
            row.status = asynctasks.RELEASED
        elif outcome == "timed_out":
            # No terminal state in 24 hours means treg does not know whether the caller got
            # anything. The platform absorbs that uncertainty: the hold goes back to the team in
            # full, the upstream charge (if any) is treg's, and the row is flagged for a human.
            # Charging the reserve here would bill a customer for an outcome nobody observed.
            await ledger.release_in_transaction(db, row.call_id, reason="async_task_timed_out",
                                                meta={"provider": row.provider, "async_task": True,
                                                      "reconcile_review": True})
            row.settled_micro = 0
            row.status = asynctasks.TIMED_OUT
            row.error = "terminal state not observed within 24 hours; hold released, platform absorbs"
            log.error("ASYNC TASK TIMED OUT: call %s on %s (%s) had no terminal state in 24h; "
                      "released %d micro-USD to the team, platform absorbs the upstream charge - "
                      "check whether the provider changed its status field",
                      row.call_id, row.provider, row.endpoint_id, row.reserved_micro)
        else:
            row.next_check_at = asynctasks.next_check(now, row.attempts)
            await db.commit()
            return "backed_off"
        row.completed_at = now
        await db.commit()
        return row.status


async def _process(call_id: str, client: httpx.AsyncClient) -> str:
    now = utcnow_naive()
    async with session_maker() as db:
        row = await db.get(AsyncTaskRecord, call_id)
        if row is None or row.status != asynctasks.PENDING:
            return "noop"
        if asynctasks.expired(row.created_at, now):
            return await _finish(call_id, "timed_out", None, now)
        if row.error:
            return "backed_off"
        snapshot = row.model_copy()
    try:
        status, body = await _poll(snapshot, client)
        if not 200 <= status < 300:
            # Only a successful poll is evidence. A 404 or 401 body that happens to carry
            # "status": "succeeded" is an error envelope, not a terminal state; the CLI treats
            # every non-2xx the same way, and money must not depend on an error page's fields.
            log.warning("async poll for call %s returned HTTP %s; backing off", call_id, status)
            return await _finish(call_id, "progress", None, now)
        document = json.loads(body)
        outcome = asynctasks.classify_terminal(snapshot.descriptor, document)
        result = await _finish(call_id, outcome, document, now)
        if outcome in ("success", "failure"):
            await archive.store_terminal_response(
                snapshot.call_id, snapshot.provider, snapshot.endpoint_id, status, body)
        return result
    except Exception as exc:  # noqa: BLE001 - one row's failure must never abort the tick
        # relay() raises GatewayFailed (an unset platform key, an SSRF refusal), httpx raises its
        # own, JSON raises ValueError: all mean "no evidence this tick". Back off and say why; the
        # deadline still ends it, and the whole tick keeps serving the other rows.
        log.warning("async poll failed for call %s: %s: %s", call_id, type(exc).__name__, exc)
        return await _finish(call_id, "progress", None, now)


async def settle_due(*, limit: int = DEFAULT_LIMIT, client: httpx.AsyncClient | None = None) -> TickResult:
    """Claim one worker tick and process network waits under global and provider caps."""
    now = utcnow_naive()
    call_ids = await _claim_due(limit, now)
    if not call_ids:
        return TickResult()
    global_sem = asyncio.Semaphore(GLOBAL_CONCURRENCY)
    provider_sems: dict[str, asyncio.Semaphore] = {}
    owned = client is None
    client = client or httpx.AsyncClient(timeout=60)

    async def run(call_id: str) -> str:
        async with session_maker() as db:
            row = await db.get(AsyncTaskRecord, call_id)
            provider = row.provider if row else ""
        sem = provider_sems.setdefault(provider, asyncio.Semaphore(PROVIDER_CONCURRENCY))
        async with global_sem, sem:
            return await _process(call_id, client)

    try:
        results = await asyncio.gather(*(run(call_id) for call_id in call_ids), return_exceptions=True)
    finally:
        if owned:
            await client.aclose()
    outcomes = []
    for call_id, result in zip(call_ids, results):
        if isinstance(result, BaseException):  # _process already guards; this is the last net
            log.error("async task %s: tick-level failure: %s", call_id, result, exc_info=result)
            outcomes.append("backed_off")
        else:
            outcomes.append(result)
    return TickResult(
        claimed=len(call_ids), settled=outcomes.count(asynctasks.SETTLED),
        released=outcomes.count(asynctasks.RELEASED),
        backed_off=outcomes.count("backed_off"), timed_out=outcomes.count(asynctasks.TIMED_OUT),
    )
