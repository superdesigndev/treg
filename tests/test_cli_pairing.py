"""CLI pairing across independent workers, transactions and actual interpreter restarts.

Run on SQLite and in the PostgreSQL CI job. No provider traffic or production credentials.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from contextlib import AsyncExitStack
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, ConnectError
from sqlalchemy import event, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from treg import crypto
from treg.api import app
from treg.application import auth
from treg.config import get_settings
from treg.domain.identity import session as sess
from treg.infra import db as database
from treg.infra import turnstile
from treg.models import ApiKey, Ephemeral, Membership, Org, User
from treg.routers import auth as auth_routes
from treg.timeutil import utcnow_naive


@pytest.fixture
async def workers(monkeypatch):
    monkeypatch.setenv("TREG_SECRET_KEY", crypto.new_key())
    monkeypatch.setenv("TREG_SESSION_SECRET", crypto.new_token())
    monkeypatch.setenv("TREG_GITHUB_CLIENT_ID", "synthetic-client")
    monkeypatch.setenv("TREG_GOOGLE_CLIENT_ID", "synthetic-client")
    get_settings.cache_clear()
    await database.reset_db()
    async with database.session_maker() as db:
        user = User(email="pairing@example.test")
        org = Org(name="Pairing test", slug="pairing-test")
        db.add_all([user, org])
        await db.flush()
        db.add(Membership(user_id=user.id, org_id=org.id, role="owner", token_hash=""))
        await db.commit()
    modules, engines = [], []
    # Separate module globals AND independent connection pools; sharing a Python lock or dict
    # cannot make these workers agree. The subprocess test below also crosses interpreters.
    for index in range(3):
        name = f"treg.application._pairing_test_worker_{index}"
        spec = importlib.util.spec_from_file_location(name, auth.__file__)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        engine = create_async_engine(get_settings().database_url)
        module.database = SimpleNamespace(session_maker=async_sessionmaker(engine, expire_on_commit=False))
        modules.append(module)
        engines.append(engine)
    try:
        yield SimpleNamespace(
            modules=modules, engines=engines, cookie=sess.make_session(user.id), user_id=user.id,
            org=org.slug,
        )
    finally:
        for engine in engines:
            await engine.dispose()
        get_settings.cache_clear()


@pytest.fixture
async def worker_clients(workers, monkeypatch):
    async with AsyncExitStack() as stack:
        clients = []
        for index, worker in enumerate(workers.modules):
            name = f"treg.routers._pairing_test_worker_{index}"
            spec = importlib.util.spec_from_file_location(name, auth_routes.__file__)
            router = importlib.util.module_from_spec(spec)
            monkeypatch.setitem(sys.modules, name, router)
            spec.loader.exec_module(router)
            router.auth_use_cases = worker
            server = FastAPI()
            server.include_router(router.email_router)
            server.include_router(router.cli_router)
            clients.append(await stack.enter_async_context(AsyncClient(
                transport=ASGITransport(app=server, client=("203.0.113.10", 1234)),
                base_url="http://registry", headers={"Origin": "http://registry"},
            )))
        yield clients


async def _approve(worker, context, started, org=None):
    return await worker.approve_cli_login(
        context.cookie, started["login_id"], started["code"], org,
    )


async def _row(started, ns=auth.CLI_LOGIN_NS):
    async with database.session_maker() as db:
        return await db.get(Ephemeral, (ns, crypto.hash_token(started["login_id"])))


async def _expire(key, ns=auth.CLI_LOGIN_NS):
    async with database.session_maker() as db:
        await db.execute(update(Ephemeral).where(
            Ephemeral.ns == ns, Ephemeral.k == crypto.hash_token(key),
        ).values(expires_at=utcnow_naive() - timedelta(seconds=1)))
        await db.commit()


async def test_independent_workers_preserve_contract_and_encrypt_storage(workers):
    a, b, c = workers.modules
    started = await a.start_cli_login()
    pending = await _row(started)
    assert pending.k != started["login_id"]
    assert crypto.decrypt(pending.v["code"]) == started["code"]
    assert pending.v["code"] != started["code"]
    assert await c.poll_cli_login(started["login_id"]) == {"status": "pending"}
    assert (await _row(started)).expires_at == pending.expires_at
    # An invalid team must leave the code usable and the attempt budget unchanged.
    with pytest.raises(b.CliPairingError, match="not_member"):
        await _approve(b, workers, started, "foreign-team")
    assert (await _row(started)).v == pending.v
    normalized = {**started, "code": " - ".join(started["code"].lower())}
    approved = await _approve(b, workers, normalized, workers.org)
    assert approved == {"ok": True, "email": "pairing@example.test", "active_org": workers.org}
    complete = await _row(started)
    result = await c.poll_cli_login(started["login_id"])
    assert json.loads(crypto.decrypt(complete.v["result"])) == result
    assert result["token"] not in json.dumps(complete.v)
    claims = sess.read_identity_claims(result["token"])
    assert claims["uid"] == workers.user_id and claims["org"] == workers.org
    assert claims["scope"] == sess.TEAM_SCOPE and "exp" not in claims
    assert await a.poll_cli_login(started["login_id"]) == {"status": "pending"}
    with pytest.raises(b.CliPairingError, match="expired"):
        await _approve(b, workers, started, workers.org)


async def test_concurrent_approvals_and_polls_have_one_winner(workers):
    a, b, c = workers.modules
    started = await a.start_cli_login()
    approvals = await asyncio.gather(*(
        _approve(worker, workers, started, workers.org)
        for worker in workers.modules * 3
    ), return_exceptions=True)
    assert sum(isinstance(result, dict) for result in approvals) == 1
    assert all(isinstance(result, dict) or getattr(result, "kind", None) == "expired"
               for result in approvals)
    results = await asyncio.gather(*(
        worker.poll_cli_login(started["login_id"]) for worker in workers.modules * 3
    ))
    assert sum("token" in result for result in results) == 1
    assert sum(result == {"status": "pending"} for result in results) == 8


async def test_concurrent_wrong_codes_share_one_attempt_budget(workers):
    started = await workers.modules[0].start_cli_login()
    outcomes = await asyncio.gather(*(
        worker.approve_cli_login(workers.cookie, started["login_id"], "0000", None)
        for worker in workers.modules * 4
    ), return_exceptions=True)
    kinds = [getattr(outcome, "kind", None) for outcome in outcomes]
    assert kinds.count("wrong_code") == auth.CLI_APPROVE_MAX_TRIES - 1
    assert kinds.count("too_many_wrong_codes") == 1
    assert kinds.count("expired") == len(kinds) - auth.CLI_APPROVE_MAX_TRIES
    with pytest.raises(workers.modules[1].CliPairingError, match="expired"):
        await _approve(workers.modules[1], workers, started)
    assert await workers.modules[2].poll_cli_login(started["login_id"]) == {"status": "pending"}


async def test_last_wrong_code_races_valid_approval(workers):
    a, b, c = workers.modules
    started = await a.start_cli_login()
    async with database.session_maker() as db:
        row = await db.get(Ephemeral, (auth.CLI_LOGIN_NS, crypto.hash_token(started["login_id"])))
        row.v = {**row.v, "attempts": 1}
        await db.commit()
    wrong, right = await asyncio.gather(
        a.approve_cli_login(workers.cookie, started["login_id"], "0000", None),
        _approve(b, workers, started), return_exceptions=True,
    )
    result = await c.poll_cli_login(started["login_id"])
    if isinstance(right, dict):
        assert wrong.kind == "expired" and "token" in result
    else:
        assert wrong.kind == "too_many_wrong_codes" and right.kind == "expired"
        assert result == {"status": "pending"}


@pytest.mark.parametrize("completed", [False, True])
async def test_expiry_is_enforced_without_another_start(workers, completed):
    a, b, c = workers.modules
    started = await a.start_cli_login()
    if completed:
        await _approve(b, workers, started)
    await _expire(started["login_id"])
    with pytest.raises(b.CliPairingError, match="expired"):
        await _approve(b, workers, started)
    assert await c.poll_cli_login(started["login_id"]) == {"status": "pending"}
    assert await _row(started) is None


async def test_wrong_attempt_does_not_extend_ttl(workers):
    a, b, c = workers.modules
    started = await a.start_cli_login()
    before = await _row(started)
    with pytest.raises(b.CliPairingError, match="wrong_code"):
        await b.approve_cli_login(workers.cookie, started["login_id"], "0000", None)
    after = await _row(started)
    assert after.expires_at == before.expires_at
    assert after.v["attempts"] == before.v["attempts"] - 1


async def test_expiry_during_org_lookup_rolls_back_default_key(workers, monkeypatch):
    a, b, c = workers.modules
    started = await a.start_cli_login()
    real_ensure = b.managed_keys.ensure_default_key

    async def expire_after_key(*args):
        result = await real_ensure(*args)
        monkeypatch.setattr(b, "_utcnow_naive", lambda: utcnow_naive() + timedelta(seconds=601))
        return result

    monkeypatch.setattr(b.managed_keys, "ensure_default_key", expire_after_key)
    with pytest.raises(b.CliPairingError, match="expired"):
        await _approve(b, workers, started, workers.org)
    async with database.session_maker() as db:
        assert (await db.execute(select(ApiKey))).scalars().all() == []
    assert await c.poll_cli_login(started["login_id"]) == {"status": "pending"}


@pytest.mark.parametrize("operation", ["approve", "poll"])
async def test_cancellation_before_commit_rolls_back(workers, monkeypatch, operation):
    a, b, c = workers.modules
    started = await a.start_cli_login()
    if operation == "poll":
        await _approve(a, workers, started)
    at_commit = asyncio.Event()

    class PausedCommit(AsyncSession):
        async def commit(self):
            await self.flush()
            at_commit.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(b.database, "session_maker", async_sessionmaker(
        workers.engines[1], class_=PausedCommit, expire_on_commit=False,
    ))
    task = asyncio.create_task(
        _approve(b, workers, started, workers.org) if operation == "approve"
        else b.poll_cli_login(started["login_id"]),
    )
    try:
        await asyncio.wait_for(at_commit.wait(), timeout=5)
        # Poll can either observe pending or wait for approval; neither path may steal the code.
        competing_poll = asyncio.create_task(c.poll_cli_login(started["login_id"]))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        observed = await asyncio.wait_for(competing_poll, timeout=5)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    if operation == "approve":
        assert observed == {"status": "pending"}
        async with database.session_maker() as db:
            assert (await db.execute(select(ApiKey))).scalars().all() == []
        await _approve(a, workers, started, workers.org)
        assert "token" in await c.poll_cli_login(started["login_id"])
    else:
        assert "token" in observed
    assert await a.poll_cli_login(started["login_id"]) == {"status": "pending"}


@pytest.mark.parametrize("operation", ["approve", "poll"])
@pytest.mark.parametrize("committed", [False, True])
async def test_commit_failure_never_republishes_credentials(workers, monkeypatch, operation, committed):
    a, b, c = workers.modules
    started = await a.start_cli_login()
    if operation == "poll":
        await _approve(a, workers, started)

    class FailedCommit(AsyncSession):
        async def commit(self):
            if committed:
                await super().commit()
            else:
                await self.flush()
            raise OperationalError("synthetic commit failure", {}, Exception("lost acknowledgement"))

    monkeypatch.setattr(b.database, "session_maker", async_sessionmaker(
        workers.engines[1], class_=FailedCommit, expire_on_commit=False,
    ))
    with pytest.raises(b.CliPairingError, match="unavailable"):
        if operation == "approve":
            await _approve(b, workers, started, workers.org)
        else:
            await b.poll_cli_login(started["login_id"])
    if operation == "approve" and not committed:
        await _approve(a, workers, started, workers.org)
    result = await c.poll_cli_login(started["login_id"])
    assert ("token" in result) is not (operation == "poll" and committed)
    assert await a.poll_cli_login(started["login_id"]) == {"status": "pending"}


@pytest.mark.parametrize("provider", ["github", "google"])
async def test_social_return_mapping_is_shared_expiring_and_single_use(workers, provider):
    a, b, c = workers.modules
    started = await a.start_cli_login()
    social = await getattr(a, f"start_{provider}_login")(started["login_id"], lambda: "http://registry")
    assert started["code"] not in social.url
    mappings = await asyncio.gather(b._consume_cli_state(social.state), c._consume_cli_state(social.state))
    assert sum(mapping is not None for mapping in mappings) == 1
    assert next(mapping for mapping in mappings if mapping)[0] == started["login_id"]
    assert await c.poll_cli_login(started["login_id"]) == {"status": "pending"}
    expired = await getattr(a, f"start_{provider}_login")(started["login_id"], lambda: "http://registry")
    await _expire(expired.state, auth.CLI_OAUTH_NS)
    assert await b._consume_cli_state(expired.state) is None


async def test_storage_failure_is_503_without_secret_logs_or_local_fallback(workers, monkeypatch, caplog):
    def unavailable():
        raise OperationalError("synthetic", {"code": "private-test-value"}, Exception("storage down"))

    monkeypatch.setattr(auth, "database", SimpleNamespace(session_maker=unavailable))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as client:
        client.cookies.set(sess.COOKIE, workers.cookie)
        responses = [
            await client.post("/auth/cli/start"),
            await client.get("/auth/cli/poll", params={"login_id": "synthetic-login"}),
            await client.post("/auth/cli/approve", json={"login_id": "synthetic-login", "code": "0000"}),
            await client.get("/auth/github", params={"cli": "synthetic-login"}),
            await client.get("/auth/google", params={"cli": "synthetic-login"}),
        ]
    assert all(response.status_code == 503 for response in responses)
    assert all(response.headers["cache-control"] == "no-store" for response in responses)
    assert "private-test-value" not in caplog.text
    assert all("private-test-value" not in response.text for response in responses)


async def test_process_restart_between_start_approve_and_poll(workers):
    script = """
