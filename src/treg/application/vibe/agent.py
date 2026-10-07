"""Vibe-it's agent: one maker message in, the agent's turns out (docs/context/architecture/vibe-it.md).

The agent's tools are existing roads taken AS THE SIGNED-IN MAKER: the find engine and the catalog's
contract, the team's tools, the hub's validator, `POST /hub/run`, publish, and the app. The requests go
in-process to the running application with the maker's own cookie and team header, so the agent
can never do what the maker could not. What costs money or changes the team (a test run, publishing,
the app, its password) is never done by the agent itself: it asks, the page shows the maker a button
(`pending`), and the maker's press runs it (`act`), recorded as an event the agent reads next turn.
A maker may let the agent test-run without asking, per conversation (`auto_test`).

Limits on every message: `MAX_TURNS` model turns, `MAX_TOOL_CALLS` tool calls, and the person's
model budget, checked before each turn. No database connection is held while the model or a tool
call is in flight (AGENTS.md non-negotiable 3): the session commits before each one. The maker can
stop it: the flag is read before each turn and step and every `STOP_POLL_S` while the model streams.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import get_settings
from ...domain.catalog import store as catalog_store
from ...domain.hub import ManifestError, as_files
from ...infra import db as database
from ...infra import llm
from ...models import HubTool, Org, VibeSession
from .. import catalog_find as find
from .. import hub as hub_app
from . import (add_message, clean_draft, copy_draft, drafts_of, left_micro, mark_published, messages, of_user,
               set_draft, set_pending, set_tool, spend, stop_wanted, touch_run, view_message)

MAX_TURNS = 12
MAX_TOOL_CALLS = 24
RESULT_MAX = 6000            # characters of one tool result the model reads back
OLD_RESULT_MAX = 1200        # ...and of a result from an earlier message, read again every turn
DRAFT_NOTE_MAX = 40_000      # characters of the files sent with every turn; past it, read_files
READ_MAX = 30_000            # characters of one file one read_files call returns
# One message may spend this much of treg's model money before the agent stops and asks to go on:
# a loop rewriting long files can otherwise spend a person's whole budget on one message.
MESSAGE_CAP_MICRO = 500_000
STOP_POLL_S = 1.5
SEARCH_ROWS = 8
# One find is one judge request on treg's key: a fixed charge against the person's budget, since
# the judge reports no cost of its own.
FIND_COST_MICRO = 2_000
# When the gateway reports no cost, count the tokens at a deliberately high rate: the budget is
# spent too early rather than never.
EST_IN_PER_TOKEN_MICRO = 5
EST_OUT_PER_TOKEN_MICRO = 25

Emit = Callable[[dict], Awaitable[None]]


async def _quiet(_: dict) -> None:
    return None


_WEB = Path(__file__).resolve().parents[2] / "web"


def _hub_rules() -> str:
    """The hub's own instructions for agents (the publish section of skill.md), so vibe-it's agent
    and every installed agent read the same rules."""
    try:
        text = (_WEB / "skill.md").read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r"<!--hub-->\n(## Task — publish a tool made of tools.*?)<!--/hub-->", text, flags=re.S)
    section = m.group(1) if m else ""
    section = re.sub(r"<!--/?hubapps-->\n?", "", section)
    return section.replace("{BASE}", get_settings().public_url.rstrip("/"))


SYSTEM = """You are vibe-it, treg's builder. You help a maker turn an idea into a hub tool: a tool \
made of other tools that their team publishes on treg and anyone's agent can call.

How to work:
- Talk first. Ask what the tool should take in and give back, who it is for, and what a good \
answer looks like. Keep questions short, a few at a time.
- Find the building blocks with catalog_search: describe the JOB in plain words ("recent reddit posts \
mentioning a brand"), not keywords. It answers with a verdict: `strong` (a good fit), `closest` \
(nothing fits well; say so), `none` or `keyword` (unjudged). Read candidates with catalog_get (inputs, \
price, how reliable). Propose the tools for each step with their prices, and say what one run will \
likely cost. Prefer the cheapest reliable option; say when a step is free.
- Every search row and catalog_get say whether this team can call the tool now (`callable`). Build \
only on tools it can call. When no provider for a step is callable, stop and tell the maker plainly: \
show `why_not` and the `fix` command, and offer a provider they could connect. Never cycle through \
providers hoping one works: an uncallable step fails every test run the same way. When the catalog \
has nothing that fits, say so, and offer the maker's own tool (their API, their key) instead.
- In a script, when a ctx.call fails, put its status and a short piece of its body (`r.text`) in the \
error you throw or in ctx.log, so a failed test run says why.
- The maker's own tools (my_tools) run on their own keys and cost callers nothing.
- To change a tool the team already published, load_my_tool first, then edit: never rewrite it from \
memory. To continue an unpublished draft from another conversation, load_draft (my_tools lists them).
- Decide steps vs script: JSON steps for a fixed chain, a script (run.js) for fallbacks, loops, \
filtering or arithmetic. A script can read an uploaded CSV as ctx.data when the maker added data.csv.
- Write the files with write_files, sending only the files you change: never resend an unchanged \
file. Fix every refusal it returns (it names the field and the rule). The current files come with \
every turn; when they say they were cut, read_files reads one in full.
- test_run, publish, app_on and app_password show the maker a button; the maker decides. Call one \
when it is the next step and say in one short line what it does and costs; then stop and wait. \
The maker's choice comes back as a note in the conversation. If test_run comes back with a result \
(the maker allowed test runs without asking), read it and fix what is wrong.
- Publish only when the maker wants it. After publishing, offer the app (a web page for the tool).
- Never ask for, accept or write an API key, secret or password. A key the team does not hold is \
registered by the maker first (treg secret add, treg tool add), then named in `uses`. An app password \
is typed by the maker into the field app_password shows; you never see it.
- Write Markdown: short paragraphs, lists, `code`, fenced code blocks; no tables wider than 4 columns. \
Be concise.

