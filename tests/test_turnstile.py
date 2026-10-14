"""Turnstile on the email code request: off, observe and enforce, through HTTP and real money."""
from unittest.mock import patch

import httpx
import pytest
from sqlmodel import select

from treg import email as email_sender
from treg.application import signup
from treg.config import get_settings
from treg.infra import turnstile
from treg.infra.db import session_maker
from treg.models import LedgerEntry, Org, User

SITE_KEY = "1x00000000000000000000AA"


@pytest.fixture
def widget(monkeypatch):
    """Enable Turnstile and stand in for siteverify; `answer` is what Cloudflare would say."""
    settings = get_settings()
    monkeypatch.setattr(settings, "turnstile_site_key", SITE_KEY)
    monkeypatch.setattr(settings, "turnstile_secret", "test-secret")
    monkeypatch.setattr(settings, "turnstile_hostnames", "treg.example, alias.example")
    state = {"answer": {"success": True, "action": "email_start", "hostname": "treg.example"},
             "forms": []}

    async def siteverify(form):
        state["forms"].append(form)
        if isinstance(state["answer"], Exception):
            raise state["answer"]
        return state["answer"]

    monkeypatch.setattr(turnstile, "siteverify", siteverify)
    return state


@pytest.fixture
def signups(monkeypatch):
    events = []
    monkeypatch.setattr(signup.analytics, "capture",
                        lambda who, event, properties=None, **_: events.append((event, properties)))
    return events


async def _start(client, email, token=""):
    delivered = {}

    async def receive(to, code, **_):
        delivered[to] = code

    with patch.object(email_sender, "send_otp", receive):
        response = await client.post("/auth/email/start", json={"email": email, "captcha_token": token},
                                     headers={"X-Forwarded-For": "203.0.113.7"})
    code = response.json().get("dev_code") or delivered.get(email) if response.status_code == 200 else None
    return response, code


async def _sign_up_and_fund(client, email, token=""):
    """Request a code, verify it, create a team; return (start response, team balance or None)."""
    response, code = await _start(client, email, token)
    if response.status_code != 200:
        return response, None
    previous = httpx.Cookies(client.cookies)
    try:
        proof = await client.post("/auth/email/verify", json={"email": email, "code": code})
        assert proof.status_code == 200, proof.text
        team = await client.post("/orgs", json={"name": email.split("@")[0]},
                                 headers={"X-Treg-Token": proof.json()["token"]})
        assert team.status_code == 200, team.text
    finally:
        client.cookies = previous
    async with session_maker() as db:
        org = await db.get(Org, team.json()["org_id"])
    return response, org.balance_micro


async def test_off_by_default_sends_no_widget_and_calls_nobody(clients, monkeypatch):
    called = []
    monkeypatch.setattr(turnstile, "siteverify", lambda form: called.append(form))
    script = await clients.get("/captcha.js")
    assert script.status_code == 200 and 'var SITE_KEY = "";' in script.text
    response, balance = await _sign_up_and_fund(clients, "off@example.org")
    assert response.status_code == 200
    assert balance == get_settings().promo_grant_micro
    assert called == []


async def test_captcha_script_carries_the_site_key_only_with_a_secret(clients, widget, monkeypatch):
    script = await clients.get("/captcha.js")
    assert f'var SITE_KEY = "{SITE_KEY}";' in script.text
    assert script.headers["content-type"].startswith("application/javascript")
    monkeypatch.setattr(get_settings(), "turnstile_secret", "")
    assert 'var SITE_KEY = "";' in (await clients.get("/captcha.js")).text


async def test_siteverify_gets_the_secret_token_and_client_ip(clients, widget):
    response, _ = await _start(clients, "form@example.org", "tok")
    assert response.status_code == 200
    assert widget["forms"] == [{"secret": "test-secret", "response": "tok", "remoteip": "203.0.113.7"}]


@pytest.mark.parametrize(("token", "answer", "outcome"), [
    ("", None, turnstile.MISSING),
    ("tok", {"success": False, "error-codes": ["invalid-input-response"]}, turnstile.INVALID),
    ("tok", {"success": False, "error-codes": ["timeout-or-duplicate"]}, turnstile.INVALID),
    ("tok", {"success": True, "action": "other", "hostname": "treg.example"}, turnstile.INVALID),
    ("tok", {"success": True, "action": "email_start", "hostname": "evil.example"}, turnstile.INVALID),
    ("x" * 2049, None, turnstile.INVALID),
    ("tok", {"success": False, "error-codes": ["internal-error"]}, turnstile.UNAVAILABLE),
    ("tok", httpx.ConnectTimeout("slow"), turnstile.UNAVAILABLE),
    ("tok", {"success": True, "action": "email_start", "hostname": "ALIAS.example"}, turnstile.PASS),
])
async def test_outcomes(widget, token, answer, outcome):
    if answer is not None:
        widget["answer"] = answer
    assert await turnstile.verify(token, action="email_start") == outcome


