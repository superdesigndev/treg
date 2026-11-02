"""Retained runner primitives and read access to historical server-run records."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlmodel import select

from treg import audit, runner
from treg.infra.db import session_maker
from treg.models import RunRecord, Tool
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


async def _run(name: str, args: list[str], *, timeout_s: int = runner.DEFAULT_TIMEOUT_S):
    async with session_maker() as db:
        tool = (await db.execute(select(Tool).where(Tool.name == name))).scalar_one()
        return await runner.run_tool(tool, args, db, timeout_s=timeout_s)


async def _historical_run(clients: AsyncClient):
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        db.add(RunRecord(org_id=org_id, user_email="synthetic@example.invalid",
                         bundle_name="sh-skill", argv=["-c", "echo hi"],
                         exit_code=0, duration_ms=1))
        await db.commit()


async def test_server_run_applies_resource_limits(clients: AsyncClient):
    # The DoS half of the server-run sandbox: the child gets POSIX rlimits. Core dumps are OFF (a core
    # would spill the injected secret to disk) and the file-size limit is finite (disk-fill guard). We
    # read them back with the shell's own `ulimit`, which reports the limits the child was started with.
    await _register_runnable(clients, name="rl-skill")
    core = await _run("rl-skill", ["-c", "ulimit -c"])
    assert core.stdout.strip() == "0"  # RLIMIT_CORE = 0

    fsize = await _run("rl-skill", ["-c", "ulimit -f"])
    assert fsize.stdout.strip().isdigit()  # a finite block count, not "unlimited"


def test_spawn_preexec_toggles_with_the_setting(monkeypatch):
    # The rlimit preexec is applied only when TREG_RUN_RLIMITS is on (default). Off → no preexec.
    from treg import runner
    from treg.config import get_settings

    monkeypatch.setenv("TREG_RUN_RLIMITS", "false")
    get_settings.cache_clear()
    assert runner._spawn_preexec() is None
    monkeypatch.setenv("TREG_RUN_RLIMITS", "true")
    get_settings.cache_clear()
    assert callable(runner._spawn_preexec())
    get_settings.cache_clear()  # restore for the next test (env reverts via monkeypatch)


async def test_run_injects_secret_and_redacts(clients: AsyncClient):
    await _register_runnable(clients)
    result = await _run("sh-skill", ["-c", "echo tok=$MY_SECRET"])
    assert result.exit_code == 0
    # the secret WAS injected (echo printed it) but is REDACTED before leaving the server
    assert "tok=***" in result.stdout
    assert "s3cr3t-value" not in result.stdout


async def test_run_injects_under_the_cli_inject_name(clients: AsyncClient):
    # The importer stores slugified secret names (`stripe-api-key`); the inject entry carries the
    # env var the CLI actually reads (STRIPE_API_KEY). The runner must inject under the mapped name.
    payload = {
        "name": "slug-skill",
        "recipe": "# slug-skill\n",
        "secrets": [{"local_name": "slug-api-key", "kind": "env", "value": "slug-v4lue"}],
        "tools": [{
            "name": "slug-skill",
            "base_url": "https://api.example.com",
            "cli": {
                "bin": "sh",
                "inject": [{"secret": "slug-api-key", "via": "env", "name": "REAL_ENV_NAME"}],
                "enabled": True,
            },
        }],
    }
    r = await clients.post("/skills", json=payload)
    assert r.status_code == 200, r.text
    result = await _run("slug-skill", ["-c", "echo mapped=${REAL_ENV_NAME:+yes}"])
    assert result.exit_code == 0
    assert "mapped=yes" in result.stdout  # injected under the cli.inject name, not the stored slug


async def test_run_resolves_from_binding_and_sole_secret_injects(clients: AsyncClient):
    # An inject entry with NO explicit secret_id (the sole-bound-secret default, or from_binding)
    # must inject on --server exactly like the local grant path resolves it — one profile, two tiers.
    payload = {
        "name": "implicit",
        "recipe": "x",
        "secrets": [{"local_name": "only-key", "kind": "env", "value": "implicit-v4l"}],
        "tools": [{
            "name": "implicit",
            "base_url": "https://api.example.com",
            "bindings": [{"secret": "only-key", "name": "Authorization", "format": "Bearer {secret}"}],
            "cli": {"bin": "sh", "inject": [{"via": "env", "name": "ONLY_KEY"}], "enabled": True},
        }],
    }
    r = await clients.post("/skills", json=payload)
    assert r.status_code == 200, r.text
    result = await _run("implicit", ["-c", "echo got=${ONLY_KEY:+yes}"])
    assert "got=yes" in result.stdout


async def test_run_scrubs_server_env(clients: AsyncClient, monkeypatch):
    # A var set on the treg process must NOT reach the child (the env is built fresh, not copied).
    monkeypatch.setenv("SERVER_ONLY_ENV", "leaky")
    await _register_runnable(clients)
    result = await _run("sh-skill", ["-c", "echo v=${SERVER_ONLY_ENV:-absent}"])
    assert result.stdout.strip() == "v=absent"


async def test_run_exit_code_propagates(clients: AsyncClient):
    await _register_runnable(clients)
    result = await _run("sh-skill", ["-c", "exit 3"])
    assert result.exit_code == 3


async def test_run_plain_http_tool_rejected(clients: AsyncClient):
    # an ordinary HTTP tool (no cli profile at all) can never be executed
    r = await clients.post("/tools", json={"name": "plainhttp", "base_url": "https://api.example.com"})
    assert r.status_code == 200, r.text
    with pytest.raises(runner.RunError, match="no CLI profile"):
        await _run("plainhttp", ["-c", "echo hi"])


async def test_run_missing_cli_rejected(clients: AsyncClient):
    await _register_runnable(clients, name="ghost", bin="treg-nonexistent-bin-xyz")
    with pytest.raises(runner.RunError, match="not installed"):
        await _run("ghost", [])


async def test_run_timeout(clients: AsyncClient):
    await _register_runnable(clients)
    result = await _run("sh-skill", ["-c", "sleep 5"], timeout_s=1)
    assert result.timed_out is True
    assert result.exit_code != 0


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