The team you build for is `{team}`; its tools become `{team}.<name>`.

The hub's rules, as every agent reads them:

{rules}
"""

TOOLS: list[dict[str, Any]] = [
    {"name": "catalog_search", "description": "Find catalog tools for a job described in plain words. Returns a verdict and rows: "
     "id, what it does, price, and whether this team can call it now.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "catalog_get", "description": "Read one catalog tool's contract: inputs, method and path, price, reliability, docs, "
     "and whether this team can call it.",
     "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "my_tools", "description": "The maker's team's own tools (their keys) and its published hub tools. Never secrets.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "load_draft", "description": "Copy one of the maker's unpublished drafts from another conversation (my_tools "
     "lists them) into this one, to continue it here. Replaces the current draft.",
     "parameters": {"type": "object", "properties": {"conversation_id": {"type": "integer"}}, "required": ["conversation_id"]}},
    {"name": "load_my_tool", "description": "Load one of the team's published hub tools (its newest version) as the draft, "
     "to change it. Replaces the current draft.",
     "parameters": {"type": "object", "properties": {"tool_id": {"type": "string", "description": "<team>.<name>"}},
                    "required": ["tool_id"]}},
    {"name": "read_files", "description": "Read one draft file in full (the files sent with each turn are cut when long). "
     "file: manifest | script | check | readme | data; offset: the character to start at.",
     "parameters": {"type": "object", "properties": {"file": {"type": "string"}, "offset": {"type": "integer"}},
                    "required": ["file"]}},
    {"name": "write_files", "description": "Save the hub tool's files as the current draft and validate them. "
     "Send every file you change; omitted files keep their current content. Returns ok, or the field and rule to fix.",
     "parameters": {"type": "object", "properties": {
         "manifest": {"type": "object", "description": "recipe.json"},
         "script": {"type": "string", "description": "run.js (script tools only)"},
         "check": {"type": "object", "description": "check.json: sample inputs + the output fields the check must find"},
         "readme": {"type": "string", "description": "README.md for humans"}}}},
    {"name": "test_run", "description": "Ask to run the current draft for real with these inputs, on the team's balance. "
     "The maker gets a button unless they allowed test runs without asking. Nothing is published.",
     "parameters": {"type": "object", "properties": {
         "inputs": {"type": "object"},
         "est_cost_usd": {"type": "number", "description": "your estimate of the run's cost from the steps' prices"}},
         "required": ["inputs", "est_cost_usd"]}},
    {"name": "publish", "description": "Ask to publish the current draft (a new version if it exists). The maker gets a button. "
     "Publishing runs check.json once for real; live on pass.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "app_on", "description": "Ask to give the published tool a web page at /apps/<team>/<name>. The maker gets a button.",
     "parameters": {"type": "object", "properties": {"name": {"type": "string", "description": "optional page name"}}}},
    {"name": "app_password", "description": "Ask the maker to set (or remove) the app's password. They type it into a field; "
     "you never see it.", "parameters": {"type": "object", "properties": {}}},
]

# The tools that only ask: the maker's press does the work (`act`).
ASKS = {"test_run": "test", "publish": "publish", "app_on": "app", "app_password": "password"}


def _trim(value: Any, limit: int = RESULT_MAX) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + f"… [{len(text) - limit} more characters]"


def _detail(r: httpx.Response) -> Any:
    try:
        body = r.json()
    except ValueError:
        return r.text[:600]
    return body.get("detail", body) if isinstance(body, dict) else body


class Toolbox:
    """The agent's tools, each an in-process request as the maker."""

    def __init__(self, db: AsyncSession, session: VibeSession, org: Org, app: Any, headers: dict[str, str],
                 *, user_id: int = 0, emit: Emit = _quiet):
        self.db, self.session, self.org, self.app = db, session, org, app
        self.headers = maker_headers(headers)
        self.user_id, self.emit = user_id, emit
        self.asked: dict | None = None      # the action this message asked the maker for
        self.versions: tuple[int | None, int | None] = (None, None)   # draft versions around the last write

    def _client(self, timeout: float = 60.0) -> httpx.AsyncClient:
        return _client(self.app, self.headers, timeout)

    async def run(self, name: str, args: dict) -> tuple[dict, str, bool]:
        """(the result the model reads, one line for the page, ok)."""
        fn = getattr(self, f"t_{name}", None)
        if fn is None:
            return {"error": f"no tool {name!r}"}, f"unknown tool {name}", False
        try:
            return await fn(**{k: v for k, v in args.items() if not k.startswith("_")})
        except TypeError as exc:
            return {"error": f"bad arguments: {exc}"}, f"{name}: bad arguments", False

    async def t_catalog_search(self, query: str = "") -> tuple[dict, str, bool]:
        await self.db.commit()
        verdict, rows = await self._find(query)
        if verdict is None:
            verdict, rows = "keyword", await self._keyword(query)
        else:
            await spend(self.db, self.user_id, FIND_COST_MICRO)
            await self.db.commit()
        access = await asyncio.gather(*(self._access(r["id"]) for r in rows))
        for r, a in zip(rows, access):
            if a is not None:
                r.update(a)
        note = {"strong": "good fits", "closest": "no strong fit; closest matches", "none": "nothing fits",
                "keyword": "keyword matches, unjudged", "name": "what this name offers"}.get(verdict, verdict)
        return ({"verdict": verdict, "results": rows},
                f"searched the catalog for “{query}”: {len(rows)} found, {note}", True)

    async def _find(self, query: str) -> tuple[str | None, list[dict]]:
        """The find engine (job recall + one judge request), in-process. (None, []) when it is not
        configured, over its hourly limits, or the judge abstained: the caller falls back to keywords."""
        q = find.clean_query(query)
        if not q or not find.configured() or not await find.admit(f"vibe:{self.user_id}"):
            return None, []
        judged: dict | None = None
        try:
            async for event in find.stream(q, lambda p: p):
                if event.get("event") == "judged":
                    judged = event
        except Exception:  # noqa: BLE001 - find abstains; keywords answer
            return None, []
        if not judged or judged.get("verdict") in ("keyword", None):
            return None, []
        rows = [{"id": r.get("id"), "does": r.get("name"), "provider": r.get("provider"),
                 "cost_usd": (r.get("cost") or {}).get("usd"), "per": (r.get("cost") or {}).get("type"),
                 "fit": r.get("p")} for r in (judged.get("rows") or [])[:SEARCH_ROWS] if r.get("id")]
        return judged["verdict"], rows

    async def _keyword(self, query: str) -> list[dict]:
        async with self._client() as c:
            r = await c.get("/catalog/search", params={"q": query, "limit": SEARCH_ROWS})
        rows = (r.json().get("results") or []) if r.status_code == 200 else []
        return [{"id": e.get("id"), "does": e.get("summary"), "cost_usd": (e.get("cost") or {}).get("usd"),
                 "price_line": e.get("price_line"), "ok_rate": (e.get("observed") or {}).get("ok_rate")}
                for e in rows if e.get("id")]

    async def t_catalog_get(self, id: str = "") -> tuple[dict, str, bool]:
        await self.db.commit()
        async with self._client() as c:
            r = await c.get(f"/catalog/endpoints/{id}")
        if r.status_code != 200:
            return {"error": f"http_{r.status_code}", "detail": _detail(r)}, f"read {id}: not found", False
        ep = r.json().get("endpoint") or {}
        keep = ("id", "summary", "method", "path", "inputs", "params", "body", "output", "cost", "observed",
                "call_template", "docs", "notes", "kind", "price_line", "example")
        out = {k: ep[k] for k in keep if k in ep}
        access = await self._access(id) if ep.get("kind") != "hub" else None
        if access is not None:
            out["access"] = access
        callable_ = access is None or access["callable"]
        return out, f"read {id}" + ("" if callable_ else ": your team cannot call it yet"), True

    async def _access(self, endpoint_id: str) -> dict | None:
        return await access_of(self.app, self.headers, endpoint_id)

    async def t_my_tools(self) -> tuple[dict, str, bool]:
        await self.db.commit()
        async with self._client() as c:
            r = await c.get("/tools")
            hub = await c.get("/hub/tools/mine")
        rows = r.json() if r.status_code == 200 and isinstance(r.json(), list) else []
        out = [{"name": t.get("name"), "base_url": t.get("base_url"), "description": t.get("description")} for t in rows]
        published: dict[str, dict] = {}
        for t in (hub.json() if hub.status_code == 200 and isinstance(hub.json(), list) else []):
            published.setdefault(t.get("tool_id"), {"tool_id": t.get("tool_id"), "version": t.get("version"),
                                                    "status": t.get("status"), "summary": t.get("summary")})
        drafts = [{"conversation_id": r.id, "title": r.title, "name": (r.draft.get("manifest") or {}).get("name"),
                   "updated_at": r.updated_at.isoformat()}
                  for r in await drafts_of(self.db, user_id=self.user_id, org_id=self.org.id, exclude=self.session.id)]
        await self.db.commit()
        return ({"tools": out, "hub_tools": list(published.values()), "drafts": drafts},
                f"found {len(published)} published tool{'' if len(published) == 1 else 's'}, "
                f"{len(drafts)} draft{'' if len(drafts) == 1 else 's'}, "
                f"{len(out)} own API tool{'' if len(out) == 1 else 's'}", True)

    async def t_load_draft(self, conversation_id: Any = 0) -> tuple[dict, str, bool]:
        source = await of_user(self.db, user_id=self.user_id, session_id=int(conversation_id or 0))
        if source is None or source.org_id != self.org.id or source.id == self.session.id or not clean_draft(source.draft):
            await self.db.commit()
            return {"error": f"no draft in conversation {conversation_id}"}, "draft: not found", False
        await copy_draft(self.db, self.session, source)
        await self.db.commit()
        await self.emit({"type": "draft", "draft": clean_draft(self.session.draft), "problem": None})
        return ({"ok": True, "copied_from": source.title, "files": _brief(self.session.draft)},
                f"copied the draft “{source.title}”", True)

    async def t_load_my_tool(self, tool_id: str = "") -> tuple[dict, str, bool]:
        row = await load_tool(self.db, self.session, self.org, tool_id)
        await self.db.commit()
        if row is None:
            return {"error": f"no hub tool {tool_id!r} in team {self.org.slug}"}, f"load {tool_id}: not found", False
        await self.emit({"type": "draft", "draft": clean_draft(self.session.draft), "problem": None})
        return ({"ok": True, "tool_id": row.tool_id, "version": row.version, "status": row.status,
                 "files": _brief(self.session.draft)}, f"loaded {row.tool_id} v{row.version}", True)

    async def t_read_files(self, file: str = "", offset: Any = 0) -> tuple[dict, str, bool]:
        d = clean_draft(self.session.draft)
        if file not in d:
            return {"error": f"no file {file!r} in the draft", "files": list(d)}, f"read {file}: not in the draft", False
        text = d[file] if isinstance(d[file], str) else json.dumps(d[file], indent=2, ensure_ascii=False)
        start = max(0, int(offset)) if isinstance(offset, int) else 0
        part = text[start:start + READ_MAX]
        return ({"file": file, "offset": start, "text": part, "length": len(text),
                 "more": start + len(part) < len(text)}, f"read the draft's {file}", True)

    async def t_write_files(self, manifest: Any = None, script: Any = None, check: Any = None,
                            readme: Any = None) -> tuple[dict, str, bool]:
        before = await _latest_n(self.db, self.session.id)
        v = await set_draft(self.db, self.session, {"manifest": manifest, "script": script, "check": check, "readme": readme},
                            author="agent")
        problem = await validate(self.db, self.session, self.org)
        await self.db.commit()
        await self.emit({"type": "draft", "draft": clean_draft(self.session.draft), "problem": problem})
        self.versions = (before, v.n if v else None)
        if problem:
            return {"ok": False, **problem}, f"saved the files; fix {problem['field']}: {problem['rule']}", False
        return {"ok": True}, "saved the files: valid", True

    async def _ask(self, kind: str, **extra: Any) -> tuple[dict, str, bool]:
        pending = {"id": secrets.token_hex(6), "kind": kind, **extra}
        await set_pending(self.db, self.session, pending)
        await self.db.commit()
        self.asked = pending
        await self.emit({"type": "pending", "pending": pending})
        return ({"waiting": "The maker now sees a button for this. Stop here; their choice comes back as a note."},
                {"test": "asked to run a test", "publish": "asked to publish", "app": "asked to turn on the app",
                 "password": "asked for the app's password"}[kind], True)

    async def t_test_run(self, inputs: Any = None, est_cost_usd: Any = None) -> tuple[dict, str, bool]:
        inputs = inputs if isinstance(inputs, dict) else {}
        est = float(est_cost_usd) if isinstance(est_cost_usd, (int, float)) and est_cost_usd >= 0 else None
        if not self.session.auto_test:
            return await self._ask("test", inputs=inputs, est_cost_usd=est)
        await self.db.commit()
        result = await run_test(self.app, self.headers, clean_draft(self.session.draft), inputs)
        if result["ok"]:
            return ({"ok": True, "output": result["output"], "usage": result["usage"], "log": result.get("log"),
                     "trace": result.get("trace")}, f"test run: ok, ${result['cost_micro'] / 1e6:.4f}", True)
        return {"ok": False, "status": result["status"], "detail": result["detail"]}, f"test run failed ({result['status']})", False

    async def t_publish(self) -> tuple[dict, str, bool]:
        return await self._ask("publish")

    async def t_app_on(self, name: str | None = None) -> tuple[dict, str, bool]:
        if not self.session.tool_id:
            return {"error": "publish the tool first"}, "app: publish first", False
        return await self._ask("app", name=name if isinstance(name, str) and name else None)

    async def t_app_password(self) -> tuple[dict, str, bool]:
        if not self.session.tool_id:
            return {"error": "publish the tool and turn on its app first"}, "password: publish first", False
        return await self._ask("password")


