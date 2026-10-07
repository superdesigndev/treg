"""Vibe-it (docs/context/architecture/vibe-it.md): conversations, the streamed agent loop with a fake
model, the budget, the draft and its versions, the maker's buttons (test run, publish, app, password)
through the hub's own routes, loading a published tool, the find engine, stop and regenerate,
trimming, deletion."""

from __future__ import annotations

from datetime import timedelta

import json

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update

from treg.config import get_settings
from treg.infra import llm
from treg.infra.db import session_maker
from treg.models import Org, VibeBudget, VibeDraft, VibeMessage, VibeSession
from tests.test_hub import hub_on  # noqa: F401


SCRIPT = "export default async function run(ctx) { return {greeting: 'hi ' + ctx.inputs.name}; }"
MANIFEST = {"name": "greeter", "summary": "Says hi to a name.",
            "inputs": {"name": {"type": "string", "example": "Ada"}},
            "uses": [], "script": "run.js", "output": {"fields": ["greeting"]}}
CHECK = {"inputs": {"name": "Ada"}, "fields": ["greeting"]}


@pytest.fixture
def vibe_on(hub_on, monkeypatch):  # noqa: F811
    for k, v in {"TREG_VIBE_ENABLED": "1", "TREG_HUB_APPS_ENABLED": "1", "TREG_HUB_TEAMS": "", "TREG_HUB_USERS": "",
                 "TREG_AI_GATEWAY_API_KEY": "test-key", "TREG_VIBE_BUDGET_USD": "1"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _browser(clients: AsyncClient) -> dict:
    """Sign the suite's user in by email code: a session cookie, no token, the team in X-Treg-Org."""
    email = "tim@superdesign.dev"
    code = (await clients.post("/auth/email/start", json={"email": email})).json()["dev_code"]
    assert (await clients.post("/auth/email/verify", json={"email": email, "code": code})).status_code == 200
    slug = (await clients.get("/orgs")).json()[0]["slug"]
    return {"X-Treg-Token": "", "X-Treg-Org": slug}


def _fake_model(monkeypatch, turns: list[llm.Turn]) -> list[list[dict]]:
    seen: list[list[dict]] = []

    async def chat_stream(messages, tools, *, on_text=None, **kw):
        seen.append(messages)
        turn = turns.pop(0) if turns else llm.Turn(text="done", tool_calls=[], ms=1)
        if on_text and turn.text:
            await on_text(turn.text)
        return turn
    monkeypatch.setattr(llm, "chat_stream", chat_stream)
    return seen


async def _say(clients: AsyncClient, sid: int, text: str, h: dict, path: str = "messages", **extra) -> tuple[dict, list[dict]]:
    """Send a message, read the stream to its end; (the conversation as stored, the events)."""
    r = await clients.post(f"/vibe/sessions/{sid}/{path}", json={"text": text, **extra} if path == "messages" else None,
                           headers=h)
    assert r.status_code == 200, r.text
    events = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    assert events[-1]["type"] == "done"
    return (await clients.get(f"/vibe/sessions/{sid}", headers=h)).json(), events


def _call(name: str, **args) -> dict:
    return {"id": f"c-{name}", "name": name, "arguments": args}


async def test_off_or_with_a_token_is_refused(clients: AsyncClient, hub_on):  # noqa: F811
    assert (await clients.get("/vibe/state")).status_code == 404
    assert (await clients.get("/vibe-it")).status_code == 404
    assert (await clients.get("/meta")).json()["vibe"] is False


async def test_an_agent_token_cannot_use_it(clients: AsyncClient, vibe_on):
    r = await clients.get("/vibe/state")                       # the suite's token, no browser session
    assert r.status_code == 403 and r.json()["detail"]["error"] == "vibe_browser_only"


async def test_the_agent_writes_valid_files(clients: AsyncClient, vibe_on, monkeypatch):
    h = await _browser(clients)
    seen = _fake_model(monkeypatch, [
        llm.Turn(text="Let me write it.", tool_calls=[_call("write_files", manifest=MANIFEST, script=SCRIPT, check=CHECK,
                                                             readme="Says hi.")], ms=1, cost_usd=0.01),
        llm.Turn(text="Written and valid. Want a test run?", tool_calls=[], ms=1, cost_usd=0.01),
    ])
    s = (await clients.post("/vibe/sessions", json={}, headers=h)).json()
    body, events = await _say(clients, s["id"], "a greeter", h)
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "tool", "assistant"]
    kinds = [e["type"] for e in events]
    assert "delta" in kinds and "step" in kinds and "draft" in kinds
    assert body["messages"][2]["args"]["manifest"]["name"] == "greeter" and body["messages"][2]["version"] == 1
    assert body["busy"] is False
    assert body["messages"][2]["ok"] is True and "valid" in body["messages"][2]["summary"]
    assert body["draft"]["manifest"]["name"] == "greeter" and body["title"] == "greeter"
    # the hub's own rules reach the model
    assert "recipe.json rules" in seen[0][0]["content"] and "never paste a credential" in seen[0][0]["content"]
    state = (await clients.get("/vibe/state", headers=h)).json()
    assert state["left_micro"] == 1_000_000 - 20_000


