"""`treg login` outside a terminal: the CLI prints the link, waits briefly, and exits 3 while the
sign-in is pending; `treg login` again or a later command collects the token. Driven through `cli.main` against the real app on a loopback socket and a real database."""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import socket
import stat
import threading
import time
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import uvicorn
from fastapi import HTTPException
from sqlalchemy import update

from treg import cli, crypto
from treg.api import app
from treg.application import auth
from treg.config import get_settings
from treg.domain.identity import session as sess
from treg.infra import db as database
from treg.models import Ephemeral, Membership, Org, User
from treg.routers import auth as auth_routes
from treg.timeutil import utcnow_naive


@pytest.fixture
async def registry(monkeypatch, tmp_path):
    monkeypatch.setenv("TREG_SECRET_KEY", crypto.new_key())
    monkeypatch.setenv("TREG_SESSION_SECRET", crypto.new_token())
    monkeypatch.setenv("TREG_TELEMETRY", "0")
    for name in ("TREG_URL", "TREG_TOKEN", "TREG_ORG"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    await database.reset_db()
    async with database.session_maker() as db:
        user = User(email="pending@example.test")
        org = Org(name="Pending test", slug="pending-test")
        db.add_all([user, org])
        await db.flush()
        db.add(Membership(user_id=user.id, org_id=org.id, role="owner", token_hash=""))
        await db.commit()
    paths: list[str] = []

    async def recorded(scope, receive, send):
        if scope["type"] == "http":
            paths.append(scope["path"])
        await app(scope, receive, send)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(recorded, lifespan="off", log_config=None,
                                           access_log=False, ws="none"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: True)
    monkeypatch.setattr(cli, "AGENT_LOGIN_GRACE_SECONDS", 0)
    cli.CONFIG_PATH.write_text(json.dumps({"base_url": base}), encoding="utf-8")
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(.01)
        async with httpx.AsyncClient(base_url=base, trust_env=False, headers={"Origin": base},
                                     cookies={sess.COOKIE: sess.make_session(user.id)}) as browser:
            yield type("Registry", (), {"base": base, "paths": paths, "browser": browser,
                                        "org": org.slug})
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        get_settings.cache_clear()


def _stored() -> dict:
    return json.loads(cli.CONFIG_PATH.read_text(encoding="utf-8"))


def _pending_file():
    return cli.CONFIG_PATH.parent / "pending-login.json"


def _pending() -> dict | None:
    path = _pending_file()
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


async def _approve(registry, pending: dict) -> None:
    response = await registry.browser.post("/auth/cli/approve", json={
        "login_id": pending["login_id"], "code": pending["code"], "org": registry.org,
    })
    assert response.status_code == 200, response.text


async def _login_left_pending(capsys) -> dict:
    with pytest.raises(SystemExit) as left:
        await asyncio.to_thread(cli.main, ["login"])
    assert left.value.code == 3
    out = capsys.readouterr().out
    pending = _pending()
    assert pending["url"] in out and pending["code"] in out
    assert "run `treg login` again to finish, or `treg login --wait`" in out
    return pending


async def _until_polled(registry, count: int, task) -> None:
    async with asyncio.timeout(10):
        while registry.paths.count("/auth/cli/poll") < count:
            assert not task.done(), "login returned before the approval"
            await asyncio.sleep(.05)


async def test_agent_login_returns_and_the_next_command_collects_the_token(registry, capsys):
    pending = await _login_left_pending(capsys)
    assert stat.S_IMODE(_pending_file().stat().st_mode) == 0o600
    assert "pending_login" not in _stored() and _stored().get("token") is None
    login_id = parse_qs(urlsplit(pending["url"]).query)["cli"][0]
    assert login_id == pending["login_id"] and pending["verifier"] not in pending["url"]

    await _approve(registry, pending)
    link_holder = await registry.browser.get("/auth/cli/poll", params={"login_id": login_id})
    assert link_holder.json() == {"status": "pending"}

    await asyncio.to_thread(cli.main, ["whoami"])
    captured = capsys.readouterr()
    assert "✓ Logged in as pending@example.test" in captured.err
    assert "pending@example.test" in captured.out and "✓" not in captured.out
    assert _pending() is None and _stored()["active_org"] == registry.org


async def test_second_login_while_pending_reprints_the_same_link(registry, capsys):
    pending = await _login_left_pending(capsys)
    with pytest.raises(SystemExit):
        await asyncio.to_thread(cli.main, ["whoami"])
    capsys.readouterr()

    assert await _login_left_pending(capsys) == pending
    assert registry.paths.count("/auth/cli/start") == 1

    await _approve(registry, pending)
    await asyncio.to_thread(cli.main, ["whoami"])
    assert "✓ Logged in as pending@example.test" in capsys.readouterr().err
    assert _stored()["token"] and _pending() is None


async def test_a_signed_in_user_finishes_with_login_and_other_commands_never_poll(registry, capsys):
    pending = await _login_left_pending(capsys)
    polls = registry.paths.count("/auth/cli/poll")
    cli._save_config({**_stored(), "token": "synthetic-earlier-token", "identity": True})
    with contextlib.suppress(SystemExit):
        await asyncio.to_thread(cli.main, ["whoami"])
    assert registry.paths.count("/auth/cli/poll") == polls and _pending() == pending

    await _approve(registry, pending)
    await asyncio.to_thread(cli.main, ["login"])
    assert "✓ Logged in as pending@example.test" in capsys.readouterr().out
    assert _stored()["token"] != "synthetic-earlier-token" and _pending() is None


async def test_a_process_with_treg_token_leaves_the_sign_in_alone(registry, capsys, monkeypatch):
    pending = await _login_left_pending(capsys)
    polls = registry.paths.count("/auth/cli/poll")
    await _approve(registry, pending)
    monkeypatch.setenv("TREG_TOKEN", "synthetic-agent-token")
    with contextlib.suppress(SystemExit):
        await asyncio.to_thread(cli.main, ["whoami"])
    assert registry.paths.count("/auth/cli/poll") == polls
    assert _pending() == pending and _stored().get("token") is None


async def test_the_last_minute_is_singular(registry, capsys):
    pending = await _login_left_pending(capsys)
    cli._write_private_json(_pending_file(), {**pending, "expires_at": int(time.time()) + 30})
    with pytest.raises(SystemExit):
        await asyncio.to_thread(cli.main, ["login"])
    assert "The link expires in 1 minute." in capsys.readouterr().out


async def test_unfinished_sign_in_is_named_on_stderr_and_kept(registry, capsys):
    pending = await _login_left_pending(capsys)
    with pytest.raises(SystemExit):
        await asyncio.to_thread(cli.main, ["whoami"])
    assert pending["url"] in capsys.readouterr().err
    assert _pending() == pending


async def test_expired_sign_in_is_dropped(registry, capsys):
    pending = await _login_left_pending(capsys)
    async with database.session_maker() as db:
        await db.execute(update(Ephemeral).where(
            Ephemeral.ns == auth.CLI_LOGIN_NS, Ephemeral.k == crypto.hash_token(pending["login_id"]),
        ).values(expires_at=utcnow_naive() - timedelta(seconds=1)))
        await db.commit()
    with pytest.raises(SystemExit):
        await asyncio.to_thread(cli.main, ["whoami"])
    assert "expired" in capsys.readouterr().err
    assert _pending() is None


async def test_approval_after_the_local_expiry_is_still_collected(registry, capsys):
    pending = await _login_left_pending(capsys)
    await _approve(registry, pending)
    cli._write_private_json(_pending_file(), {**pending, "expires_at": int(time.time()) - 60})

    await asyncio.to_thread(cli.main, ["whoami"])
    assert "✓ Logged in as pending@example.test" in capsys.readouterr().err
    assert _stored()["token"] and _pending() is None


async def test_sign_in_past_the_longest_approval_window_is_dropped(registry, capsys):
    pending = await _login_left_pending(capsys)
    polls = registry.paths.count("/auth/cli/poll")
    past = int(time.time()) - pending["expires_in"] - 1
    cli._write_private_json(_pending_file(), {**pending, "expires_at": past})

    with pytest.raises(SystemExit):
        await asyncio.to_thread(cli.main, ["whoami"])
    assert "expired" in capsys.readouterr().err
    assert registry.paths.count("/auth/cli/poll") == polls + 1 and _pending() is None


async def test_expired_claim_keeps_a_token_another_process_saved(registry, capsys):
    pending = await _login_left_pending(capsys)
    stale = cli._load_config()
    await _approve(registry, pending)
    other = await registry.browser.get("/auth/cli/poll", params={"login_id": pending["login_id"]},
                                       headers={"X-Treg-Poll-Verifier": pending["verifier"]})
    cli._save_config({**_stored(), "token": other.json()["token"], "identity": True})

    assert await asyncio.to_thread(cli._claim_pending_login, stale, pending, wait_until=0) == "logged_in"
    assert _stored()["token"] == other.json()["token"] and _pending() is None
    assert stale["token"] == other.json()["token"]


async def test_a_token_saved_just_after_an_expired_answer_is_adopted(registry, capsys, monkeypatch):
    pending = await _login_left_pending(capsys)
    await _approve(registry, pending)
    other = await registry.browser.get("/auth/cli/poll", params={"login_id": pending["login_id"]},
                                       headers={"X-Treg-Poll-Verifier": pending["verifier"]})
    token = other.json()["token"]
    real_poll = cli._poll_login

    def poll(*args, **kwargs):
        answer = real_poll(*args, **kwargs)
        assert answer == {"status": "expired"}
        threading.Timer(.5, cli._save_config, [{**_stored(), "token": token, "identity": True}]).start()
        return answer

    monkeypatch.setattr(cli, "_poll_login", poll)
    await asyncio.to_thread(cli.main, ["whoami"])
    captured = capsys.readouterr()
    assert "expired" not in captured.err and "pending@example.test" in captured.out
    assert _stored()["token"] == token and _pending() is None


async def test_expired_handling_between_a_claim_and_its_save_keeps_the_token(registry, capsys, monkeypatch):
    pending = await _login_left_pending(capsys)
    await _approve(registry, pending)
    role = contextvars.ContextVar("role", default="")
    a_claimed, b_polled, b_loaded, a_done = (threading.Event() for _ in range(4))
    real_save, real_load, real_poll = cli._save_config, cli._load_config, cli._poll_login

    def save(cfg):
        if role.get() == "A" and cfg.get("token") and not a_claimed.is_set():
            a_claimed.set()
            assert b_loaded.wait(10), "B never reloaded the config"
        real_save(cfg)

    def poll(*args, **kwargs):
        answer = real_poll(*args, **kwargs)
        if role.get() == "B":
            assert answer == {"status": "expired"}
            b_polled.set()
        return answer

    def load():
        cfg = real_load()
        if role.get() == "B" and b_polled.is_set() and not b_loaded.is_set():
            b_loaded.set()
            assert a_done.wait(10), "A never finished"
        return cfg

    monkeypatch.setattr(cli, "_save_config", save)
    monkeypatch.setattr(cli, "_poll_login", poll)
    monkeypatch.setattr(cli, "_load_config", load)

    def run(name: str) -> None:
        role.set(name)
        try:
            with contextlib.suppress(SystemExit):
                cli.main(["whoami"])
        finally:
            if name == "A":
                a_done.set()

    first = asyncio.create_task(asyncio.to_thread(run, "A"))
    assert await asyncio.to_thread(a_claimed.wait, 10)
    second = asyncio.create_task(asyncio.to_thread(run, "B"))
    await asyncio.wait_for(asyncio.gather(first, second), 20)
    assert b_loaded.is_set()
    assert _stored()["token"] and _pending() is None


async def test_wait_resumes_the_saved_sign_in(registry, capsys, monkeypatch):
    pending = await _login_left_pending(capsys)
    opened: list[str] = []
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)
    polls = registry.paths.count("/auth/cli/poll")
    waiting = asyncio.create_task(asyncio.to_thread(cli.main, ["login", "--wait"]))
    await _until_polled(registry, polls + 3, waiting)
    await _approve(registry, pending)
    await asyncio.wait_for(waiting, 10)
    assert opened == [pending["url"]]
    assert "✓ Logged in as pending@example.test" in capsys.readouterr().out
    assert registry.paths.count("/auth/cli/start") == 1
    assert _stored()["token"] and _pending() is None