def _client(app: Any, headers: dict[str, str], timeout: float = 60.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://treg.internal",
                             headers=headers, timeout=timeout)


def maker_headers(headers: dict[str, str]) -> dict[str, str]:
    """The maker's own credentials for an in-process request: the session cookie and the team."""
    h = {k: v for k, v in headers.items() if k.lower() in ("cookie", "x-treg-org")}
    h["X-Treg-Client"] = "vibe-it"
    return h


async def access_of(app: Any, headers: dict[str, str], endpoint_id: str) -> dict | None:
    """Whether THIS team can call a catalog tool now, and with whose key (the catalog's own access
    answer): a step the team cannot call fails every test run with a 404 before any provider."""
    async with _client(app, headers) as c:
        r = await c.get(f"/catalog/endpoints/{endpoint_id}/access")
    if r.status_code != 200:
        return None
    a = r.json()
    tier = a.get("tier") or "none"
    return {"callable": tier != "none", "tier": tier,
            **({"why_not": a.get("detail"), "fix": a.get("connect_command")} if tier == "none" else {})}


SECRET_SHOWN = "••••••"


def masked_inputs(draft: dict, inputs: dict) -> dict:
    """Inputs as stored in the conversation and read by the model: a `secret` input (a key, a
    token) is never kept or sent on, only that it was given."""
    specs = ((draft.get("manifest") or {}).get("inputs") or {}) if isinstance(draft, dict) else {}
    return {k: (SECRET_SHOWN if isinstance(specs.get(k), dict) and specs[k].get("secret") and v not in (None, "")
                else v) for k, v in inputs.items()}