async def test_a_refusal_names_the_field(clients: AsyncClient, vibe_on, monkeypatch):
    h = await _browser(clients)
    _fake_model(monkeypatch, [llm.Turn(text="", tool_calls=[_call("write_files", manifest={**MANIFEST, "uses": ["nope.tool"]},
                                                                  script=SCRIPT, check=CHECK, readme="x")], ms=1)])
    s = (await clients.post("/vibe/sessions", json={}, headers=h)).json()
    body, _ = await _say(clients, s["id"], "go", h)
    tool = next(m for m in body["messages"] if m["role"] == "tool")
    assert tool["ok"] is False and "uses[0]" in tool["summary"]


async def test_the_budget_stops_the_agent(clients: AsyncClient, vibe_on, monkeypatch):
    h = await _browser(clients)
    _fake_model(monkeypatch, [])
    s = (await clients.post("/vibe/sessions", json={}, headers=h)).json()
    async with session_maker() as db:
        user_id = (await db.get(VibeSession, s["id"])).user_id
        db.add(VibeBudget(user_id=user_id, spent_micro=1_000_000))
        await db.commit()
    body, _ = await _say(clients, s["id"], "hi", h)
    assert "model budget" in body["messages"][-1]["text"]


async def test_tool_calls_are_capped(clients: AsyncClient, vibe_on, monkeypatch):
    from treg.application.vibe import agent
    monkeypatch.setattr(agent, "MAX_TOOL_CALLS", 2)
    h = await _browser(clients)
    _fake_model(monkeypatch, [llm.Turn(text="", tool_calls=[_call("my_tools")] * 5, ms=1)] * 5)
    s = (await clients.post("/vibe/sessions", json={}, headers=h)).json()
    body, _ = await _say(clients, s["id"], "go", h)
    assert sum(1 for m in body["messages"] if m["role"] == "tool") == 2
    assert "as many steps" in body["messages"][-1]["text"]


async def test_edit_test_and_publish_from_the_panel(clients: AsyncClient, vibe_on):
    h = await _browser(clients)
    s = (await clients.post("/vibe/sessions", json={}, headers=h)).json()
    r = (await clients.put(f"/vibe/sessions/{s['id']}/draft", json={"manifest": MANIFEST, "script": SCRIPT,
                                                                    "check": CHECK, "readme": "Says hi."}, headers=h)).json()
    assert r["problem"] is None
    t = (await clients.post(f"/vibe/sessions/{s['id']}/actions", json={"kind": "test", "inputs": {"name": "Bo"}},
                            headers=h)).json()["event"]
    assert t["ok"] is True and t["detail"]["output"] == {"greeting": "hi Bo"}
    p = (await clients.post(f"/vibe/sessions/{s['id']}/actions", json={"kind": "publish"}, headers=h)).json()
    assert p["event"]["ok"] is True and p["event"]["status"] == 201 and p["event"]["version"] == 1, p
    assert (await clients.get(f"/vibe/sessions/{s['id']}", headers=h)).json()["tool_id"] == p["tool_id"]
    p2 = (await clients.post(f"/vibe/sessions/{s['id']}/actions", json={"kind": "publish"}, headers=h)).json()
    assert p2["event"]["version"] == 2                                                   # a new version
    versions = (await clients.get(f"/vibe/sessions/{s['id']}/versions", headers=h)).json()["versions"]
    assert versions[0]["published_version"] == 2 and versions[0]["author"] == "maker"
    body = (await clients.get(f"/vibe/sessions/{s['id']}", headers=h)).json()
    assert [m["kind"] for m in body["messages"] if m["role"] == "event"] == ["test", "publish", "publish"]


async def test_another_person_cannot_read_it(clients: AsyncClient, vibe_on):
    h = await _browser(clients)
    s = (await clients.post("/vibe/sessions", json={}, headers=h)).json()
    other = AsyncClient(transport=clients._transport, base_url="http://registry")
    code = (await other.post("/auth/email/start", json={"email": "someone@example.com"})).json()["dev_code"]
    await other.post("/auth/email/verify", json={"email": "someone@example.com", "code": code})
    await other.post("/orgs", json={"name": "Other team"})
    slug = (await other.get("/orgs")).json()[0]["slug"]
    assert (await other.get(f"/vibe/sessions/{s['id']}", headers={"X-Treg-Org": slug})).status_code == 404


