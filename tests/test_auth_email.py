"""Email one-time-code login — the third identity door (alongside GitHub OAuth + per-org token).

Proving an email == login; first proof registers (creates a personal org). Dev mode returns the
code so dummy emails are testable without a mail sender.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from treg.api import app
from treg.config import get_settings
from treg.infra.db import reset_db


@pytest.fixture
async def client():
    await reset_db()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://registry",
        headers={"ngrok-skip-browser-warning": "1", "X-Treg-Key-Protocol": "1"},
    ) as c:
        yield c


async def _otp_login(c: AsyncClient, email: str) -> str:
    code = (await c.post("/auth/email/start", json={"email": email})).json()["dev_code"]
    r = await c.post("/auth/email/verify", json={"email": email, "code": code})
    assert r.status_code == 200, r.text
    return r.json()["token"]


async def test_first_login_registers_user_with_no_org_then_reuses_identity(client):
    tok = await _otp_login(client, "neo@matrix.io")
    orgs = (await client.get("/orgs", headers={"X-Treg-Token": tok})).json()
    assert orgs == []  # no auto personal org — the user names + creates their first team next

    await _otp_login(client, "neo@matrix.io")  # second time = login, not a new user
    orgs2 = (await client.get("/orgs", headers={"X-Treg-Token": tok})).json()
    assert orgs2 == []  # still no duplicate user; still zero orgs until they create one


async def test_verify_rejects_wrong_and_unknown_code(client):
    await client.post("/auth/email/start", json={"email": "trinity@matrix.io"})
    bad = await client.post("/auth/email/verify", json={"email": "trinity@matrix.io", "code": "000000"})
    assert bad.status_code == 401
    unknown = await client.post("/auth/email/verify", json={"email": "nobody@x.io", "code": "123456"})
    assert unknown.status_code == 401


async def test_code_is_one_time(client):
    email = "morpheus@matrix.io"
    code = (await client.post("/auth/email/start", json={"email": email})).json()["dev_code"]
    ok = await client.post("/auth/email/verify", json={"email": email, "code": code})
    assert ok.status_code == 200
    replay = await client.post("/auth/email/verify", json={"email": email, "code": code})
    assert replay.status_code == 401  # consumed on first use


async def test_start_is_rate_limited_per_email(client):
    from treg.routers.auth import OTP_START_MAX_PER_EMAIL
    email = "flood@matrix.io"
    for _ in range(OTP_START_MAX_PER_EMAIL):
        assert (await client.post("/auth/email/start", json={"email": email})).status_code == 200
    blocked = await client.post("/auth/email/start", json={"email": email})
    assert blocked.status_code == 429  # the (N+1)th code request for one inbox is refused (email-bomb guard)


async def test_start_is_rate_limited_per_ip(client):
    from treg.routers.auth import OTP_START_MAX_PER_IP
    for i in range(OTP_START_MAX_PER_IP):  # distinct emails so the per-email cap never trips first
        assert (await client.post("/auth/email/start", json={"email": f"u{i}@matrix.io"})).status_code == 200
    blocked = await client.post("/auth/email/start", json={"email": "late@matrix.io"})
    assert blocked.status_code == 429  # a fresh inbox is blocked purely by the per-IP cap


async def test_dev_mode_off_hides_the_code(client):
    get_settings.cache_clear()
    settings = get_settings()
    object.__setattr__(settings, "email_dev_mode", False)
    try:
        r = (await client.post("/auth/email/start", json={"email": "a@b.io"})).json()
        assert r["sent"] is True and "dev_code" not in r
    finally:
        object.__setattr__(settings, "email_dev_mode", True)
        get_settings.cache_clear()


async def test_signup_tracking_only_after_new_identity_not_login_or_bad_code(client, monkeypatch):
    from treg import analytics
    events = []
    monkeypatch.setattr(analytics, "capture", lambda *a, **k: events.append((a, k)))
    client.cookies.set("treg_entry_surface", "arena")
    email = "tracking@example.test"
    code = (await client.post("/auth/email/start", json={"email": email})).json()["dev_code"]
    assert (await client.post("/auth/email/verify", json={"email": email, "code": "wrong"})).status_code == 401
    assert events == []
    assert (await client.post("/auth/email/verify", json={"email": email, "code": code})).status_code == 200
    await _otp_login(client, email)
    signups = [a for a, _ in events if a[1] == "signup_completed"]
    assert len(signups) == 1
    assert signups[0] == (email, "signup_completed", {"signup_method": "email", "entry_surface": "arena"})


@pytest.mark.parametrize("method", ["github", "google"])
async def test_social_signup_tracking_reuses_same_new_user_rule(client, monkeypatch, method):
    from treg import analytics
    from treg.application.auth import _provision_social_user
    events = []
    monkeypatch.setattr(analytics, "capture", lambda *a, **k: events.append(a))
    await _provision_social_user("social@example.test", "unused", method, "arena")
    await _provision_social_user("social@example.test", "unused", method, "arena")
    assert len(events) == 1
    assert events[0][1:] == ("signup_completed", {"signup_method": method, "entry_surface": "arena"})


REVIEWER = "reviewer@example.com"
REVIEWER_CODE = "40718293561728394056"


@pytest.fixture
def fixed_code(monkeypatch):
    import hashlib
    digest = hashlib.sha256(REVIEWER_CODE.encode()).hexdigest()
    monkeypatch.setattr(get_settings(), "fixed_login_codes", f"Reviewer@Example.com={digest}")


async def test_designated_account_signs_in_with_its_fixed_code_and_nothing_is_sent(
    client, fixed_code, monkeypatch,
):
    from treg import email as email_sender
    sent: list[str] = []

    async def record(email, code, **_):
        sent.append(email)

    monkeypatch.setattr(get_settings(), "email_dev_mode", False)
    monkeypatch.setattr(email_sender, "send_otp", record)
    for _ in range(2):  # the same code works on every sign-in, not once
        start = await client.post("/auth/email/start", json={"email": REVIEWER})
        assert start.status_code == 200 and "dev_code" not in start.json()
        ok = await client.post("/auth/email/verify", json={"email": REVIEWER, "code": REVIEWER_CODE})
        assert ok.status_code == 200, ok.text
    assert sent == []  # a designated account has no inbox, so no code is ever mailed

    await client.post("/auth/email/start", json={"email": "someone@matrix.io"})
    assert sent == ["someone@matrix.io"]  # every other email still gets its emailed code


async def test_designated_account_keeps_the_attempt_limit(client, fixed_code):
    await client.post("/auth/email/start", json={"email": REVIEWER})
    from treg.application.auth import MAX_OTP_ATTEMPTS
    for _ in range(MAX_OTP_ATTEMPTS):
        bad = await client.post("/auth/email/verify", json={"email": REVIEWER, "code": "123456"})
        assert bad.status_code == 401
    # The issued code is spent after the allowed wrong guesses, so even the right code needs a new start.
    spent = await client.post("/auth/email/verify", json={"email": REVIEWER, "code": REVIEWER_CODE})
    assert spent.status_code == 401
    await client.post("/auth/email/start", json={"email": REVIEWER})
    ok = await client.post("/auth/email/verify", json={"email": REVIEWER, "code": REVIEWER_CODE})
    assert ok.status_code == 200


def test_fixed_login_codes_refuses_a_malformed_entry():
    from pydantic import ValidationError
    from treg.config import Settings
    with pytest.raises(ValidationError):
        Settings(fixed_login_codes="reviewer@example.com=not-a-sha256")
    with pytest.raises(ValidationError):
        Settings(fixed_login_codes="reviewer@example.com")
