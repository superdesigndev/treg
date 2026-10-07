"""A hub tool's web page, the maker's side (docs/context/architecture/hub-apps.md): the app record,
its name, its optional password, and the flag that hides all of it."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from conftest import funded_user
from sqlalchemy import select, update

from treg.config import get_settings
from treg.domain.hub import ManifestError
from treg.domain.hub.apps import default_app_name, hash_password, validate_app_name, validate_password, verify_password
from treg.infra.db import session_maker
from treg.models import HubApp, HubTool, Membership, Org
from tests.test_hub import _publish_live, hub_on  # noqa: F401 - the hub flag, through the environment


@pytest.fixture
def apps_on(hub_on, monkeypatch):  # noqa: F811
    monkeypatch.setenv("TREG_HUB_APPS_ENABLED", "1")
    # open to every team, whatever a local .env limits the hub to
    monkeypatch.setenv("TREG_HUB_TEAMS", "")
    monkeypatch.setenv("TREG_HUB_USERS", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------------------------
# The rules

@pytest.mark.parametrize("name", ["a", "leads-db", "x1", "a" * 48])
def test_app_names_accepted(name):
    assert validate_app_name(name) == name


@pytest.mark.parametrize("name", ["", "-a", "a-", "Leads", "a_b", "a.b", "a/b", "a" * 49, None, 3])
def test_app_names_refused(name):
    with pytest.raises(ManifestError) as e:
        validate_app_name(name)
    assert e.value.field == "name"


def test_default_name_is_the_tools_own():
    assert default_app_name("acme.leads-db") == "leads-db"
    assert default_app_name("acme.leads-db@3") == "leads-db"


def test_password_hash_round_trip():
    h = hash_password("correct horse")
    assert h.startswith("scrypt$") and "correct horse" not in h
    assert verify_password("correct horse", h)
    assert not verify_password("wrong horse", h)
    assert hash_password("correct horse") != h          # a fresh salt each time


@pytest.mark.parametrize("stored", [None, "", "plain", "scrypt$1$2", "bcrypt$a$b$c$d$e", "scrypt$x$8$1$AA$AA"])
def test_malformed_hash_never_matches(stored):
    assert not verify_password("anything-at-all", stored)


@pytest.mark.parametrize("pw", ["short", "x" * 129, None, 12345678])
def test_password_length(pw):
    with pytest.raises(ManifestError):
        validate_password(pw)


# ---------------------------------------------------------------------------------------------
# The flag

async def test_routes_hidden_without_the_apps_flag(clients: AsyncClient, hub_on):  # noqa: F811
    tool_id = await _publish_live(clients)
    assert (await clients.get(f"/hub/tools/{tool_id}/app")).status_code == 404
    assert (await clients.put(f"/hub/tools/{tool_id}/app", json={})).status_code == 404
    assert (await clients.get("/meta")).json()["hub_apps"] is False


async def test_meta_says_apps_exist(clients: AsyncClient, apps_on):
    assert (await clients.get("/meta")).json()["hub_apps"] is True


# ---------------------------------------------------------------------------------------------
# The maker's routes

async def _publish_another(clients: AsyncClient, name: str) -> str:
    """A second live tool of the same team (`_publish_live` registers the own tool it needs)."""
    from tests.test_hub import CHECK, _steps_manifest
    tool_id = (await clients.post("/hub/tools", json={
        "manifest": _steps_manifest(name=name), "check": CHECK, "readme": "x"})).json()["tool_id"]
    async with session_maker() as s:
        await s.execute(update(HubTool).where(HubTool.tool_id == tool_id).values(status="live"))
        await s.commit()
    return tool_id


async def test_turn_on_names_it_after_the_tool(clients: AsyncClient, apps_on):
    tool_id = await _publish_live(clients)
    assert (await clients.get(f"/hub/tools/{tool_id}/app")).json()["enabled"] is False
    r = await clients.put(f"/hub/tools/{tool_id}/app", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    team = tool_id.split(".", 1)[0]
    assert body["enabled"] is True and body["name"] == "leads-db"
    assert body["url"].endswith(f"/apps/{team}/leads-db")
    assert body["password"] is False and body["locked"] is False
    assert (await clients.get(f"/hub/tools/{tool_id}/app")).json() == body


async def test_rename_and_refusals(clients: AsyncClient, apps_on):
    tool_id = await _publish_live(clients)
    other = await _publish_another(clients, "other-tool")
    await clients.put(f"/hub/tools/{tool_id}/app", json={})
    assert (await clients.put(f"/hub/tools/{tool_id}/app", json={"name": "leads"})).json()["name"] == "leads"
    r = await clients.put(f"/hub/tools/{tool_id}/app", json={"name": "Bad Name"})
    assert r.status_code == 422 and r.json()["detail"]["field"] == "name"
    r = await clients.put(f"/hub/tools/{other}/app", json={"name": "leads"})   # taken within the team
    assert r.status_code == 422 and "already uses" in r.json()["detail"]["rule"]
    assert (await clients.put(f"/hub/tools/{other}/app", json={"name": "other"})).status_code == 200


async def test_old_links_redirect_after_a_rename(clients: AsyncClient, apps_on):
    """A shared link outlives a rename: the app's old name and the team's old slug redirect to
    where the app is now. A current name always wins over another app's old one."""
    tool_id = await _publish_live(clients)
    team = tool_id.split(".", 1)[0]
    await clients.put(f"/hub/tools/{tool_id}/app", json={})
    await clients.put(f"/hub/tools/{tool_id}/app", json={"name": "leads"})
    r = await clients.get(f"/apps/{team}/leads-db?x=1", follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == f"/apps/{team}/leads?x=1"
    assert (await clients.get(f"/apps/{team}/leads", follow_redirects=False)).status_code != 308   # current
    assert (await clients.get(f"/apps/{team}/never-was", follow_redirects=False)).status_code == 404
    # another app may take the old name; then it is that app's, no redirect
    other = await _publish_another(clients, "other-tool")
    await clients.put(f"/hub/tools/{other}/app", json={"name": "leads-db"})
    assert (await clients.get(f"/apps/{team}/leads-db", follow_redirects=False)).status_code != 308
    assert (await clients.get(f"/apps/{team}/leads-db/contract")).json()["name"] == "other-tool"
    # the team renames itself: its old slug still finds the app
    async with session_maker() as s:
        org = (await s.execute(select(Org).where(Org.slug == team))).scalars().one()
        org.previous_slug, org.slug = team, "renamed-team"
        await s.commit()
    r = await clients.get(f"/apps/{team}/leads", follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == "/apps/renamed-team/leads"


async def test_needs_a_live_version(clients: AsyncClient, apps_on):
    tool_id = await _publish_live(clients)
    async with session_maker() as s:
        await s.execute(update(HubTool).where(HubTool.tool_id == tool_id).values(status="failed"))
        await s.commit()
    assert (await clients.put(f"/hub/tools/{tool_id}/app", json={})).status_code == 404
    assert (await clients.put("/hub/tools/nobody.nothing/app", json={})).status_code == 404


async def test_another_team_cannot_touch_it(clients: AsyncClient, apps_on):
    tool_id = await _publish_live(clients)
    await clients.put(f"/hub/tools/{tool_id}/app", json={})
    h = {"X-Treg-Token": (await funded_user(clients, "stranger@example.com"))["token"]}
    assert (await clients.put(f"/hub/tools/{tool_id}/app", json={}, headers=h)).status_code == 404
    assert (await clients.delete(f"/hub/tools/{tool_id}/app", headers=h)).status_code == 404
    assert (await clients.put(f"/hub/tools/{tool_id}/app/password", json={"password": "hijacked!!"},
                              headers=h)).status_code == 404
    r = await clients.get(f"/hub/tools/{tool_id}/app", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is False


async def test_viewer_cannot_change_it(clients: AsyncClient, apps_on):
    tool_id = await _publish_live(clients)
    async with session_maker() as s:
        await s.execute(update(Membership).values(role="viewer"))
        await s.commit()
    assert (await clients.put(f"/hub/tools/{tool_id}/app", json={})).status_code == 403


async def test_off_keeps_the_name_and_on_brings_it_back(clients: AsyncClient, apps_on):
    tool_id = await _publish_live(clients)
    await clients.put(f"/hub/tools/{tool_id}/app", json={"name": "keepme"})
    await clients.put(f"/hub/tools/{tool_id}/app/password", json={"password": "a long secret"})
    off = (await clients.delete(f"/hub/tools/{tool_id}/app")).json()
    assert off["enabled"] is False and off["name"] == "keepme" and off["password"] is True
    assert off["locked"] is False                    # off lifts the lock
    on = (await clients.put(f"/hub/tools/{tool_id}/app", json={})).json()
    assert on["enabled"] is True and on["name"] == "keepme" and on["locked"] is True


async def test_password_is_stored_only_as_a_hash(clients: AsyncClient, apps_on):
    tool_id = await _publish_live(clients)
    assert (await clients.put(f"/hub/tools/{tool_id}/app/password",
                              json={"password": "a long secret"})).status_code == 404   # no app yet
    await clients.put(f"/hub/tools/{tool_id}/app", json={})
    r = await clients.put(f"/hub/tools/{tool_id}/app/password", json={"password": "short"})
    assert r.status_code == 422 and r.json()["detail"]["field"] == "password"
    r = await clients.put(f"/hub/tools/{tool_id}/app/password", json={"password": "a long secret"})
    assert r.status_code == 200 and r.json()["password"] is True and r.json()["locked"] is True
    assert "a long secret" not in r.text and "scrypt" not in r.text
    async with session_maker() as s:
        row = await s.get(HubApp, tool_id)
        assert verify_password("a long secret", row.password_hash) and row.lock_version == 1
    cleared = (await clients.put(f"/hub/tools/{tool_id}/app/password", json={"password": None})).json()
    assert cleared["password"] is False and cleared["locked"] is False
    async with session_maker() as s:
        row = await s.get(HubApp, tool_id)
        assert row.password_hash is None and row.lock_version == 2


async def test_team_deletion_takes_the_app(clients: AsyncClient, apps_on):
    from treg.domain.governance.teams import cascade_delete_org
    from treg.models import Org
    tool_id = await _publish_live(clients)
    await clients.put(f"/hub/tools/{tool_id}/app", json={})
    async with session_maker() as s:
        app = await s.get(HubApp, tool_id)
        await cascade_delete_org(await s.get(Org, app.org_id), s)
        await s.commit()
        assert (await s.execute(select(HubApp))).scalars().all() == []


# ---------------------------------------------------------------------------------------------
# The visitor's side and the lock

from tests.test_hub import _live_tool_with_readme  # noqa: E402
from tests.test_marketplace_call import platform_on  # noqa: E402,F401


async def _app(clients, monkeypatch, *, password: str | None = None) -> tuple[str, str]:
    tool_id = (await _live_tool_with_readme(clients, monkeypatch))["tool_id"]
    on = (await clients.put(f"/hub/tools/{tool_id}/app", json={})).json()
    if password:
        await clients.put(f"/hub/tools/{tool_id}/app/password", json={"password": password})
    return tool_id, on["url"].split("://", 1)[1].split("/", 1)[1]   # "apps/<team>/<name>"


async def _stranger(clients) -> dict:
    return {"X-Treg-Token": (await funded_user(clients, "visitor@example.com"))["token"]}


async def test_the_contract_is_public_and_shows_no_makers_side(clients, apps_on, platform_on, monkeypatch):  # noqa: F811
    tool_id, path = await _app(clients, monkeypatch)
    r = await clients.get(f"/{path}/contract", headers={"X-Treg-Token": ""})
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["locked"] is False and c["tool_id"] == tool_id and c["inputs"]["domain"]["example"] == "figma.com"
    assert c["output"] == ["leads"] and c["price"]["seller"].startswith("seller $0.01")
    text = r.text.replace("x.supabase.co", "")
    assert "supabase" not in text and "tikhub" not in text and "uses" not in c and "script" not in c


async def test_unknown_or_off_app_is_404(clients, apps_on, platform_on, monkeypatch):  # noqa: F811
    tool_id, path = await _app(clients, monkeypatch)
    team = path.split("/")[1]
    assert (await clients.get(f"/apps/{team}/nope/contract")).status_code == 404
    await clients.delete(f"/hub/tools/{tool_id}/app")
    assert (await clients.get(f"/{path}/contract")).status_code == 404
    monkeypatch.setenv("TREG_HUB_APPS_ENABLED", "0"); get_settings.cache_clear()
    assert (await clients.get(f"/{path}/contract")).status_code == 404


async def test_a_run_from_the_app_is_the_call_road(clients, apps_on, platform_on, monkeypatch):  # noqa: F811
    tool_id, path = await _app(clients, monkeypatch)
    h = await _stranger(clients)
    direct = await clients.post(f"/call/{tool_id}", json={"domain": "figma.com"}, headers=h)
    via_app = await clients.post(f"/{path}/run", json={"domain": "figma.com"}, headers=h)
    assert direct.status_code == via_app.status_code == 200, via_app.text
    d, a = direct.json(), via_app.json()
    assert d["output"] == a["output"] and d["usage"]["price_micro"] == a["usage"]["price_micro"] == 10_000
    runs = (await clients.get(f"/{path}/runs", headers=h)).json()["runs"]
    assert [r["run_id"] for r in runs] == [a["run_id"], d["run_id"]]
    one = (await clients.get(f"/{path}/runs/{a['run_id']}", headers=h)).json()
    assert one["output"] == a["output"] and one["status"] == "ok"
    # the maker's own history (the publish check) never shows the visitor's runs
    assert a["run_id"] not in [r["run_id"] for r in (await clients.get(f"/{path}/runs")).json()["runs"]]
    assert (await clients.get(f"/{path}/runs/{a['run_id']}")).status_code == 404


async def test_running_needs_a_sign_in(clients, apps_on, platform_on, monkeypatch):  # noqa: F811
    _, path = await _app(clients, monkeypatch)
    r = await clients.post(f"/{path}/run", json={"domain": "x"}, headers={"X-Treg-Token": ""})
    assert r.status_code == 401


async def test_a_cross_site_run_is_refused(clients, apps_on, platform_on, monkeypatch):  # noqa: F811
    _, path = await _app(clients, monkeypatch)
    r = await clients.post(f"/{path}/run", json={"domain": "x"}, headers={**await _stranger(clients), "Origin": "https://evil.example"})
    assert r.status_code == 403


async def test_the_lock_guards_page_and_tool_only_while_the_app_is_on(clients, apps_on, platform_on, monkeypatch):  # noqa: F811
    tool_id, path = await _app(clients, monkeypatch, password="open sesame!")
    h = await _stranger(clients)
    # the page shows only that it is locked
    c = (await clients.get(f"/{path}/contract", headers=h)).json()
    assert c == {"locked": True, "name": "leads-db", "maker": path.split("/")[1], "app": "leads-db"}
    assert (await clients.post(f"/{path}/run", json={"domain": "x"}, headers=h)).status_code == 401
    # the tool itself: no password, a wrong one, the right one
    r = await clients.post(f"/call/{tool_id}", json={"domain": "x"}, headers=h)
    assert r.status_code == 403 and r.json()["detail"]["error"] == "hub_tool_locked"
    r = await clients.post(f"/call/{tool_id}", json={"domain": "x"}, headers={**h, "X-Treg-Tool-Password": "nope nope"})
    assert r.status_code == 403
    r = await clients.post(f"/call/{tool_id}", json={"domain": "x"}, headers={**h, "X-Treg-Tool-Password": "open sesame!"})
    assert r.status_code == 200, r.text
    # the maker's team never needs it
    assert (await clients.post(f"/call/{tool_id}", json={"domain": "x"})).status_code == 200
    assert (await clients.get(f"/{path}/contract")).json()["locked"] is False
    # off lifts the lock on the tool
    await clients.delete(f"/hub/tools/{tool_id}/app")
    assert (await clients.post(f"/call/{tool_id}", json={"domain": "x"}, headers=h)).status_code == 200


async def test_unlock_sets_a_cookie_for_this_app_only(clients, apps_on, platform_on, monkeypatch):  # noqa: F811
    tool_id, path = await _app(clients, monkeypatch, password="open sesame!")
    h = await _stranger(clients)
    assert (await clients.post(f"/{path}/unlock", json={"password": "wrong one!"})).status_code == 401
    r = await clients.post(f"/{path}/unlock", json={"password": "open sesame!"})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert f"Path=/{path}" in cookie and "HttpOnly" in cookie and "open sesame" not in cookie
    assert (await clients.get(f"/{path}/contract", headers=h)).json()["locked"] is False
    assert (await clients.post(f"/{path}/run", json={"domain": "figma.com"}, headers=h)).status_code == 200
    # a new password signs everyone out
    await clients.put(f"/hub/tools/{tool_id}/app/password", json={"password": "new password"})
    assert (await clients.get(f"/{path}/contract", headers=h)).json()["locked"] is True


@pytest.fixture(autouse=True)
def _forget_verified_passwords():
    from treg.application.hub import apps as hub_apps
    hub_apps._VERIFIED.clear()
    yield
    hub_apps._VERIFIED.clear()


async def test_password_tries_are_limited(clients, apps_on, platform_on, monkeypatch):  # noqa: F811
    from treg.application.hub import apps as hub_apps
    monkeypatch.setattr(hub_apps, "TRIES_PER_CLIENT", 3)
    _, path = await _app(clients, monkeypatch, password="open sesame!")
    codes = [(await clients.post(f"/{path}/unlock", json={"password": "wrong one!"})).status_code for _ in range(4)]
    assert codes == [401, 401, 401, 429]
    assert (await clients.post(f"/{path}/unlock", json={"password": "open sesame!"})).status_code == 429


async def test_the_right_password_never_runs_out(clients, apps_on, platform_on, monkeypatch):  # noqa: F811
    """A caller who knows the password is not limited like a guesser: a password verified lately
    passes without spending a try, so the 11th call in five minutes still runs."""
    from treg.application.hub import apps as hub_apps
    monkeypatch.setattr(hub_apps, "TRIES_PER_CLIENT", 3)
    path = (await _app(clients, monkeypatch, password="open sesame!"))[1]
    codes = [(await clients.post(f"/{path}/unlock", json={"password": "open sesame!"})).status_code for _ in range(6)]
    assert codes == [200] * 6
    # a guesser still runs out, and the right password keeps working from memory
    codes = [(await clients.post(f"/{path}/unlock", json={"password": "wrong one!"})).status_code for _ in range(3)]
    assert codes[-1] == 429
    assert (await clients.post(f"/{path}/unlock", json={"password": "open sesame!"})).status_code == 200


async def test_a_locked_tool_leaves_search_and_says_so(clients, apps_on, platform_on, monkeypatch):  # noqa: F811
    tool_id, _ = await _app(clients, monkeypatch, password="open sesame!")
    get = (await clients.get(f"/catalog/endpoints/{tool_id}")).json()["endpoint"]
    assert get["password_protected"] is True
    assert "Password protected" in (await clients.get(f"/hub/{tool_id}")).text
    from treg.application import hub as hub_app
    async with session_maker() as s:
        assert await hub_app.locked_ids(s, [tool_id]) == {tool_id}


async def test_the_password_header_never_reaches_a_step(clients, apps_on, platform_on, monkeypatch):  # noqa: F811
    from treg.application.call import service as call_service
    seen: list = []
    from tests.test_hub import _fake_relay
    real = _fake_relay(200, b'{"data": {"domain": "figma.com"}}')

    async def spy(*a, **kw):
        seen.append(repr(a) + repr(kw))
        return await real(*a, **kw)
    tool_id, _ = await _app(clients, monkeypatch, password="open sesame!")
    monkeypatch.setattr(call_service, "relay", spy)
    h = await _stranger(clients)
    r = await clients.post(f"/call/{tool_id}", json={"domain": "x"}, headers={**h, "X-Treg-Tool-Password": "open sesame!"})
    assert r.status_code == 200 and seen
    assert not any("open sesame" in s or "x-treg-tool-password" in s.lower() for s in seen)


async def test_mcp_lists_hub_app_only_with_apps_on(clients, hub_on, monkeypatch):  # noqa: F811
    from tests.test_mcp import _tool_names
    monkeypatch.setenv("TREG_HUB_TEAMS", ""); monkeypatch.setenv("TREG_HUB_USERS", ""); get_settings.cache_clear()
    token = clients.headers["X-Treg-Token"]
    names = await _tool_names(clients, token)
    assert "hub_create" in names and "hub_app" not in names
    monkeypatch.setenv("TREG_HUB_APPS_ENABLED", "1"); get_settings.cache_clear()
    assert "hub_app" in await _tool_names(clients, token)


async def test_agent_files_mention_apps_only_with_apps_on(clients, hub_on, monkeypatch):  # noqa: F811
    monkeypatch.setenv("TREG_HUB_TEAMS", ""); monkeypatch.setenv("TREG_HUB_USERS", ""); get_settings.cache_clear()
    for f in ("/skill.md", "/llms.txt"):
        text = (await clients.get(f)).text
        assert "treg hub publish" in text and "hub app" not in text and "<!--" not in text, f
    monkeypatch.setenv("TREG_HUB_APPS_ENABLED", "1"); get_settings.cache_clear()
    for f in ("/skill.md", "/llms.txt"):
        text = (await clients.get(f)).text
        assert "treg hub app" in text and "hubapps" not in text, f