async def test_idle_conversations_keep_the_draft_and_a_summary(clients: AsyncClient, vibe_on, monkeypatch):
    from treg.application import vibe as vibe_app
    h = await _browser(clients)
    _fake_model(monkeypatch, [llm.Turn(text="Sure, a greeter.", tool_calls=[], ms=1)])
    s = (await clients.post("/vibe/sessions", json={}, headers=h)).json()
    await clients.put(f"/vibe/sessions/{s['id']}/draft", json={"manifest": MANIFEST}, headers=h)
    await _say(clients, s["id"], "build me a greeter", h)
    async with session_maker() as db:
        row = await db.get(VibeSession, s["id"])
        await db.execute(update(VibeSession).where(VibeSession.id == row.id)
                         .values(updated_at=row.updated_at - timedelta(days=40)))
        await db.commit()
        assert await vibe_app.trim_idle(db) == 1
        await db.commit()
    body = (await clients.get(f"/vibe/sessions/{s['id']}", headers=h)).json()
    assert body["messages"] == [] and body["trimmed"] is True
    assert "build me a greeter" in body["summary"] and body["draft"]["manifest"]["name"] == "greeter"


async def test_team_deletion_takes_its_conversations(clients: AsyncClient, vibe_on, monkeypatch):
    from treg.domain.governance.teams import cascade_delete_org
    h = await _browser(clients)
    _fake_model(monkeypatch, [llm.Turn(text="ok", tool_calls=[], ms=1)])
    s = (await clients.post("/vibe/sessions", json={}, headers=h)).json()
    await clients.put(f"/vibe/sessions/{s['id']}/draft", json={"manifest": MANIFEST}, headers=h)
    await _say(clients, s["id"], "hi", h)
    async with session_maker() as db:
        row = await db.get(VibeSession, s["id"])
        await cascade_delete_org(await db.get(Org, row.org_id), db)
        await db.commit()
        assert (await db.execute(select(VibeSession))).scalars().all() == []
        assert (await db.execute(select(VibeMessage))).scalars().all() == []
        assert (await db.execute(select(VibeDraft))).scalars().all() == []


async def test_catalog_get_says_whether_the_team_can_call_it(clients: AsyncClient, vibe_on, monkeypatch):
    """A step the team holds no key for (and treg serves no shared key for) fails every test run
    with a 404 before any provider: the agent is told before it builds on it."""
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", ""); get_settings.cache_clear()
    h = await _browser(clients)
    _fake_model(monkeypatch, [llm.Turn(text="", tool_calls=[_call("catalog_get", id="scrapecreators.reddit.search.posts")], ms=1)])
    s = (await clients.post("/vibe/sessions", json={}, headers=h)).json()
    body, _ = await _say(clients, s["id"], "reddit", h)
    tool = next(m for m in body["messages"] if m["role"] == "tool")
    assert "cannot call it yet" in tool["summary"]
    async with session_maker() as db:
        msg = (await db.execute(select(VibeMessage).where(VibeMessage.role == "tool"))).scalars().first()
        assert '"callable": false' in msg.content["result"] and "treg connections connect" in msg.content["result"]



async def _with_files(clients: AsyncClient, h: dict) -> dict:
    s = (await clients.post("/vibe/sessions", json={}, headers=h)).json()
    await clients.put(f"/vibe/sessions/{s['id']}/draft", json={"manifest": MANIFEST, "script": SCRIPT, "check": CHECK,
                                                              "readme": "Says hi."}, headers=h)
    return s


async def test_a_test_run_waits_for_the_makers_button(clients: AsyncClient, vibe_on, monkeypatch):
    h = await _browser(clients)
    seen = _fake_model(monkeypatch, [
        llm.Turn(text="Shall I run it? About $0.", tool_calls=[_call("test_run", inputs={"name": "Bo"}, est_cost_usd=0)], ms=1),
        llm.Turn(text="should never be asked", tool_calls=[], ms=1),
    ])
    s = await _with_files(clients, h)
    body, events = await _say(clients, s["id"], "test it", h)
    assert len(seen) == 1                                   # it stopped and waits
    assert body["pending"]["kind"] == "test" and body["pending"]["inputs"] == {"name": "Bo"}
    assert any(e["type"] == "pending" for e in events)
    assert not [m for m in body["messages"] if m["role"] == "event"]   # nothing ran
    r = (await clients.post(f"/vibe/sessions/{s['id']}/actions",
                            json={"kind": "test", "inputs": body["pending"]["inputs"]}, headers=h)).json()
    assert r["event"]["ok"] is True and r["event"]["detail"]["output"] == {"greeting": "hi Bo"}
    body = (await clients.get(f"/vibe/sessions/{s['id']}", headers=h)).json()
    assert body["pending"] is None
    # the agent reads the press as a note on its next turn
    _fake_model(monkeypatch, [])
    seen = _fake_model(monkeypatch, [llm.Turn(text="Looks right.", tool_calls=[], ms=1)])
    await _say(clients, s["id"], "how did it go?", h)
    notes = [m["content"] for m in seen[0] if m["role"] == "user" and "pressed a button" in (m["content"] or "")]
    assert notes and "hi Bo" in notes[0]