def _brief(d: Any) -> dict:
    """The files as the model reads them: data.csv as its header and size only."""
    out = clean_draft(d)
    if "data" in out:
        text = out["data"]
        out["data"] = {"header": text.split("\n", 1)[0][:500], "rows": max(0, text.count("\n")), "bytes": len(text)}
    return out


async def _latest_n(db: AsyncSession, session_id: int) -> int | None:
    from . import latest_n
    return await latest_n(db, session_id)


async def load_tool(db: AsyncSession, session: VibeSession, org: Org, tool_id: str) -> HubTool | None:
    """The team's own hub tool, newest version, as the draft (data.csv included). None when the
    team has no such tool. Does not commit."""
    base, _ = hub_app.split_id(str(tool_id or ""))
    row = (await db.execute(select(HubTool).where(HubTool.tool_id == base, HubTool.org_id == org.id)
                            .order_by(HubTool.version.desc()).limit(1))).scalars().first()
    if row is None:
        return None
    manifest, check = as_files(row.manifest, row.check)
    files = {"manifest": manifest, "check": check, "readme": row.readme or ""}
    if row.script:
        files["script"] = row.script
    if row.data:
        files["data"] = row.data
    await set_draft(db, session, files, author="load", note=f"loaded {row.tool_id} v{row.version}", replace=True)
    await set_tool(db, session, row.tool_id)
    return row