import asyncio, json, sys
from treg.application import auth
from treg.infra import db
async def run():
    operation, args = json.loads(sys.stdin.read())
    try:
        result = await getattr(auth, operation)(*args)
        print(json.dumps(result))
    finally:
        await db.dispose_engine()
asyncio.run(run())
"""
    # Deliberately construct the environment: no developer config, provider keys or analytics.
    settings = get_settings()
    env = {
        "PATH": os.environ["PATH"], "TREG_DATABASE_URL": settings.database_url,
        "TREG_SECRET_KEY": settings.secret_key, "TREG_SESSION_SECRET": settings.session_secret,
        "TREG_TELEMETRY": "0", "TREG_KV_URL": "", "TREG_READ_DATABASE_URL": "",
    }

    async def fresh_process(operation, *args):
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", script, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(json.dumps([operation, args]).encode()), timeout=30,
        )
        assert process.returncode == 0, stderr.decode()
        return json.loads(stdout)

    started = await fresh_process("start_cli_login")
    await fresh_process("approve_cli_login", workers.cookie, started["login_id"], started["code"], workers.org)
    result = await fresh_process("poll_cli_login", started["login_id"])
    assert sess.read_identity_claims(result["token"])["org"] == workers.org
    assert await fresh_process("poll_cli_login", started["login_id"]) == {"status": "pending"}


@pytest.mark.parametrize("login_id", ["", "no", "a" * 129, "valid-id\n", "<script>alert(1)</script>"])
async def test_invalid_poll_never_opens_a_store(workers, worker_clients, monkeypatch, login_id):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid poll must not access the DB")

    for worker in workers.modules:
        monkeypatch.setattr(worker.database, "session_maker", forbidden)
    response = await worker_clients[0].get("/auth/cli/poll", params={"login_id": login_id})
    assert response.status_code == 200 and response.json() == {"status": "pending"}
    assert response.headers["cache-control"] == "no-store"


async def test_pending_and_unknown_polls_are_read_only(workers):
    started = await workers.modules[0].start_cli_login()
    statements, commits = [], []
    engine = workers.engines[1].sync_engine
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().split()[0].upper())
    def commit(conn):
        commits.append(True)
    event.listen(engine, "before_cursor_execute", capture)
    event.listen(engine, "commit", commit)
    try:
        for login_id in (started["login_id"], "synthetic-unknown"):
            assert await workers.modules[1].poll_cli_login(login_id) == {"status": "pending"}
        assert statements == ["SELECT", "SELECT"] and commits == []
    finally:
        event.remove(engine, "before_cursor_execute", capture)
        event.remove(engine, "commit", commit)


@pytest.mark.parametrize("nested_loop", [False, True])
async def test_cleanup_is_bounded_and_leaves_live_rows_and_other_namespaces(workers, monkeypatch, nested_loop):
    if nested_loop:
        if workers.engines[0].dialect.name != "postgresql":
            pytest.skip("PostgreSQL query planner")
        class NestedLoopSession(AsyncSession):
            async def __aenter__(self):
                await super().__aenter__()
                await self.execute(text("SET LOCAL enable_hashjoin = off"))
                await self.execute(text("SET LOCAL enable_mergejoin = off"))
                return self
        monkeypatch.setattr(workers.modules[0].database, "session_maker", async_sessionmaker(
            workers.engines[0], class_=NestedLoopSession, expire_on_commit=False,
        ))
    old = utcnow_naive() - timedelta(seconds=100)
    async with database.session_maker() as db:
        for i in range(130):
            ns = (auth.CLI_LOGIN_NS, auth.CLI_OAUTH_NS)[i % 2]
            db.add(Ephemeral(ns=ns, k=str(i), v={}, expires_at=old))
        db.add(Ephemeral(ns=auth.OTP_NS, k="unrelated", v={}, expires_at=old))
        db.add(Ephemeral(ns=auth.CLI_LOGIN_NS, k="live", v={},
                         expires_at=utcnow_naive() + timedelta(seconds=600)))
        await db.commit()
    await workers.modules[0].start_cli_login()
    async with database.session_maker() as db:
        rows = (await db.execute(select(Ephemeral).where(Ephemeral.expires_at <= old))).scalars().all()
    assert len(rows) == 31 and any(r.ns == auth.OTP_NS for r in rows)
    # Another start can immediately clean the remaining batch; there is no sweep budget.
    await workers.modules[2].start_cli_login()
    async with database.session_maker() as db:
        rows = (await db.execute(select(Ephemeral).where(Ephemeral.expires_at <= old))).scalars().all()
        assert await db.get(Ephemeral, (auth.CLI_LOGIN_NS, "live")) is not None
    assert [(r.ns, r.k) for r in rows] == [(auth.OTP_NS, "unrelated")]


async def test_postgres_cleanup_skips_locked_expired_handshakes(workers):
    if workers.engines[0].dialect.name != "postgresql":
        pytest.skip("PostgreSQL SKIP LOCKED")
    old = utcnow_naive() - timedelta(seconds=1)
    async with database.session_maker() as db:
        db.add_all([Ephemeral(ns=auth.CLI_LOGIN_NS, k=key, v={}, expires_at=old)
                    for key in ("locked", "free")])
        await db.commit()
    async with workers.modules[0].database.session_maker() as blocker:
        await blocker.execute(update(Ephemeral).where(
            Ephemeral.ns == auth.CLI_LOGIN_NS, Ephemeral.k == "locked",
        ).values(k=Ephemeral.k))
        await asyncio.wait_for(workers.modules[1]._prune_handshakes(), 1)
        async with database.session_maker() as db:
            assert await db.get(Ephemeral, (auth.CLI_LOGIN_NS, "free")) is None
            assert await db.get(Ephemeral, (auth.CLI_LOGIN_NS, "locked")) is not None


async def test_cleanup_commits_separately_from_new_handshake(workers, monkeypatch):
    old = utcnow_naive() - timedelta(seconds=1)
    async with database.session_maker() as db:
        db.add(Ephemeral(ns=auth.CLI_LOGIN_NS, k="expired", v={}, expires_at=old))
        await db.commit()
    class FailedInsert(AsyncSession):
        async def commit(self):
            if self.new:
                raise OperationalError("synthetic", {}, Exception("insert failed"))
            await super().commit()
    worker = workers.modules[0]
    monkeypatch.setattr(worker.database, "session_maker", async_sessionmaker(
        workers.engines[0], class_=FailedInsert, expire_on_commit=False,
    ))
    with pytest.raises(worker.CliPairingError, match="unavailable"):
        await worker.start_cli_login()
    async with database.session_maker() as db:
        assert await db.get(Ephemeral, (auth.CLI_LOGIN_NS, "expired")) is None
        assert (await db.execute(select(Ephemeral).where(Ephemeral.ns == auth.CLI_LOGIN_NS))).first() is None


@pytest.mark.parametrize("verification", ["pass", "secret_rejected", "unavailable"])
async def test_turnstile_email_browser_pairing_across_workers(
    workers, worker_clients, monkeypatch, caplog, verification,
):
    settings = get_settings()
    monkeypatch.setattr(settings, "turnstile_site_key", "synthetic-site")
    monkeypatch.setattr(settings, "turnstile_secret", "synthetic-secret")
    monkeypatch.setattr(settings, "turnstile_hostnames", "registry")
    monkeypatch.setattr(settings, "turnstile_mode", "enforce")
    forms, delivered = [], []
    async def siteverify(form):
        forms.append(form)
        if verification == "unavailable":
            raise ConnectError("synthetic transport failure")
        if verification == "secret_rejected":
            return {"success": False, "error-codes": ["invalid-input-secret"]}
        return {"success": True, "action": "email_start", "hostname": "registry"}
    async def capture_otp(email, code, **kwargs):
        delivered.append(code)
    monkeypatch.setattr(turnstile, "siteverify", siteverify)
    monkeypatch.setattr(auth.email_sender, "send_otp", capture_otp)
    caplog.set_level("INFO", logger="treg")
    a, b, c = worker_clients
    started = (await a.post("/auth/cli/start")).json()
    email = " Pair.Ing+flow@GMail.com. "
    refused = await b.post("/auth/email/start", json={"email": email}, headers={"X-Treg-Key-Protocol": "1"})
    assert refused.status_code == 400 and refused.headers["x-treg-captcha"] == "required"
    assert "without --email" in refused.text
    page = await c.get("/login", params={"cli": started["login_id"]})
    assert page.status_code == 200 and 'src="/captcha.js"' in page.text
    response = await b.post("/auth/email/start", json={"email": email, "captcha_token": "synthetic-widget-token"})
    assert response.status_code == 200
    otp = response.json().get("dev_code") or delivered[0]
    verified = await c.post("/auth/email/verify", json={"email": email, "code": otp})
    assert verified.status_code == 200
    a.cookies.set(sess.COOKIE, c.cookies.get(sess.COOKIE))
    wrong = await a.post("/auth/cli/approve", json={**started, "code": "0000"})
    assert wrong.status_code == 400
    assert (await a.post("/auth/cli/approve", json=started)).status_code == 200
    result = (await b.get("/auth/cli/poll", params={"login_id": started["login_id"]})).json()
    assert result["email"] == "pair.ing+flow@gmail.com"
    claims = sess.read_identity_claims(result["token"])
    assert claims["scope"] == sess.BOOTSTRAP_SCOPE
    assert (await c.get("/auth/cli/poll", params={"login_id": started["login_id"]})).json() == {"status": "pending"}
    async with database.session_maker() as db:
        user = await db.get(User, claims["uid"])
        assert user.signup_promo_available is (verification == "pass")
    assert len(forms) == 1
    assert "synthetic-widget-token" not in caplog.text and "synthetic-secret" not in caplog.text
