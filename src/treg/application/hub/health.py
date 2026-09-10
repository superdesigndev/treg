"""Health of a hub tool (docs/HUB-DECISIONS.md round 2 q8, round 5 q7), and the scheduled check.

Health is DERIVED, never stored: a version is `failing` when its last three runs (callers' runs
and scheduled checks alike) all failed, else `ok`; `unknown` before any run. Nothing to keep in
sync, nothing to migrate. A failing tool stays callable; the public page and `catalog_get` say
the state, and the maker's next passing run or check clears it.

The scheduled check runs `check.json` the way publish did, but from `treg-worker hub check` where
no maker is signed in: it builds the maker's identity from the database (the membership of the
person who published, else an owner of the team) and runs the tool through the runner directly,
charged to the maker's balance at step prices, seller price never charged (the caller IS the
maker). Its run is a `HubRun` row like any other, with `caller_email = "hub-check"`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...models import HubRun, HubTool, Membership, Org, User

FAILS_IN_A_ROW = 3
CHECK_EMAIL = "hub-check"


@dataclass(frozen=True)
class Health:
    state: str            # ok | failing | unknown
    fails_in_a_row: int
    last_run_at: str | None
    last_check: dict | None


async def health_of(db: AsyncSession, tool_id: str, version: int, check_result: dict | None) -> Health:
    rows = (await db.execute(
        select(HubRun.status, HubRun.started_at)
        .where(HubRun.tool_id == tool_id, HubRun.version == version)
        .order_by(HubRun.started_at.desc()).limit(FAILS_IN_A_ROW))).all()
    if not rows:
        return Health("unknown", 0, None, check_result)
    fails = 0
    for status, _ in rows:
        if status == "ok":
            break
        fails += 1
    state = "failing" if fails >= FAILS_IN_A_ROW else "ok"
    return Health(state, fails, rows[0][1].isoformat() if rows[0][1] else None, check_result)


def verdict_from(row: HubTool, status_code: int, body: Any, headers: dict[str, str]) -> dict[str, Any]:
    """The check's verdict from a run's answer: the rule publish uses, in one place."""
    from datetime import datetime, timezone
    v: dict[str, Any] = {"run_id": headers.get("x-treg-run-id") or headers.get("X-Treg-Run-Id"),
                         "checked_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                         "status_code": status_code,
                         "charged_micro": int(headers.get("x-treg-cost-micro") or headers.get("X-Treg-Cost-Micro") or 0)}
    if status_code != 200:
        detail = body.get("detail", body) if isinstance(body, dict) else body
        v["status"] = "failed"
        v["error"] = detail if isinstance(detail, dict) else {"message": str(detail)[:600]}
        if status_code == 402:
            v["error"] = {**v["error"], "hint": "the check runs on your own balance at the normal step prices; top up and publish again"}
        return v
    output = body.get("output") if isinstance(body, dict) else None
    missing = [f for f in row.check.get("fields", []) if not isinstance(output, dict) or output.get(f) in (None, "", [], {})]
    min_rows = int(row.check.get("min_rows", 0) or 0)
    rows_short = None
    if min_rows and isinstance(output, dict):
        lists = [x for x in output.values() if isinstance(x, list)]
        if not lists or len(lists[0]) < min_rows:
            rows_short = len(lists[0]) if lists else 0
    v["trace"] = body.get("trace", []) if isinstance(body, dict) else []
    if missing or rows_short is not None:
        v["status"] = "failed"
        v["error"] = {"error": "check_failed", **({"missing": missing} if missing else {}),
                      **({"rows": rows_short, "min_rows": min_rows} if rows_short is not None else {}),
                      "message": "the run answered, but not what check.json requires"}
    else:
        v["status"] = "passed"
    return v


async def _maker_caller(db: AsyncSession, row: HubTool):
    """The identity the scheduled check runs as: the publisher's membership in the maker's team,
    else an owner's. None when the team has nobody left (then the check is skipped, not faked)."""
    from ...domain.identity.access import Caller
    org = await db.get(Org, row.org_id)
    if org is None:
        return None
    user = (await db.execute(select(User).where(User.email == row.created_by))).scalars().first()
    membership = None
    if user is not None:
        membership = (await db.execute(select(Membership).where(
            Membership.org_id == org.id, Membership.user_id == user.id))).scalars().first()
    if membership is None:
        membership = (await db.execute(select(Membership).where(
            Membership.org_id == org.id, Membership.role.in_(("owner", "admin")))
            .order_by(Membership.id.asc()))).scalars().first()
        user = await db.get(User, membership.user_id) if membership else None
    if membership is None or user is None:
        return None
    return Caller(membership=membership, user=user, org=org)


async def check_as_maker(db: AsyncSession, row: HubTool, upstream_client) -> dict[str, Any]:
    """Run `check.json` for one version as its maker, through the runner, and store the verdict on
    the row. Returns the verdict. Does not commit."""
    from ..call.service import create_call_context, execute_call
    from ..call.types import CallFailure, CallInput, CallerSnapshot
    from . import runner as hub_runner

    caller = await _maker_caller(db, row)
    await db.commit()   # non-negotiable 3: the selects above autobegan; nothing stays open during the run
    if caller is None:
        v = {"status": "skipped", "error": {"message": "the maker's team has no member left to run the check as"}}
        row.check_result = {**(row.check_result or {}), **v}
        db.add(row)
        return v
    payload = json.dumps(row.check.get("inputs", {})).encode()
    from dataclasses import replace
    from ..call.types import UserSnapshot
    snapshot = CallerSnapshot.capture(caller)
    # The run row names the CHECK, not the person whose membership it ran under, so the run log
    # tells a schedule from a person. Money and rules still follow the membership.
    snapshot = replace(snapshot, user=UserSnapshot(id=snapshot.user.id, email=CHECK_EMAIL))
    call_input = CallInput(
        method="POST", raw_rest=f"{row.tool_id}@{row.version}",
        raw_headers=((b"x-treg-client", b"hub-check"), (b"content-type", b"application/json")),
        query_items=(), raw_query="", body=_Bytes(payload),
        caller=snapshot, client_ip="127.0.0.1", catalog_only=False)
    context = create_call_context(call_input)
    try:
        response, charged = await hub_runner.run_hub_tool(
            context, row, payload, lambda name: "5.00" if name == hub_runner.RUN_MAX_COST_HEADER else None,
            upstream_client, execute_call, audit_client="hub-check")
        raw = b"".join([chunk async for chunk in response.body_stream])
        await response.close()
        body = json.loads(raw) if raw else {}
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in response.raw_headers}
        headers.setdefault("x-treg-cost-micro", str(charged))
        v = verdict_from(row, response.status, body, headers)
    except CallFailure as exc:
        v = verdict_from(row, exc.status_code, {"detail": exc.detail}, {})
    v["scheduled"] = True
    # A failing check never retires a tool by itself (round 2 q8): health says it, the maker decides.
    row.check_result = v
    db.add(row)
    return v


class _Bytes:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def stream(self):
        yield self._data

    async def read(self) -> bytes:
        return self._data