async def test_hostnames_default_to_the_public_url(monkeypatch):
    monkeypatch.setattr(get_settings(), "turnstile_hostnames", "")
    monkeypatch.setattr(get_settings(), "public_url", "https://treg.example/")
    assert turnstile.hostnames() == {"treg.example"}
    monkeypatch.setattr(get_settings(), "public_url", "https://treg.to")
    assert turnstile.hostnames() == {"treg.to", "treg.superdesign.dev"}


async def test_observe_changes_nothing_and_records_the_outcome(clients, widget, signups, monkeypatch):
    monkeypatch.setattr(get_settings(), "turnstile_mode", "observe")
    response, balance = await _sign_up_and_fund(clients, "observed@example.org")
    assert response.status_code == 200
    assert balance == get_settings().promo_grant_micro
    assert [props["captcha"] for event, props in signups if event == "signup_completed"] == ["missing"]


@pytest.mark.parametrize("answer", [None, {"success": False, "error-codes": ["timeout-or-duplicate"]}])
async def test_enforce_refuses_a_code_without_a_valid_token(clients, widget, monkeypatch, answer):
    monkeypatch.setattr(get_settings(), "turnstile_mode", "enforce")
    if answer:
        widget["answer"] = answer
    response, code = await _start(clients, "refused@example.org", "tok" if answer else "")
    assert response.status_code == 400
    assert "verify this browser" in response.json()["detail"]
    assert code is None
    async with session_maker() as db:
        assert (await db.execute(select(User).where(User.email == "refused@example.org"))).first() is None


async def test_enforce_pass_signs_up_with_credit(clients, widget, signups, monkeypatch):
    monkeypatch.setattr(get_settings(), "turnstile_mode", "enforce")
    response, balance = await _sign_up_and_fund(clients, "human@example.org", "tok")
    assert response.status_code == 200
    assert balance == get_settings().promo_grant_micro
    assert [props["captcha"] for event, props in signups if event == "signup_completed"] == ["pass"]


async def test_enforce_outage_signs_in_without_credit(clients, widget, monkeypatch):
    monkeypatch.setattr(get_settings(), "turnstile_mode", "enforce")
    widget["answer"] = httpx.ConnectError("down")
    response, balance = await _sign_up_and_fund(clients, "outage@example.org", "tok")
    assert response.status_code == 200
    assert balance == 0
    async with session_maker() as db:
        user = (await db.execute(select(User).where(User.email == "outage@example.org"))).scalar_one()
        assert not user.signup_promo_available
        grants = (await db.execute(select(LedgerEntry).where(LedgerEntry.kind == "grant"))).scalars().all()
        assert all(entry.meta.get("user_id") != user.id for entry in grants)


async def test_enforce_outage_never_touches_an_existing_user(clients, widget, monkeypatch):
    async with session_maker() as db:
        db.add(User(email="existing@example.org"))
        await db.commit()
    monkeypatch.setattr(get_settings(), "turnstile_mode", "enforce")
    widget["answer"] = httpx.ConnectError("down")
    response, balance = await _sign_up_and_fund(clients, "existing@example.org", "tok")
    assert response.status_code == 200
    assert balance == get_settings().promo_grant_micro


@pytest.mark.parametrize(("token", "reason"), [
    ("!blocked", "blocked"), ("!pending", "pending"), ("!error", "error"), ("!<script>", "other"),
    ("tok", ""), ("", ""),
])
def test_client_reason_only_reads_the_bang_prefix(token, reason):
    assert turnstile.client_reason(token) == reason


async def test_a_client_reason_is_never_sent_to_cloudflare(widget):
    assert await turnstile.verify("!blocked", action="email_start") == turnstile.MISSING
    assert widget["forms"] == []


@pytest.mark.parametrize(("token", "words"), [
    ("!blocked", "blocked the security check"),
    ("!pending", "Tick the security check"),
    ("!error", "verify this browser"),
])
async def test_enforce_explains_why_the_browser_had_no_token(clients, widget, monkeypatch, token, words):
    monkeypatch.setattr(get_settings(), "turnstile_mode", "enforce")
    response, _ = await _start(clients, f"reason-{token[1:]}@example.org", token)
    assert response.status_code == 400
    assert words in response.json()["detail"]