async def test_always_allow_lets_the_agent_test_run(clients: AsyncClient, vibe_on, monkeypatch):
    h = await _browser(clients)
    s = await _with_files(clients, h)
    r = (await clients.post(f"/vibe/sessions/{s['id']}/actions", json={"kind": "test", "inputs": {"name": "A"}, "always": True},
                            headers=h)).json()
    assert r["auto_test"] is True
    _fake_model(monkeypatch, [llm.Turn(text="", tool_calls=[_call("test_run", inputs={"name": "Cy"}, est_cost_usd=0)], ms=1),
                              llm.Turn(text="It says hi Cy.", tool_calls=[], ms=1)])
    body, _ = await _say(clients, s["id"], "try Cy", h)
    tool = next(m for m in body["messages"] if m["role"] == "tool")
    assert tool["ok"] is True and "hi Cy" in tool["result"] and body["pending"] is None


async def test_publish_and_app_are_buttons_too(clients: AsyncClient, vibe_on, monkeypatch):
    h = await _browser(clients)
    s = await _with_files(clients, h)
    _fake_model(monkeypatch, [llm.Turn(text="Publish?", tool_calls=[_call("publish")], ms=1)])
    body, _ = await _say(clients, s["id"], "ship it", h)
    assert body["pending"]["kind"] == "publish" and body["tool_id"] is None
    await clients.post(f"/vibe/sessions/{s['id']}/actions", json={"kind": "publish"}, headers=h)
    _fake_model(monkeypatch, [llm.Turn(text="An app?", tool_calls=[_call("app_on")], ms=1)])
    body, _ = await _say(clients, s["id"], "and a page", h)
    assert body["pending"]["kind"] == "app"
    ev = (await clients.post(f"/vibe/sessions/{s['id']}/actions", json={"kind": "app"}, headers=h)).json()["event"]
    assert ev["ok"] is True and ev["url"].endswith("/greeter")
    status = (await clients.get(f"/vibe/sessions/{s['id']}/status", headers=h)).json()
    assert status["tool"]["status"] == "live" and status["tool"]["app"]["enabled"] is True
    assert status["tool"]["share_url"] == f"/hub/{body['tool_id']}"


async def test_the_password_never_reaches_the_conversation(clients: AsyncClient, vibe_on, monkeypatch):
    h = await _browser(clients)
    s = await _with_files(clients, h)
    await clients.post(f"/vibe/sessions/{s['id']}/actions", json={"kind": "publish"}, headers=h)
    await clients.post(f"/vibe/sessions/{s['id']}/actions", json={"kind": "app"}, headers=h)
    ev = (await clients.post(f"/vibe/sessions/{s['id']}/actions", json={"kind": "password", "password": "hunter2-secret"},
                             headers=h)).json()["event"]
    assert ev["ok"] is True and ev["summary"] == "App password set"
    status = (await clients.get(f"/vibe/sessions/{s['id']}/status", headers=h)).json()
    assert status["tool"]["app"]["locked"] is True
    # a password press without a password never removes the lock; removing says so
    r = await clients.post(f"/vibe/sessions/{s['id']}/actions", json={"kind": "password"}, headers=h)
    assert r.status_code == 422 and r.json()["detail"]["error"] == "vibe_password_missing"
    assert (await clients.get(f"/vibe/sessions/{s['id']}/status", headers=h)).json()["tool"]["app"]["locked"] is True
    ev = (await clients.post(f"/vibe/sessions/{s['id']}/actions", json={"kind": "password", "clear": True},
                             headers=h)).json()["event"]
    assert ev["summary"] == "App password removed"
    async with session_maker() as db:
        for m in (await db.execute(select(VibeMessage))).scalars().all():
            assert "hunter2" not in json.dumps(m.content)


async def test_not_now_clears_the_ask(clients: AsyncClient, vibe_on, monkeypatch):
    h = await _browser(clients)
    s = await _with_files(clients, h)
    _fake_model(monkeypatch, [llm.Turn(text="", tool_calls=[_call("publish")], ms=1)])
    await _say(clients, s["id"], "ship", h)
    ev = (await clients.post(f"/vibe/sessions/{s['id']}/actions", json={"kind": "skip"}, headers=h)).json()["event"]
    assert ev["summary"] == "Not now: publishing"
    assert (await clients.get(f"/vibe/sessions/{s['id']}", headers=h)).json()["pending"] is None