async def validate(db: AsyncSession, session: VibeSession, org: Org) -> dict | None:
    """None when the draft would publish, else {field, rule}: the hub's own validator against the
    team's world (catalog ids, the team's tools). Nothing stored."""
    d = session.draft or {}
    if not isinstance(d.get("manifest"), dict):
        return {"field": "manifest", "rule": "required: recipe.json"}
    if not isinstance(d.get("check"), dict):
        return {"field": "check", "rule": "required: check.json"}
    if not (d.get("readme") or "").strip():
        return {"field": "readme", "rule": "required: README.md"}
    try:
        await hub_app.transient(db, org=org, maker_email="", manifest=d["manifest"], script=d.get("script"),
                                check=d["check"], readme=d["readme"], data=d.get("data"))
    except ManifestError as exc:
        return {"field": exc.field, "rule": exc.rule}
    return None


def _files(d: dict) -> dict:
    out = {"manifest": d.get("manifest") or {}, "script": d.get("script"), "check": d.get("check") or {},
           "readme": d.get("readme") or ""}
    if d.get("data"):
        out["data"] = d["data"]
    return out


async def run_test(app: Any, headers: dict, draft: dict, inputs: dict) -> dict:
    """A real run of the draft (`POST /hub/run`): {ok, status, output, usage, cost_micro, log, trace}
    or {ok: False, status, detail}."""
    async with _client(app, headers, 200.0) as c:
        r = await c.post("/hub/run", json={**_files(draft), "inputs": inputs})
    if r.status_code == 200:
        try:
            body = r.json()
        except ValueError:
            body = {}
        usage = body.get("usage") or {} if isinstance(body, dict) else {}
        return {"ok": True, "status": 200, "output": body.get("output") if isinstance(body, dict) else body,
                "usage": usage, "cost_micro": int(usage.get("cost_micro") or r.headers.get("x-treg-cost-micro") or 0),
                "log": body.get("log") if isinstance(body, dict) else None,
                "trace": body.get("trace") if isinstance(body, dict) else None}
    return {"ok": False, "status": r.status_code, "detail": _detail(r)}