async def test_approval_inside_the_grace_finishes_the_login(registry, capsys, monkeypatch):
    monkeypatch.setattr(cli, "AGENT_LOGIN_GRACE_SECONDS", 10)
    waiting = asyncio.create_task(asyncio.to_thread(cli.main, ["login"]))
    await _until_polled(registry, 2, waiting)
    pending = _pending()
    assert pending["url"] in capsys.readouterr().out
    await _approve(registry, pending)
    await asyncio.wait_for(waiting, 10)
    out = capsys.readouterr().out
    assert "✓ Logged in as pending@example.test" in out and "treg login --wait" not in out
    assert _stored()["token"] and _pending() is None


async def test_unapproved_login_exits_3_after_the_grace_and_keeps_the_sign_in(registry, capsys, monkeypatch):
    monkeypatch.setattr(cli, "AGENT_LOGIN_GRACE_SECONDS", 1)
    pending = await _login_left_pending(capsys)
    assert registry.paths.count("/auth/cli/poll") >= 2
    assert _pending() == pending and _stored().get("token") is None


async def test_resumed_login_waits_the_same_grace(registry, capsys, monkeypatch):
    pending = await _login_left_pending(capsys)
    polls = registry.paths.count("/auth/cli/poll")
    monkeypatch.setattr(cli, "AGENT_LOGIN_GRACE_SECONDS", 10)
    waiting = asyncio.create_task(asyncio.to_thread(cli.main, ["login"]))
    await _until_polled(registry, polls + 3, waiting)
    assert pending["url"] in capsys.readouterr().out
    await _approve(registry, pending)
    await asyncio.wait_for(waiting, 10)
    assert "✓ Logged in as pending@example.test" in capsys.readouterr().out
    assert registry.paths.count("/auth/cli/start") == 1
    assert _stored()["token"] and _pending() is None