async def test_the_rate_window_bounds_siteverify_calls(clients, widget, monkeypatch):
    monkeypatch.setattr(get_settings(), "turnstile_mode", "enforce")
    widget["answer"] = {"success": False, "error-codes": ["invalid-input-response"]}
    statuses = [(await _start(clients, "junk@example.org", "junk"))[0].status_code for _ in range(7)]
    assert statuses == [400] * 5 + [429] * 2
    assert len(widget["forms"]) == 5


async def test_a_fixed_code_account_skips_the_check(clients, widget, monkeypatch):
    import hashlib
    monkeypatch.setattr(get_settings(), "turnstile_mode", "enforce")
    monkeypatch.setattr(get_settings(), "fixed_login_codes",
                        "reviewer@example.com=" + hashlib.sha256(b"40718293561728394056").hexdigest())
    response = await clients.post("/auth/email/start", json={"email": "reviewer@example.com"})
    assert response.status_code == 200
    assert widget["forms"] == []


async def test_the_cli_login_page_runs_the_check(clients, widget):
    page = (await clients.get("/login", params={"cli": "abcDEF123-_x"})).text
    assert '<script src="/captcha.js" defer></script>' in page
    assert 'data-captcha="email_start"' in page
    assert "tregCaptcha.token('email_start')" in page


def test_every_email_sign_in_page_loads_the_check():
    """A page that posts to /auth/email/start without the check would 400 once enforce is on."""
    from pathlib import Path
    web = Path(signup.__file__).parents[1] / "web"
    frontend = Path(signup.__file__).parents[3] / "frontend"
    sources = [*web.rglob("*.html"), *web.rglob("*.js")]
    sources += [p for ext in ("*.js", "*.ts", "*.vue") for p in frontend.joinpath("src").rglob(ext)]
    callers = [p for p in sources
               if "dashboard" not in p.parts and "/auth/email/start" in p.read_text(errors="ignore")]
    assert len(callers) >= 12
    for path in callers:
        assert "tregCaptcha.token('email_start')" in path.read_text(), path
    pages = [p for p in web.glob("*.html") if 'data-captcha="email_start"' in p.read_text()]
    for page in pages:
        assert '<script src="/captcha.js" defer></script>' in page.read_text(), page
    assert 'src="/captcha.js"' in (frontend / "index.html").read_text()
    assert len(pages) >= 11


def test_the_cli_falls_back_to_the_browser_on_the_captcha_header():
    import httpx as _httpx
    from treg.cli import _wants_browser_check

    assert _wants_browser_check(_httpx.Response(400, headers={"X-Treg-Captcha": "required"}, json={"detail": "x"}))
    assert not _wants_browser_check(_httpx.Response(400, json={"detail": "We couldn't verify this browser."}))
    assert not _wants_browser_check(_httpx.Response(429, headers={"X-Treg-Captcha": "required"}))


@pytest.mark.parametrize("token", ["", "!blocked", "!pending"])
async def test_every_captcha_refusal_carries_the_header(clients, widget, monkeypatch, token):
    monkeypatch.setattr(get_settings(), "turnstile_mode", "enforce")
    response, _ = await _start(clients, f"hdr{len(token)}@example.org", token)
    assert response.status_code == 400
    assert response.headers["X-Treg-Captcha"] == "required"
    other = await clients.post("/auth/email/start", json={"email": "a@demo.treg.local"})
    assert "X-Treg-Captcha" not in other.headers


async def test_verify_logs_the_outcome_for_new_and_existing_users(clients, widget, caplog, monkeypatch):
    caplog.set_level("INFO", logger="treg")
    await _sign_up_and_fund(clients, "logged@example.org", "tok")
    await _sign_up_and_fund(clients, "logged@example.org", "tok")
    lines = [r.getMessage() for r in caplog.records if "turnstile_email_verify" in r.getMessage()]
    assert lines == ["event=turnstile_email_verify outcome=pass new_user=True client=browser",
                     "event=turnstile_email_verify outcome=pass new_user=False client=browser"]
    starts = [r.getMessage() for r in caplog.records if "turnstile_email_start" in r.getMessage()]
    assert starts[0] == "event=turnstile_email_start outcome=pass reason=- client=browser mode=observe"


