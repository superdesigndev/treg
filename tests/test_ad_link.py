"""The short install link: `/start` binds the visitor's ad click to a six-character code, the copied
command fetches `/i/<code>`, the served script hands the code to the CLI, and `treg login` sends it
back on the sign-in URL, where `/login` restores the attribution cookie. That is what lets a sign-in
completed in a browser the ad never touched (an agent's embedded browser) still land the click on
the team."""

from __future__ import annotations

from httpx import AsyncClient
from sqlmodel import select

from tests.test_adsconv import ads_disabled, ads_enabled  # noqa: F401
from treg.infra.db import session_maker
from treg.models import Org

LOGIN_ID = "a" * 24
COOKIE = "gclid%7CCLICK_LINK%7Cstart"  # as adtrack.js writes it: encodeURIComponent(kind|id|landing)


async def test_short_link_carries_the_click_into_a_cookieless_login(clients: AsyncClient, ads_enabled):
    clients.cookies.set("treg_ad", COOKIE)
    minted = await clients.post("/i")
    assert minted.status_code == 200, minted.text
    code = minted.json()["code"]
    assert code and len(code) == 6 and code == code.lower()

    script = await clients.get(f"/i/{code}")
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/x-shellscript")
    assert f'AD_LINK="{code}"' in script.text and ".treg/ad-link" in script.text
    assert "{BASE}" not in script.text and "{AD_LINK}" not in script.text

    clients.cookies.clear()  # the agent's browser never saw the ad
    page = await clients.get(f"/login?cli={LOGIN_ID}&ad={code}")
    assert page.status_code == 200
    set_cookie = page.headers.get("set-cookie", "")
    assert f"treg_ad={COOKIE}" in set_cookie
    max_age = int(set_cookie.split("Max-Age=")[1].split(";")[0])
    assert 7776000 - 60 <= max_age <= 7776000  # 90 days from the mint, not renewed
    assert "SameSite=lax" in set_cookie and "Path=/" in set_cookie
    assert "HttpOnly" not in set_cookie  # adtrack.js must still be able to read and carry it

    # The restored cookie reaches the signup door exactly like a first-party capture would.
    r = await clients.post("/users", json={"email": "link-user@example.com"})
    assert r.status_code == 200, r.text
    async with session_maker() as db:
        org = (await db.execute(select(Org).where(Org.name == "link-user@example.com"))).scalar_one()
        assert org.ad_gclid == "CLICK_LINK"
        assert org.ad_click_id_type == "gclid"
        assert org.ad_landing == "start"


async def test_no_click_no_code(clients: AsyncClient, ads_enabled):
    assert (await clients.post("/i")).json() == {"code": None}


async def test_feature_off_mints_nothing_and_restores_nothing(clients: AsyncClient, ads_disabled):
    clients.cookies.set("treg_ad", COOKIE)
    assert (await clients.post("/i")).json() == {"code": None}
    clients.cookies.clear()
    page = await clients.get(f"/login?cli={LOGIN_ID}&ad=abc123")
    assert page.status_code == 200
    assert "treg_ad=" not in page.headers.get("set-cookie", "")


async def test_unknown_code_still_installs_and_sets_no_cookie(clients: AsyncClient, ads_enabled):
    script = await clients.get("/i/zzzzzz")
    assert script.status_code == 200 and 'AD_LINK="zzzzzz"' in script.text
    assert (await clients.get("/i/not-a-code!")).status_code == 404
    page = await clients.get(f"/login?cli={LOGIN_ID}&ad=zzzzzz")
    assert page.status_code == 200
    assert "treg_ad=" not in page.headers.get("set-cookie", "")


async def test_plain_install_script_has_no_link(clients: AsyncClient):
    script = (await clients.get("/install.sh")).text
    assert 'AD_LINK=""' in script and "{AD_LINK}" not in script


async def test_minting_is_rate_limited_per_address(clients: AsyncClient, ads_enabled):
    clients.cookies.set("treg_ad", COOKIE)
    statuses = [(await clients.post("/i")).status_code for _ in range(31)]
    assert statuses[:30] == [200] * 30
    assert statuses[30] == 429


async def test_a_code_redeems_once(clients: AsyncClient, ads_enabled):
    clients.cookies.set("treg_ad", COOKIE)
    code = (await clients.post("/i")).json()["code"]
    clients.cookies.clear()
    first = await clients.get(f"/login?cli={LOGIN_ID}&ad={code}")
    assert f"treg_ad={COOKIE}" in first.headers.get("set-cookie", "")
    clients.cookies.clear()  # a stranger running the same pasted command
    second = await clients.get(f"/login?cli={LOGIN_ID}&ad={code}")
    assert second.status_code == 200
    assert "treg_ad=" not in second.headers.get("set-cookie", "")


async def test_a_browser_keeps_its_own_click(clients: AsyncClient, ads_enabled):
    clients.cookies.set("treg_ad", COOKIE)
    code = (await clients.post("/i")).json()["code"]
    clients.cookies.set("treg_ad", "gclid%7CNEWER_CLICK%7Chome")
    page = await clients.get(f"/login?cli={LOGIN_ID}&ad={code}")
    assert page.status_code == 200
    assert "treg_ad=" not in page.headers.get("set-cookie", "")


async def test_restored_cookie_expires_with_the_mint(clients: AsyncClient, ads_enabled, monkeypatch):
    import time as _time
    clients.cookies.set("treg_ad", COOKIE)
    code = (await clients.post("/i")).json()["code"]
    clients.cookies.clear()
    real = _time.time
    monkeypatch.setattr(_time, "time", lambda: real() + 29 * 86400)  # redeemed 29 days later
    set_cookie = (await clients.get(f"/login?cli={LOGIN_ID}&ad={code}")).headers.get("set-cookie", "")
    max_age = int(set_cookie.split("Max-Age=")[1].split(";")[0])
    assert 61 * 86400 - 60 <= max_age <= 61 * 86400


async def test_a_bad_login_link_does_not_spend_the_code(clients: AsyncClient, ads_enabled):
    clients.cookies.set("treg_ad", COOKIE)
    code = (await clients.post("/i")).json()["code"]
    clients.cookies.clear()
    assert (await clients.get(f"/login?cli=bad&ad={code}")).status_code == 400
    page = await clients.get(f"/login?cli={LOGIN_ID}&ad={code}")
    assert f"treg_ad={COOKIE}" in page.headers.get("set-cookie", "")