async def test_server_without_poll_verifier_keeps_the_blocking_login(registry, capsys, monkeypatch):
    real_start = auth_routes.auth_use_cases.start_cli_login

    async def old_server(poll_challenge=None):
        started = await real_start()
        return {"login_id": started["login_id"], "code": started["code"]}

    monkeypatch.setattr(auth_routes.auth_use_cases, "start_cli_login", old_server)
    started = threading.Event()
    monkeypatch.setattr(cli.time, "sleep", lambda s: started.set() or threading.Event().wait(.02))
    waiting = asyncio.create_task(asyncio.to_thread(cli.main, ["login"]))
    await asyncio.to_thread(started.wait, 10)
    out = capsys.readouterr().out
    url = out.split("visit:\n  ", 1)[1].split("\n", 1)[0]
    login_id = parse_qs(urlsplit(url).query)["cli"][0]
    await _approve(registry, {"login_id": login_id, "code": urlsplit(url).fragment.removeprefix("code=")})
    await asyncio.wait_for(waiting, 10)
    assert "✓ Logged in as pending@example.test" in capsys.readouterr().out
    assert _stored()["token"] and _pending() is None


async def test_a_failed_start_names_the_failure_instead_of_waiting(registry, monkeypatch):
    async def unavailable(poll_challenge=None):
        raise HTTPException(503, "synthetic outage")

    monkeypatch.setattr(auth_routes.auth_use_cases, "start_cli_login", unavailable)
    with pytest.raises(SystemExit) as failed:
        await asyncio.to_thread(cli.main, ["login"])
    assert "503" in str(failed.value) and "Retry `treg login`" in str(failed.value)
    assert "/auth/cli/poll" not in registry.paths and _pending() is None


async def test_logout_forgets_the_saved_sign_in(registry, capsys):
    await _login_left_pending(capsys)
    await asyncio.to_thread(cli.main, ["logout"])
    assert _pending() is None