async def test_a_rejected_real_solve_names_its_action_and_host(widget, caplog):
    caplog.set_level("INFO", logger="treg")
    widget["answer"] = {"success": True, "action": "email_start", "hostname": "preview.onrender.com"}
    assert await turnstile.verify("tok", action="email_start") == turnstile.INVALID
    assert "event=turnstile_mismatch action=email_start hostname=preview.onrender.com" in caplog.text
    widget["answer"] = {"success": False, "error-codes": ["timeout-or-duplicate", "bad\nline"]}
    assert await turnstile.verify("tok", action="email_start") == turnstile.INVALID
    assert "event=turnstile_rejected codes=timeout-or-duplicate,bad?line" in caplog.text


def test_misconfiguration_names_the_ways_enforce_would_refuse_everyone(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "public_url", "https://treg.example")
    monkeypatch.setattr(s, "turnstile_site_key", "")
    monkeypatch.setattr(s, "turnstile_secret", "")
    assert turnstile.misconfiguration() == []
    monkeypatch.setattr(s, "turnstile_site_key", "key")
    assert "only one of" in turnstile.misconfiguration()[0]
    monkeypatch.setattr(s, "turnstile_secret", "secret")
    monkeypatch.setattr(s, "turnstile_hostnames", "")
    assert turnstile.misconfiguration() == []
    monkeypatch.setattr(s, "turnstile_hostnames", "typo.example")
    assert "treg.example is not in" in turnstile.misconfiguration()[0]


async def test_captcha_js_revalidates_with_an_etag(clients, widget):
    first = await clients.get("/captcha.js")
    etag = first.headers["etag"].removeprefix("W/")
    for sent in (etag, "W/" + etag, '"other", W/' + etag):
        again = await clients.get("/captcha.js", headers={"If-None-Match": sent})
        assert again.status_code == 304 and again.content == b"", sent
    assert (await clients.get("/captcha.js", headers={"If-None-Match": '"other"'})).status_code == 200


@pytest.mark.parametrize("code", ["invalid-input-secret", "missing-input-secret"])
async def test_a_rejected_secret_is_our_problem_not_the_callers(widget, caplog, code):
    widget["answer"] = {"success": False, "error-codes": [code]}
    assert await turnstile.verify("tok", action="email_start") == turnstile.UNAVAILABLE
    assert f"event=turnstile_secret_rejected codes={code}" in caplog.text


async def test_a_code_requested_before_the_deploy_still_verifies_with_credit(clients):
    from treg import crypto, ratestore
    from treg.application.auth import EMAIL_CODE_TTL, MAX_OTP_ATTEMPTS, OTP_NS
    async with session_maker() as db:
        await ratestore.kv_put(db, OTP_NS, "legacy-entry@example.org",
                               {"hash": crypto.hash_token("123456"), "attempts": MAX_OTP_ATTEMPTS}, EMAIL_CODE_TTL)
        await db.commit()
    proof = await clients.post("/auth/email/verify", json={"email": "legacy-entry@example.org", "code": "123456"})
    assert proof.status_code == 200
    async with session_maker() as db:
        user = (await db.execute(select(User).where(User.email == "legacy-entry@example.org"))).scalar_one()
    assert user.signup_promo_available


async def test_observe_keeps_credit_for_an_invalid_token_and_records_it(clients, widget, signups):
    widget["answer"] = {"success": False, "error-codes": ["invalid-input-response"]}
    response, balance = await _sign_up_and_fund(clients, "observed-invalid@example.org", "tok")
    assert response.status_code == 200 and balance == get_settings().promo_grant_micro
    assert [props["captcha"] for event, props in signups if event == "signup_completed"] == ["invalid"]


async def test_enforce_outage_records_unavailable(clients, widget, signups, monkeypatch):
    monkeypatch.setattr(get_settings(), "turnstile_mode", "enforce")
    widget["answer"] = httpx.ConnectError("down")
    await _sign_up_and_fund(clients, "outage-event@example.org", "tok")
    assert [props["captcha"] for event, props in signups if event == "signup_completed"] == ["unavailable"]


async def test_a_released_cli_is_told_which_command_works(clients, widget, monkeypatch):
    """Released CLIs print `detail` and stop; only newer ones read the header."""
    monkeypatch.setattr(get_settings(), "turnstile_mode", "enforce")
    response = await clients.post("/auth/email/start", json={"email": "old-cli@example.org"},
                                  headers={"X-Treg-Key-Protocol": "1", "user-agent": "python-httpx/0.27"})
    assert response.status_code == 400
    assert response.headers["X-Treg-Captcha"] == "required"
    assert "Run `treg login` without --email" in response.json()["detail"]