async def test_edit_a_published_tool_from_a_new_conversation(clients: AsyncClient, vibe_on, monkeypatch):
    h = await _browser(clients)
    s = await _with_files(clients, h)
    tool_id = (await clients.post(f"/vibe/sessions/{s['id']}/actions", json={"kind": "publish"}, headers=h)).json()["tool_id"]
    # from the page's picker
    s2 = (await clients.post("/vibe/sessions", json={"from_tool": tool_id}, headers=h)).json()
    assert s2["tool_id"] == tool_id and s2["draft"]["script"] == SCRIPT and "version" not in s2["draft"]["manifest"]
    assert s2["messages"][0]["kind"] == "load"
    assert (await clients.post(f"/vibe/sessions/{s2['id']}/validate", headers=h)).json()["problem"] is None
    assert (await clients.post("/vibe/sessions", json={"from_tool": "nobody.nothing"}, headers=h)).status_code == 404
    # by the agent, in any conversation
    s3 = (await clients.post("/vibe/sessions", json={}, headers=h)).json()
    _fake_model(monkeypatch, [llm.Turn(text="", tool_calls=[_call("load_my_tool", tool_id=tool_id)], ms=1),
                              llm.Turn(text="Loaded.", tool_calls=[], ms=1)])
    body, _ = await _say(clients, s3["id"], f"change {tool_id}", h)
    assert body["tool_id"] == tool_id and body["draft"]["manifest"]["name"] == "greeter"


async def test_versions_restore_and_regenerate(clients: AsyncClient, vibe_on, monkeypatch):
    h = await _browser(clients)
    s = await _with_files(clients, h)                                   # draft 1, by the maker
    _fake_model(monkeypatch, [llm.Turn(text="", tool_calls=[_call("write_files", readme="Says hello.")], ms=1),
                              llm.Turn(text="Changed the readme.", tool_calls=[], ms=1)])
    body, _ = await _say(clients, s["id"], "reword the readme", h)
    step = next(m for m in body["messages"] if m["role"] == "tool")
    assert (step["prev_version"], step["version"]) == (1, 2)
    v = (await clients.get(f"/vibe/sessions/{s['id']}/versions/2", headers=h)).json()
    assert v["files"]["readme"] == "Says hello." and v["prev"]["readme"] == "Says hi."
    r = (await clients.post(f"/vibe/sessions/{s['id']}/versions/1/restore", headers=h)).json()
    assert r["draft"]["readme"] == "Says hi." and r["version"] == 3
    # regenerate: the answer goes, the files go back to how they were when the message was sent
    await clients.put(f"/vibe/sessions/{s['id']}/draft", json={"readme": "Something else."}, headers=h)
    _fake_model(monkeypatch, [llm.Turn(text="", tool_calls=[_call("write_files", readme="Hi there.")], ms=1),
                              llm.Turn(text="Done again.", tool_calls=[], ms=1)])
    body, _ = await _say(clients, s["id"], "", h, path="regenerate")
    assert [m["role"] for m in body["messages"] if m["role"] != "event"] == ["user", "assistant", "tool", "assistant"]
    assert body["messages"][-1]["text"] == "Done again." and body["draft"]["readme"] == "Hi there."
    versions = (await clients.get(f"/vibe/sessions/{s['id']}/versions", headers=h)).json()["versions"]
    assert versions[1]["note"] == "back to before the regenerated answer"
    assert [vv for vv in versions if vv["n"] == versions[1]["n"]][0]


async def test_stop_ends_the_turn(clients: AsyncClient, vibe_on, monkeypatch):
    from treg.application import vibe as vibe_app
    h = await _browser(clients)
    s = await _with_files(clients, h)
    calls = []

    async def chat_stream(messages, tools, *, on_text=None, **kw):
        calls.append(1)
        async with session_maker() as db:                       # the maker presses Stop mid-run
            await vibe_app.request_stop(db, s["id"])
            await db.commit()
        return llm.Turn(text="", tool_calls=[_call("my_tools")], ms=1)
    monkeypatch.setattr(llm, "chat_stream", chat_stream)
    body, _ = await _say(clients, s["id"], "go", h)
    assert len(calls) == 1 and body["messages"][-1]["text"] == "Stopped." and body["busy"] is False