async def publish(app: Any, headers: dict, draft: dict, team_slug: str) -> tuple[int, Any]:
    name = (draft.get("manifest") or {}).get("name")
    async with _client(app, headers, 240.0) as c:
        mine = await c.get("/hub/tools/mine")
        exists = mine.status_code == 200 and any(t.get("tool_id") == f"{team_slug}.{name}" for t in mine.json())
        r = (await c.put(f"/hub/tools/{team_slug}.{name}", json=_files(draft)) if exists
             else await c.post("/hub/tools", json=_files(draft)))
    try:
        return r.status_code, r.json() if r.status_code in (200, 201) else _detail(r)
    except ValueError:
        return r.status_code, r.text[:600]


# ---- the maker's actions: what a button press does, each recorded as an event ----

async def act(db: AsyncSession, session: VibeSession, org: Org, *, kind: str, app: Any, headers: dict[str, str],
              inputs: dict | None = None, name: str | None = None, password: str | None = None) -> dict:
    """Run one action the maker pressed (`test`, `publish`, `app`, `password`, `skip`) and store it
    as an event message. The pending ask, if any, is cleared. Returns the event's view. Commits;
    no connection is held while the action runs."""
    h = maker_headers(headers)
    pending = session.pending or {}
    await set_pending(db, session, None)
    draft = clean_draft(session.draft)
    tool_id = session.tool_id
    await db.commit()
    if kind == "skip":
        what = {"test": "the test run", "publish": "publishing", "app": "the app", "password": "the password"}
        thing = what.get(pending.get("kind"))
        ev = {"kind": "skip", "ok": True, "summary": f"Not now: {thing}" if thing else "Not now"}
    elif kind == "test":
        r = await run_test(app, h, draft, inputs or {})
        ev = {"kind": "test", "ok": r["ok"], "inputs": masked_inputs(draft, inputs or {}), "status": r["status"]}
        if r["ok"]:
            ev.update(summary=f"Test run: ok, ${r['cost_micro'] / 1e6:.4f}", cost_micro=r["cost_micro"],
                      detail={"output": r["output"], "log": r.get("log")})
        else:
            ev.update(summary=f"Test run failed ({r['status']})", detail=r["detail"])
    elif kind == "publish":
        status, body = await publish(app, h, draft, org.slug)
        ok = status in (200, 201) and isinstance(body, dict)
        ev = {"kind": "publish", "status": status}
        if ok:
            tool_id = body.get("tool_id")
            verdict = body.get("status")
            ev.update(ok=verdict in ("live", "review"), tool_id=tool_id, version=body.get("version"),
                      url=body.get("page"), summary=f"Published {tool_id} v{body.get('version')}: {verdict}",
                      detail={"check": body.get("check")} if body.get("check") else None)
            await set_tool(db, session, tool_id)
            if body.get("version"):
                await mark_published(db, session, int(body["version"]))
        else:
            ev.update(ok=False, summary=f"Publish refused ({status})", detail=body)
    elif kind in ("app", "password"):
        if not tool_id:
            ev = {"kind": kind, "ok": False, "summary": "Publish the tool first"}
        else:
            async with _client(app, h) as c:
                if kind == "app":
                    r = await c.put(f"/hub/tools/{tool_id}/app", json={"name": name} if name else {})
                else:
                    r = await c.put(f"/hub/tools/{tool_id}/app/password", json={"password": password or None})
            ok = r.status_code == 200
            body = r.json() if ok else _detail(r)
            if kind == "app":
                ev = {"kind": "app", "ok": ok, "tool_id": tool_id, "url": body.get("url") if ok else None,
                      "summary": f"App on at {body.get('url')}" if ok else f"App refused ({r.status_code})",
                      "detail": None if ok else body}
            else:
                # the password itself is never stored or shown, only that it changed
                ev = {"kind": "password", "ok": ok, "tool_id": tool_id,
                      "summary": ("App password set" if password else "App password removed") if ok
                      else f"Password refused ({r.status_code})", "detail": None if ok else body}
    else:
        raise ValueError(kind)
    msg = await add_message(db, session, "event", ev)
    await db.commit()
    return view_message(msg)


# ---- the conversation as the model reads it ----

