"""Read access to historical server-run records, and local runs logged beside them."""

from __future__ import annotations

from httpx import AsyncClient

from treg import audit
from treg.infra.db import session_maker
from treg.models import RunRecord
from conftest import verified_signup


async def _register_runnable(c: AsyncClient, *, name="sh-skill", value="s3cr3t-value", bin="sh"):
    """A skill whose tool carries the full cli profile — the canonical runnable shape."""
    payload = {
        "name": name,
        "recipe": f"# {name}\n",
        "secrets": [{"local_name": "my-secret", "kind": "env", "value": value}],
        "tools": [{
            "name": name,
            "base_url": "https://api.example.com",
            "cli": {
                "bin": bin,
                "inject": [{"secret": "my-secret", "via": "env", "name": "MY_SECRET"}],
                "enabled": True,
            },
        }],
    }
    r = await c.post("/skills", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


async def _historical_run(clients: AsyncClient):
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        db.add(RunRecord(org_id=org_id, user_email="synthetic@example.invalid",
                         bundle_name="sh-skill", argv=["-c", "echo hi"],
                         exit_code=0, duration_ms=1))
        await db.commit()


async def test_historical_server_runs_remain_readable(clients: AsyncClient):
    await _historical_run(clients)
    r = await clients.get("/runs")
    assert r.status_code == 200
    runs = r.json()
    assert len(runs) == 1
    assert runs[0]["tool"] == "sh-skill"
    assert runs[0]["exit_code"] == 0
    assert runs[0]["argv"] == ["-c", "echo hi"]
    assert runs[0]["where"] == "server"


async def test_runs_log_includes_local_runs_tagged_where(clients: AsyncClient):
    """Historical server runs and new local grants remain distinguishable in /runs."""
    await _register_runnable(clients)
    await _historical_run(clients)
    # a local run is audited as its grant (kind="local_run")
    g = await clients.post("/tools/sh-skill/grant", json={"argv": ["local", "args"]})
    assert g.status_code == 200, g.text
    await audit.drain()
    runs = (await clients.get("/runs")).json()
    where = {r["where"] for r in runs}
    assert where == {"server", "local"}
    local = next(r for r in runs if r["where"] == "local")
    assert local["tool"] == "sh-skill" and local["exit_code"] is None
    assert local["argv"] == ["local", "args"]


async def test_cli_profile_never_advertises_server_execution(clients: AsyncClient):
    payload = {
        "name": "flip",
        "recipe": "x",
        "secrets": [{"local_name": "k", "kind": "env", "value": "vvv"}],
        "tools": [{
            "name": "flip",
            "base_url": "https://api.example.com",
            "cli": {"bin": "sh", "inject": [{"secret": "k", "via": "env", "name": "K"}], "enabled": False},
        }],
    }
    b = await clients.post("/skills", json=payload)
    assert b.status_code == 200, b.text
    tool = (await clients.get("/tools/by-name/flip")).json()
    assert tool["server_runnable"] is False


async def test_historical_server_runs_are_org_isolated(clients: AsyncClient):
    await _historical_run(clients)
    r = await verified_signup(clients, json={"email": "other@superdesign.dev"})
    tok2 = r.json()["token"]
    r = await clients.get("/runs", headers={"X-Treg-Token": tok2})
    assert r.status_code == 200
    assert r.json() == []