async def test_one_agent_at_a_time(clients: AsyncClient, vibe_on):
    from treg.application import vibe as vibe_app
    h = await _browser(clients)
    s = await _with_files(clients, h)
    async with session_maker() as db:
        token = await vibe_app.claim_run(db, s["id"])
        assert token
        await db.commit()
    r = await clients.post(f"/vibe/sessions/{s['id']}/messages", json={"text": "hi"}, headers=h)
    assert r.status_code == 409 and r.json()["detail"]["error"] == "vibe_working"
    assert (await clients.post(f"/vibe/sessions/{s['id']}/actions", json={"kind": "skip"}, headers=h)).status_code == 409
    # the maker's edit would be lost under the agent's next write: refused until it is done
    assert (await clients.put(f"/vibe/sessions/{s['id']}/draft", json={"readme": "mine"}, headers=h)).status_code == 409


async def test_rename_pin_and_attachments(clients: AsyncClient, vibe_on, monkeypatch):
    h = await _browser(clients)
    s = await _with_files(clients, h)
    r = (await clients.patch(f"/vibe/sessions/{s['id']}", json={"title": "Greeter v2", "pinned": True}, headers=h)).json()
    assert r["title"] == "Greeter v2" and r["pinned"] is True
    seen = _fake_model(monkeypatch, [llm.Turn(text="Got the sample.", tool_calls=[], ms=1)])
    body, _ = await _say(clients, s["id"], "here is a sample", h, attachments=[{"name": "sample.json", "text": '{"a": 1}'}])
    assert body["messages"][0]["attachments"] == [{"name": "sample.json", "size": 8}]
    user = [m for m in seen[0] if m["role"] == "user"][-1]["content"]
    assert "sample.json" in user and '{"a": 1}' in user


async def test_search_uses_the_find_engine(clients: AsyncClient, vibe_on, monkeypatch):
    from treg.application import catalog_find
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", ""); get_settings.cache_clear()
    monkeypatch.setattr(catalog_find, "configured", lambda: True)

    async def admit(_):
        return True

    async def stream(query, provider_display, platform=None, evidence=None):
        yield {"event": "candidates", "candidates": []}
        yield {"event": "judged", "verdict": "strong", "rows": [
            {"id": "scrapecreators.reddit.search.posts", "name": "Search Reddit posts", "provider": "scrapecreators",
             "cost": {"usd": 0.002, "type": "per_call"}, "p": 0.93}]}
    monkeypatch.setattr(catalog_find, "admit", admit)
    monkeypatch.setattr(catalog_find, "stream", stream)
    h = await _browser(clients)
    s = (await clients.post("/vibe/sessions", json={}, headers=h)).json()
    _fake_model(monkeypatch, [llm.Turn(text="", tool_calls=[_call("catalog_search", query="reddit posts about my brand")], ms=1)])
    body, _ = await _say(clients, s["id"], "brand mentions", h)
    tool = next(m for m in body["messages"] if m["role"] == "tool")
    result = json.loads(tool["result"])
    assert result["verdict"] == "strong" and result["results"][0]["fit"] == 0.93
    assert result["results"][0]["callable"] is False and "fix" in result["results"][0]
    state = (await clients.get("/vibe/state", headers=h)).json()
    assert state["left_micro"] < 1_000_000                   # the judge's request counts against the budget