def _history(session: VibeSession, rows: list) -> list[dict]:
    """The stored turns as the model's messages. The maker's actions are notes on the user side."""
    out: list[dict] = []
    last_user = max((i for i, m in enumerate(rows) if m.role == "user"), default=-1)
    if session.summary:
        out.append({"role": "system", "content": "Summary of the earlier conversation (its messages were trimmed):\n"
                    + session.summary})
    for i, m in enumerate(rows):
        c = m.content or {}
        if m.role == "user":
            text = c.get("text", "")
            for a in c.get("attachments") or []:
                body = a.get("text", "")
                if i < last_user and len(body) > OLD_RESULT_MAX:   # read in full when sent, then a glimpse
                    body = body[:OLD_RESULT_MAX] + f"… [cut: {len(body):,} characters, attached to an earlier message]"
                text += f"\n\nAttached `{a.get('name')}`:\n```\n{body}\n```"
            out.append({"role": "user", "content": text})
        elif m.role == "assistant":
            msg: dict = {"role": "assistant", "content": c.get("text") or None}
            if c.get("tool_calls"):
                msg["tool_calls"] = [{"id": t["id"], "type": "function",
                                      "function": {"name": t["name"], "arguments": json.dumps(_slim_args(t))}}
                                     for t in c["tool_calls"]]
            if msg["content"] is None and not c.get("tool_calls"):
                continue
            out.append(msg)
        elif m.role == "tool":
            result = c.get("result", "")
            if i < last_user and len(result) > OLD_RESULT_MAX:
                result = result[:OLD_RESULT_MAX] + "… [cut: from an earlier message]"
            out.append({"role": "tool", "tool_call_id": c.get("tool_call_id", ""), "content": result})
        elif m.role == "event":
            note = f"[The maker pressed a button, not a message] {c.get('summary', '')}"
            if c.get("inputs"):
                note += f"\ninputs: {_trim(c['inputs'], 1500)}"
            if c.get("detail") is not None:
                note += f"\n{_trim(c['detail'], 4000)}"
            out.append({"role": "user", "content": note})
    return _paired(out)


def _slim_args(call: dict) -> dict:
    """A past write_files call without the files it wrote: the current files come with every turn,
    and resending each old copy made long conversations cost many times more."""
    args = call.get("arguments") or {}
    if call.get("name") == "write_files":
        return {k: f"[{len(v) if isinstance(v, str) else len(json.dumps(v))} characters, written]" for k, v in args.items()}
    return args


def _paired(out: list[dict]) -> list[dict]:
    """Every assistant tool call answered, in order: a stopped turn may leave calls without results,
    and the model APIs refuse such a history."""
    fixed: list[dict] = []
    i = 0
    while i < len(out):
        m = out[i]
        fixed.append(m)
        i += 1
        if m["role"] == "assistant" and m.get("tool_calls"):
            answered = set()
            while i < len(out) and out[i]["role"] == "tool":
                answered.add(out[i]["tool_call_id"])
                fixed.append(out[i])
                i += 1
            for t in m["tool_calls"]:
                if t["id"] not in answered:
                    fixed.append({"role": "tool", "tool_call_id": t["id"], "content": "(stopped by the maker before it ran)"})
    return fixed


def _draft_note(session: VibeSession) -> str:
    d = _brief(session.draft)
    if not d:
        return "There is no draft yet."
    text = json.dumps(d, ensure_ascii=False, default=str)
    note = "The current draft files (the maker may have edited them):\n" + text[:DRAFT_NOTE_MAX]
    if len(text) > DRAFT_NOTE_MAX:
        note += "\n[The files were cut here. Use read_files to read one in full before changing it.]"
    if session.tool_id:
        note += f"\nThis conversation's published tool: {session.tool_id}."
    return note


def turn_cost(turn: llm.Turn, history: list[dict], stopped: bool) -> int:
    """What one model turn cost treg, in micro-USD: the gateway's own figure when it gives one;
    otherwise, for a turn the model worked on (it answered, or was stopped mid-answer), its tokens
    at the deliberately high estimate, the input counted from the history when the gateway sent no
    usage (the input, files included, is most of a turn's cost). A request that never reached a
    model (no answer, no usage) cost nothing."""
    if turn.cost_usd:
        return int(round(turn.cost_usd * 1_000_000))
    if not (turn.text or turn.tool_calls or stopped or turn.input_tokens or turn.output_tokens):
        return 0
    tokens_in = turn.input_tokens or len(json.dumps(history, ensure_ascii=False, default=str)) // 3
    tokens_out = turn.output_tokens or len(turn.text) // 3
    return tokens_in * EST_IN_PER_TOKEN_MICRO + tokens_out * EST_OUT_PER_TOKEN_MICRO


async def _stop_check(session_id: int) -> bool:
    async with database.session_maker() as s:
        wanted = await stop_wanted(s, session_id)
        await s.commit()
    return wanted