async def test_warnings_before_a_test_run(clients: AsyncClient, vibe_on, monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", ""); get_settings.cache_clear()
    h = await _browser(clients)
    s = (await clients.post("/vibe/sessions", json={}, headers=h)).json()
    m = {**MANIFEST, "uses": ["scrapecreators.reddit.search.posts"]}
    await clients.put(f"/vibe/sessions/{s['id']}/draft", json={"manifest": m}, headers=h)
    warn = (await clients.get(f"/vibe/sessions/{s['id']}/status", headers=h)).json()["warnings"]
    access = [w for w in warn if w["kind"] == "access"]
    assert access and access[0]["id"] == "scrapecreators.reddit.search.posts" and access[0]["fix"]


def test_a_stopped_turn_still_pairs_every_tool_call():
    from treg.application.vibe.agent import _paired
    out = _paired([{"role": "assistant", "content": None, "tool_calls": [{"id": "a"}, {"id": "b"}]},
                   {"role": "tool", "tool_call_id": "a", "content": "x"}, {"role": "user", "content": "next"}])
    assert [m.get("tool_call_id") for m in out if m["role"] == "tool"] == ["a", "b"]



def test_old_writes_and_results_are_not_resent_in_full():
    from types import SimpleNamespace as NS
    from treg.application.vibe.agent import _history
    big = "x" * 5000
    rows = [NS(role="user", content={"text": "go"}),
            NS(role="assistant", content={"text": "", "tool_calls": [{"id": "w", "name": "write_files", "arguments": {"script": big}}]}),
            NS(role="tool", content={"tool_call_id": "w", "result": big}),
            NS(role="user", content={"text": "again"})]
    out = _history(NS(summary=None), rows)
    assert big not in out[1]["tool_calls"][0]["function"]["arguments"]
    assert "5000 characters, written" in out[1]["tool_calls"][0]["function"]["arguments"]
    assert len(out[2]["content"]) < 1500


async def test_one_message_has_a_spending_cap(clients: AsyncClient, vibe_on, monkeypatch):
    from treg.application.vibe import agent
    monkeypatch.setattr(agent, "MESSAGE_CAP_MICRO", 30_000)
    h = await _browser(clients)
    _fake_model(monkeypatch, [llm.Turn(text="", tool_calls=[_call("my_tools")], ms=1, cost_usd=0.02)] * 5)
    s = (await clients.post("/vibe/sessions", json={}, headers=h)).json()
    body, _ = await _say(clients, s["id"], "go", h)
    assert "the most one message may" in body["messages"][-1]["text"]
    assert sum(1 for m in body["messages"] if m["role"] == "assistant" and m.get("cost_micro")) == 2


async def test_read_files_returns_a_file_in_full(clients: AsyncClient, vibe_on, monkeypatch):
    h = await _browser(clients)
    s = await _with_files(clients, h)
    _fake_model(monkeypatch, [llm.Turn(text="", tool_calls=[_call("read_files", file="script")], ms=1)])
    body, _ = await _say(clients, s["id"], "read it", h)
    tool = next(m for m in body["messages"] if m["role"] == "tool")
    assert json.loads(tool["result"])["text"] == SCRIPT



async def test_continue_a_draft_in_another_conversation(clients: AsyncClient, vibe_on, monkeypatch):
    h = await _browser(clients)
    s = await _with_files(clients, h)                                    # an unpublished draft
    state = (await clients.get("/vibe/state", headers=h)).json()
    assert next(x for x in state["sessions"] if x["id"] == s["id"])["has_files"] is True
    # from the page
    s2 = (await clients.post("/vibe/sessions", json={"from_draft": s["id"]}, headers=h)).json()
    assert s2["draft"]["script"] == SCRIPT and s2["tool_id"] is None and s2["messages"][0]["kind"] == "load"
    assert s2["title"] == "greeter (copy)"
    await clients.put(f"/vibe/sessions/{s2['id']}/draft", json={"readme": "Changed here only."}, headers=h)
    assert (await clients.get(f"/vibe/sessions/{s['id']}", headers=h)).json()["draft"]["readme"] == "Says hi."  # a copy
    assert (await clients.post("/vibe/sessions", json={"from_draft": 999999}, headers=h)).status_code == 404
    # by the agent: my_tools lists the drafts, load_draft copies one
    s3 = (await clients.post("/vibe/sessions", json={}, headers=h)).json()
    _fake_model(monkeypatch, [llm.Turn(text="", tool_calls=[_call("my_tools")], ms=1),
                              llm.Turn(text="", tool_calls=[_call("load_draft", conversation_id=s["id"])], ms=1),
                              llm.Turn(text="Copied.", tool_calls=[], ms=1)])
    body, _ = await _say(clients, s3["id"], "continue my greeter draft", h)
    listed = json.loads(next(m for m in body["messages"] if m.get("name") == "my_tools")["result"])
    assert s["id"] in [d["conversation_id"] for d in listed["drafts"]]
    assert body["draft"]["manifest"]["name"] == "greeter"



async def test_a_secret_test_input_never_reaches_the_conversation(clients: AsyncClient, vibe_on, monkeypatch):
    h = await _browser(clients)
    s = (await clients.post("/vibe/sessions", json={}, headers=h)).json()
    m = {**MANIFEST, "inputs": {"name": {"type": "string", "example": "Ada"},
                                "token": {"type": "string", "secret": True}}}
    check = {"inputs": {"name": "Ada", "token": "x"}, "fields": ["greeting"]}
    await clients.put(f"/vibe/sessions/{s['id']}/draft", json={"manifest": m, "script": SCRIPT, "check": check,
                                                              "readme": "Says hi."}, headers=h)
    ev = (await clients.post(f"/vibe/sessions/{s['id']}/actions",
                             json={"kind": "test", "inputs": {"name": "Bo", "token": "not-a-real-token"}}, headers=h)).json()["event"]
    assert ev["ok"] is True and ev["inputs"] == {"name": "Bo", "token": "••••••"}
    seen = _fake_model(monkeypatch, [llm.Turn(text="ok", tool_calls=[], ms=1)])
    await _say(clients, s["id"], "how did it go?", h)
    assert "not-a-real-token" not in json.dumps(seen)
    async with session_maker() as db:
        for msg in (await db.execute(select(VibeMessage))).scalars().all():
            assert "not-a-real-token" not in json.dumps(msg.content)



async def test_spend_from_two_conversations_at_once_adds_up(clients: AsyncClient, vibe_on):
    import asyncio as aio
    from treg.application import vibe as vibe_app
    h = await _browser(clients)
    s = (await clients.post("/vibe/sessions", json={}, headers=h)).json()
    async with session_maker() as db:
        user_id = (await db.get(VibeSession, s["id"])).user_id

    async def one(n: int) -> None:
        async with session_maker() as db:
            await vibe_app.spend(db, user_id, n)
            await db.commit()
    await aio.gather(*(one(1000) for _ in range(8)))
    async with session_maker() as db:
        assert await vibe_app.spent_micro(db, user_id) == 8000



def test_a_turn_without_a_reported_cost_is_still_charged():
    from treg.application.vibe.agent import turn_cost
    history = [{"role": "user", "content": "x" * 3000}]
    assert turn_cost(llm.Turn(text="hi", tool_calls=[], ms=1, cost_usd=0.002), history, False) == 2000
    stopped = turn_cost(llm.Turn(text="", tool_calls=[], ms=1), history, True)
    assert stopped >= 1000 * 5                                   # the input is counted from the history
    assert turn_cost(llm.Turn(text="", tool_calls=[], ms=1, error="ConnectError"), history, False) == 0


async def test_a_stale_run_never_clears_a_newer_ones_mark(clients: AsyncClient, vibe_on):
    from treg.application import vibe as vibe_app
    h = await _browser(clients)
    s = (await clients.post("/vibe/sessions", json={}, headers=h)).json()
    async with session_maker() as db:
        old = await vibe_app.claim_run(db, s["id"])
        from treg.timeutil import utcnow_naive
        await db.execute(update(VibeSession).where(VibeSession.id == s["id"])
                         .values(running_since=utcnow_naive() - timedelta(hours=1)))   # it died
        new = await vibe_app.claim_run(db, s["id"])
        assert old and new and old != new
        assert await vibe_app.touch_run(db, s["id"], old) is False      # the old run learns it lost
        await vibe_app.release_run(db, s["id"], old)                    # and cannot clear the new mark
        await db.commit()
        assert vibe_app.running(await db.get(VibeSession, s["id"]))



async def test_data_csv_is_kept_once_across_versions(clients: AsyncClient, vibe_on):
    from treg.application import vibe as vibe_app
    h = await _browser(clients)
    s = await _with_files(clients, h)
    csv = "name\nAda\nBo\n" * 1000
    await clients.put(f"/vibe/sessions/{s['id']}/draft", json={"data": csv}, headers=h)          # version 2 holds it
    await clients.put(f"/vibe/sessions/{s['id']}/draft", json={"readme": "Now with data."}, headers=h)   # version 3 refers
    async with session_maker() as db:
        v3 = await vibe_app.version(db, s["id"], 3)
        assert "data" not in v3.files and v3.files["__data_of"] == 2
        assert (await vibe_app.files_of(db, v3))["data"] == csv
    v = (await clients.get(f"/vibe/sessions/{s['id']}/versions/3", headers=h)).json()
    assert v["files"]["data"] == csv and "__data_of" not in v["files"]
    r = (await clients.post(f"/vibe/sessions/{s['id']}/versions/1/restore", headers=h)).json()
    assert "data" not in r["draft"]
    r = (await clients.post(f"/vibe/sessions/{s['id']}/versions/3/restore", headers=h)).json()
    assert r["draft"]["data"] == csv


async def test_a_resumed_conversation_is_trimmed_again(clients: AsyncClient, vibe_on, monkeypatch):
    from treg.application import vibe as vibe_app
    h = await _browser(clients)
    s = await _with_files(clients, h)

    async def idle_and_trim():
        async with session_maker() as db:
            from treg.timeutil import utcnow_naive
            await db.execute(update(VibeSession).where(VibeSession.id == s["id"])
                             .values(updated_at=utcnow_naive() - timedelta(days=40)))
            n = await vibe_app.trim_idle(db)
            await db.commit()
            return n
    _fake_model(monkeypatch, [llm.Turn(text="first answer", tool_calls=[], ms=1)])
    await _say(clients, s["id"], "first ask", h)
    assert await idle_and_trim() == 1
    _fake_model(monkeypatch, [llm.Turn(text="second answer", tool_calls=[], ms=1)])
    await _say(clients, s["id"], "second ask", h)
    assert await idle_and_trim() == 1
    body = (await clients.get(f"/vibe/sessions/{s['id']}", headers=h)).json()
    assert body["messages"] == [] and "first ask" in body["summary"] and "second ask" in body["summary"]