async def _turn(history: list[dict], *, model: str, session_id: int, emit: Emit) -> tuple[llm.Turn, bool]:
    """One streamed model turn; (the turn, stopped). The stop flag is read while it streams."""
    s = get_settings()
    pieces: list[str] = []

    async def on_text(t: str) -> None:
        pieces.append(t)
        await emit({"type": "delta", "text": t})

    task = asyncio.create_task(llm.chat_stream(history, TOOLS, api_key=s.ai_gateway_api_key, model=model, on_text=on_text))
    while True:
        done, _ = await asyncio.wait({task}, timeout=STOP_POLL_S)
        if done:
            return task.result(), False
        if await _stop_check(session_id):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            text = "".join(pieces)
            return llm.Turn(text=text, tool_calls=[], ms=0, output_tokens=len(text) // 3), True


async def send(db: AsyncSession, session: VibeSession, org: Org, *, user_id: int, text: str | None, app: Any,
               headers: dict[str, str], attachments: list[dict] | None = None, emit: Emit = _quiet,
               run_token: str | None = None) -> None:
    """Add the maker's message (none on a regenerate), then let the agent work until it answers in
    text, asks the maker for something, is stopped, or hits a limit. Every turn and tool step is
    stored and emitted as it happens. Commits as it goes."""
    s = get_settings()

    async def store(role: str, content: dict, cost: int = 0) -> None:
        msg = await add_message(db, session, role, content, cost)
        await db.commit()
        await emit({"type": "message", "message": view_message(msg)})

    if text is not None:
        content: dict = {"text": text}
        if attachments:
            content["attachments"] = attachments
        await store("user", content)
    if session.pending:
        await set_pending(db, session, None)        # a new message answers the open ask in words
        await db.commit()
        await emit({"type": "pending", "pending": None})
    box = Toolbox(db, session, org, app, headers, user_id=user_id, emit=emit)
    system = SYSTEM.format(team=org.slug, rules=_hub_rules())
    calls = 0
    spent_here = 0
    async def still_ours() -> bool:
        """Refresh this run's busy mark; False when another run took the conversation over."""
        if run_token is None:
            return True
        ok = await touch_run(db, session.id, run_token)
        await db.commit()
        return ok

    for _ in range(MAX_TURNS):
        if not await still_ours():
            return
        if spent_here >= MESSAGE_CAP_MICRO:
            await store("assistant", {"text": f"This message has used ${spent_here / 1e6:.2f} of model time, the most one "
                                      "message may. Your files are saved. Tell me to continue and I'll pick up from here."})
            return
        if await stop_wanted(db, session.id):
            await store("assistant", {"text": "Stopped.", "stopped": True})
            return
        left = await left_micro(db, user_id)
        if left <= 0:
            await store("assistant", {"text": "I've used up the model budget treg gives each person for vibe-it. Your "
                                      "draft is saved: you can still edit it, test it and publish it from the panel, "
                                      "or finish with `treg hub` in a terminal."})
            return
        history = [{"role": "system", "content": system}, {"role": "system", "content": _draft_note(session)},
                   *_history(session, await messages(db, session.id))]
        await db.commit()       # no connection held while the model thinks
        turn, stopped = await _turn(history, model=s.vibe_model, session_id=session.id, emit=emit)
        cost = turn_cost(turn, history, stopped)
        if turn.error and not turn.text and not turn.tool_calls and s.vibe_fallback_model:
            await emit({"type": "reset"})
            turn, stopped = await _turn(history, model=s.vibe_fallback_model, session_id=session.id, emit=emit)
            cost += turn_cost(turn, history, stopped)       # the first attempt is paid for too
        await spend(db, user_id, cost)
        spent_here += cost
        await emit({"type": "budget", "left_micro": max(0, left - cost)})
        if stopped:
            await store("assistant", {"text": turn.text or "Stopped.", "stopped": True}, cost)
            return
        if turn.error and not turn.text and not turn.tool_calls:
            network = turn.error in ("ConnectError", "ConnectTimeout", "ReadTimeout", "timeout", "RemoteProtocolError")
            await store("assistant", {"text": ("I couldn't reach the model: the network dropped or is slow. "
                                               if network else f"The model did not answer just now ({turn.error}). ")
                                      + "Send your message again in a moment.", "error": turn.error}, cost)
            return
        calls_now = turn.tool_calls[: max(0, MAX_TOOL_CALLS - calls)]
        await store("assistant", {"text": turn.text, "tool_calls": calls_now}, cost)
        if not calls_now:
            return
        for call in calls_now:
            if not await still_ours():
                return
            if await stop_wanted(db, session.id):
                await store("assistant", {"text": "Stopped.", "stopped": True})
                return
            calls += 1
            args = call.get("arguments") or {}
            await emit({"type": "step", "name": call["name"], "args": args})
            box.versions = (None, None)
            result, line, ok = await box.run(call["name"], args)
            content = {"tool_call_id": call["id"], "name": call["name"], "arguments": args,
                       "result": _trim(result), "summary": line, "ok": ok}
            before, after = box.versions
            if after:
                content.update(version=after, prev_version=before)
            await store("tool", content)
            await emit({"type": "budget", "left_micro": await left_micro(db, user_id)})
        if box.asked is not None:
            return                      # the maker decides; their press comes back as an event
        if calls >= MAX_TOOL_CALLS:
            await store("assistant", {"text": "I've taken as many steps as I can for one message. "
                                      "Tell me to continue and I'll pick up from here."})
            return
    await store("assistant", {"text": "I'll stop here for this message. Tell me to continue when you're ready."})
