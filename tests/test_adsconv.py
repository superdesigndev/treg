import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from conftest import make_upstream, verified_signup
from treg import adsconv
from treg.api import app
from treg.config import get_settings
from treg.infra.db import reset_db, session_maker
from pathlib import Path

from treg.models import AdConversion, Membership, Org, User
from treg.timeutil import utcnow_naive


def _h(token: str) -> dict:
    return {"X-Treg-Token": token}


class FakeAdsResponse:
    def __init__(self, body: dict, status_code: int = 200):
        self._body = body
        self.status_code = status_code
        self.text = json.dumps(body)

    def json(self) -> dict:
        return self._body


class FakeAdsClient:
    """Routes POSTs by URL, because one drain now makes TWO different calls: the token exchange
    (`adsconv.GOOGLE_TOKEN_URL`) and the Data Manager ingest (`adsconv.DATA_MANAGER_URL`). Tests
    that care about only one keep the default for the other; `token_calls`/`calls` are recorded
    separately so a caching test can assert the exchange did NOT repeat while the ingest did."""
    def __init__(self, data_response: FakeAdsResponse,
                 token_response: FakeAdsResponse | None = None):
        self.data_response = data_response
        self.token_response = token_response or FakeAdsResponse(
            {"access_token": "tok-test", "expires_in": 3599}
        )
        self.calls = []        # Data Manager events:ingest calls
        self.token_calls = []  # oauth2.googleapis.com/token calls

    async def post(self, url, **kwargs):
        if url == adsconv.GOOGLE_TOKEN_URL:
            self.token_calls.append((url, kwargs))
            return self.token_response
        self.calls.append((url, kwargs))
        return self.data_response


@pytest.fixture
def ads_enabled(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "google_ads_customer_id", "5149790776", raising=False)
    monkeypatch.setattr(settings, "google_ads_developer_token", "dev-tok-test", raising=False)
    monkeypatch.setattr(settings, "google_ads_login_customer_id", "", raising=False)
    monkeypatch.setattr(settings, "google_ads_client_id", "ads-client-id-test", raising=False)
    monkeypatch.setattr(settings, "google_ads_client_secret", "ads-client-secret-test", raising=False)
    monkeypatch.setattr(settings, "ads_conv_refresh_token", "ads-refresh-tok-test", raising=False)
    # The access-token cache lives in MODULE state (see adsconv._auth_headers), deliberately, so
    # the worker doesn't re-exchange the refresh token every 300s drain. That means it persists
    # ACROSS tests unless reset here — without this a token cached by an earlier test would make
    # a later test's drain skip the exchange entirely and its FakeAdsClient assertions lie.
    monkeypatch.setattr(adsconv, "_cached_access_token", None, raising=False)
    monkeypatch.setattr(adsconv, "_token_expires_at", 0.0, raising=False)
    assert adsconv.enabled() is True
    return settings


@pytest.fixture
def ads_disabled(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "google_ads_customer_id", "", raising=False)
    monkeypatch.setattr(settings, "google_ads_developer_token", "", raising=False)
    monkeypatch.setattr(settings, "ads_conv_refresh_token", "", raising=False)
    assert adsconv.enabled() is False
    return settings


@pytest.fixture
async def callenv(ads_enabled):
    """An ad-attributed org with one callable HTTP tool pointed at the fake upstream."""
    await reset_db()
    app.state.http = AsyncClient(transport=ASGITransport(app=make_upstream()),
                                 base_url="http://upstream")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
        r = await verified_signup(c, json={"email": "caller@example.com"})
        assert r.status_code == 200, r.text
        token, org_id = r.json()["token"], r.json()["org_id"]
        sid = (await c.post("/secrets", headers=_h(token),
                            json={"name": "a-key", "value": "v"})).json()["id"]
        await c.post("/tools", headers=_h(token),
                     json={"name": "alpha", "base_url": "http://upstream", "secret_id": sid})
        async with session_maker() as db:            # attribute the org to an ad click
            org = await db.get(Org, org_id)
            org.ad_gclid = "CLICK_CALL"
            db.add(org)
            await db.commit()
        yield SimpleNamespace(c=c, token=token, org_id=org_id)
    await app.state.http.aclose()


@pytest.mark.parametrize("usd,aud", [
    (20_000_000, 28_571_428),   # 1 AUD = 0.70 USD, so USD converts UP into AUD: US$20.00 -> A$28.57
    (1, 1),                     # integer only: 1 micro-USD must not become 1.4285... micro-AUD
    (0, 0),
    (-7_000_000, -10_000_000),  # even-divisible negative: -7,000,000 * 10 / 7 exactly
    (-1_000_000, -1_428_572),   # non-exact negative: floor division rounds away from zero
])
def test_usd_to_aud_is_a_fixed_integer_rate(usd, aud):
    result = adsconv.usd_micro_to_aud_micro(usd)
    assert isinstance(result, int)
    assert result == aud


@pytest.mark.parametrize(
    "setting_name",
    ["google_ads_customer_id", "ads_conv_refresh_token"],
)
def test_tracking_is_disabled_when_any_required_setting_is_missing(
    monkeypatch, ads_enabled, setting_name
):
    monkeypatch.setattr(ads_enabled, setting_name, "", raising=False)
    assert adsconv.enabled() is False


async def test_ad_conversion_is_unique_per_org_and_action(clients):
    async with session_maker() as db:
        org = Org(name="t", slug="t-adsconv")
        db.add(org)
        await db.commit()
        await db.refresh(org)

        db.add(AdConversion(org_id=org.id, action="signup", dedupe_key="signup"))
        await db.commit()

        db.add(AdConversion(org_id=org.id, action="signup", dedupe_key="signup"))
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_queue_writes_one_row_and_is_idempotent(clients, ads_enabled):
    async with session_maker() as db:
        org = Org(name="t", slug="t-queue", ad_gclid="CLICK1")
        db.add(org)
        await db.commit()
        await db.refresh(org)

        assert await adsconv.queue(db, org, adsconv.ACTION_SIGNUP) is True
        await db.commit()
        # Second call for the same (org, action) must be a silent no-op, not an error
        assert await adsconv.queue(db, org, adsconv.ACTION_SIGNUP) is False
        await db.commit()

        rows = (await db.execute(
            select(AdConversion).where(AdConversion.org_id == org.id))).scalars().all()
        assert len(rows) == 1
        assert rows[0].uploaded_at is None


async def test_queue_is_a_noop_without_a_gclid(clients, ads_enabled):
    # Organic signups are the majority; they must not fill the outbox with unattributable rows.
    async with session_maker() as db:
        org = Org(name="t", slug="t-noclick")
        db.add(org)
        await db.commit()
        await db.refresh(org)
        assert await adsconv.queue(db, org, adsconv.ACTION_SIGNUP) is False


async def test_queue_is_a_noop_when_disabled(clients, ads_disabled):
    async with session_maker() as db:
        org = Org(name="t", slug="t-disabled", ad_gclid="CLICK_DISABLED")
        db.add(org)
        await db.commit()
        await db.refresh(org)
        assert await adsconv.queue(db, org, adsconv.ACTION_SIGNUP) is False
        assert (await db.execute(select(AdConversion))).scalars().all() == []


@pytest.mark.parametrize("email,cookie,click_id,click_type,landing", [
    # the legacy cookie format remains readable
    ("click@example.com", "CLICK_XYZ|p3", "CLICK_XYZ", "gclid", "p3"),
    ("braid@example.com", "wbraid|BRAID_XYZ|p4", "BRAID_XYZ", "wbraid", "p4"),
])
async def test_signup_persists_the_ad_click_cookie(clients, ads_enabled, email, cookie, click_id,
                                                   click_type, landing):
    r = await verified_signup(clients, json={"email": email}, cookies={"treg_ad": cookie})
    assert r.status_code == 200, r.text
    async with session_maker() as db:
        org = await db.get(Org, r.json()["org_id"])
        assert org.ad_gclid == click_id
        assert org.ad_click_id_type == click_type
        assert org.ad_landing == landing
        assert org.ad_click_at is not None


async def test_signup_without_the_cookie_leaves_attribution_null(clients):
    r = await clients.post("/orgs", json={"name": "organic team"})
    assert r.status_code == 200, r.text
    async with session_maker() as db:
        org = await db.get(Org, r.json()["org_id"])
        assert org.ad_gclid is None


async def test_disabled_signup_ignores_ad_cookie(clients, ads_disabled):
    r = await verified_signup(clients, json={"email": "disabled-click@example.com"},
        cookies={"treg_ad": "gclid|CLICK_DISABLED|p1"},
    )
    assert r.status_code == 200, r.text
    async with session_maker() as db:
        org = await db.get(Org, r.json()["org_id"])
        assert org.ad_gclid is None
        assert (await db.execute(select(AdConversion))).scalars().all() == []


@pytest.mark.parametrize("path", ["/adtrack.js", "/gtag.js"])
async def test_tracking_script_is_empty_when_disabled(clients, ads_disabled, path):
    r = await clients.get(path)
    assert r.status_code == 200
    assert r.text == ""


async def test_signup_queues_a_conversion_when_attributed(clients, ads_enabled):
    r = await verified_signup(clients, json={"email": "conv@example.com"},
                              cookies={"treg_ad": "CLICK_SIGNUP|p1"})
    assert r.status_code == 200, r.text
    async with session_maker() as db:
        rows = (await db.execute(select(AdConversion).where(
            AdConversion.org_id == r.json()["org_id"]))).scalars().all()
        assert [x.action for x in rows] == [adsconv.ACTION_SIGNUP]


async def test_org_creation_persists_the_gclid_cookie(clients, ads_enabled):
    # The OTHER signup door: a signed-in user creating their first team via /orgs (the browser
    # sign-in path). `clients` is already authenticated by the fixture; this only adds the
    # ad-click cookie on top.
    r = await clients.post(
        "/orgs",
        json={"name": "ad team"},
        cookies={"treg_ad": "CLICK_XYZ|p3"},
    )
    assert r.status_code == 200, r.text
    async with session_maker() as db:
        org = (await db.execute(select(Org).where(Org.id == r.json()["org_id"]))).scalar_one()
        assert org.ad_gclid == "CLICK_XYZ"
        assert org.ad_landing == "p3"
        assert org.ad_click_at is not None


async def test_first_successful_call_fires_once(callenv):
    """Two successful calls: one timestamp, one conversion. The second must be a no-op."""
    r1 = await callenv.c.get("/call/alpha", headers=_h(callenv.token))
    assert 200 <= r1.status_code < 400, r1.text
    r2 = await callenv.c.get("/call/alpha", headers=_h(callenv.token))
    assert 200 <= r2.status_code < 400, r2.text

    async with session_maker() as db:
        org = await db.get(Org, callenv.org_id)
        assert org.first_call_at is not None
        rows = (await db.execute(select(AdConversion).where(
            AdConversion.org_id == callenv.org_id,
            AdConversion.action == adsconv.ACTION_FIRST_CALL))).scalars().all()
        assert len(rows) == 1


async def test_unattributed_org_records_timestamp_but_no_conversion(callenv):
    """first_call_at is a product metric and must be set for every team; only ad-clicked ones queue."""
    async with session_maker() as db:
        org = await db.get(Org, callenv.org_id)
        org.ad_gclid = None
        db.add(org)
        await db.commit()

    assert (await callenv.c.get("/call/alpha", headers=_h(callenv.token))).status_code < 400
    async with session_maker() as db:
        org = await db.get(Org, callenv.org_id)
        assert org.first_call_at is not None
        rows = (await db.execute(select(AdConversion).where(
            AdConversion.org_id == callenv.org_id))).scalars().all()
        assert rows == []


def test_build_payload_converts_currency_and_formats_time():
    click = datetime(2026, 8, 17, 3, 0, tzinfo=timezone.utc)
    org = Org(id=1, name="t", slug="t", ad_gclid="CLICK1", ad_click_at=click)
    row = AdConversion(id=1, org_id=1, action=adsconv.ACTION_PAID,
                       value_usd_micro=20_000_000,
                       created_at=click + timedelta(hours=6))
    payload = adsconv.build_payload([row], {1: org})
    event = payload["events"][0]
    assert event["adIdentifiers"]["gclid"] == "CLICK1"
    assert event["eventTimestamp"] == "2026-08-17T09:00:00Z"  # RFC 3339, not the old "+hh:mm" form
    assert event["destinationReferences"] == [adsconv.ACTION_PAID]
    # NOT bare "1": Data Manager 400s on a purely numeric transactionId (verified live
    # 2026-08-18). validateOnly does not catch it, so this assertion is the guard.
    assert event["transactionId"] == "treg-1"
    # US$20.00 at the fixed rate -> A$28.571428
    assert event["conversionValue"] == pytest.approx(28.571428, rel=1e-6)
    assert event["currency"] == "AUD"
    dest = payload["destinations"][0]
    assert dest["productDestinationId"] == "7723667020"
    assert dest["reference"] == adsconv.ACTION_PAID
    assert dest["operatingAccount"] == {
        "accountType": "GOOGLE_ADS", "accountId": get_settings().google_ads_customer_id
    }
    assert payload["validateOnly"] is False


@pytest.mark.parametrize("click_field", ["gclid", "gbraid", "wbraid"])
def test_build_payload_preserves_click_id_field(click_field):
    org = Org(id=1, name="t", slug="t", ad_gclid="CLICK1", ad_click_id_type=click_field)
    row = AdConversion(id=1, org_id=1, action=adsconv.ACTION_SIGNUP)
    ad_identifiers = adsconv.build_payload([row], {1: org})["events"][0]["adIdentifiers"]
    assert ad_identifiers[click_field] == "CLICK1"
    assert set(ad_identifiers) & {"gclid", "gbraid", "wbraid"} == {click_field}


def test_build_payload_omits_value_for_non_revenue_actions():
    org = Org(id=1, name="t", slug="t", ad_gclid="C", ad_click_at=datetime.now(timezone.utc))
    row = AdConversion(id=1, org_id=1, action=adsconv.ACTION_SIGNUP, value_usd_micro=0)
    event = adsconv.build_payload([row], {1: org})["events"][0]
    assert "conversionValue" not in event
    assert "currency" not in event


def test_build_payload_batches_every_action_into_one_request_with_deduped_destinations():
    """The whole point of the Data Manager port: a mixed batch spans multiple conversion actions
    but still goes out as ONE request, with `destinations` deduped by action and each event routed
    to its own destination via `destinationReferences` — not one request per action."""
    org = Org(id=1, name="t", slug="t", ad_gclid="C")
    rows = [
        AdConversion(id=1, org_id=1, action=adsconv.ACTION_SIGNUP),
        AdConversion(id=2, org_id=1, action=adsconv.ACTION_FIRST_CALL),
        AdConversion(id=3, org_id=1, action=adsconv.ACTION_PAID, value_usd_micro=1_000_000),
        AdConversion(id=4, org_id=1, action=adsconv.ACTION_SIGNUP),  # second signup row
    ]
    payload = adsconv.build_payload(rows, {1: org})
    assert len(payload["events"]) == 4  # one event per row, no merging
    references = [d["reference"] for d in payload["destinations"]]
    assert sorted(references) == sorted(
        {adsconv.ACTION_SIGNUP, adsconv.ACTION_FIRST_CALL, adsconv.ACTION_PAID}
    )
    assert len(references) == len(set(references))  # deduped: 4 rows, only 3 actions
    for event, row in zip(payload["events"], rows):
        assert event["destinationReferences"] == [row.action]
        assert event["transactionId"] == f"treg-{row.id}"


def test_build_payload_sets_manager_account_per_destination(monkeypatch, ads_enabled):
    """Data Manager expresses the manager (MCC) account as `destinations[].loginAccount`, not a
    `login-customer-id` header (see `_auth_headers`) — and never off `Secret.resource_ref`, which
    is the target CLIENT account discovery stored, not the manager."""
    org = Org(id=1, name="t", slug="t", ad_gclid="C")
    row = AdConversion(id=1, org_id=1, action=adsconv.ACTION_SIGNUP)

    monkeypatch.setattr(get_settings(), "google_ads_login_customer_id", "351-912-5194", raising=False)
    dest = adsconv.build_payload([row], {1: org})["destinations"][0]
    assert dest["loginAccount"] == {"accountType": "GOOGLE_ADS", "accountId": "3519125194"}

    monkeypatch.setattr(get_settings(), "google_ads_login_customer_id", "", raising=False)
    dest = adsconv.build_payload([row], {1: org})["destinations"][0]
    assert "loginAccount" not in dest


async def test_drain_sends_every_pending_row_in_one_batch(clients, ads_enabled):
    """Rows are sent as soon as they exist, regardless of age, and a mixed batch goes in one call.

    `_auth_headers` exchanges treg's OWN platform `ads_conv_refresh_token` against Google's token
    endpoint, which `FakeAdsClient` fakes alongside the Data Manager ingest call.
    """
    client = FakeAdsClient(FakeAdsResponse({"requestId": "req-drain-1"}))

    async with session_maker() as db:
        org = Org(name="t", slug="t-drain", ad_gclid="C",
                  ad_click_at=utcnow_naive() - timedelta(days=1))
        db.add(org)
        await db.commit()
        await db.refresh(org)
        old = AdConversion(org_id=org.id, action=adsconv.ACTION_SIGNUP,
                           created_at=utcnow_naive() - timedelta(hours=12))
        fresh = AdConversion(org_id=org.id, action=adsconv.ACTION_PAID,
                             created_at=utcnow_naive())
        db.add(old)
        db.add(fresh)
        await db.commit()

        await adsconv.drain_once(db, client)

        await db.refresh(old)
        await db.refresh(fresh)
        for row in (old, fresh):
            assert row.uploaded_at is not None
            assert row.next_attempt_at is None
            assert row.failed_at is None
        assert len(client.calls) == 1
        assert client.calls[0][0] == adsconv.DATA_MANAGER_URL
        assert len(client.token_calls) == 1
        assert client.token_calls[0][0] == adsconv.GOOGLE_TOKEN_URL


async def _seed_upload_rows(db, *, actions, attempts=0):
    """Create an ad-attributed org and old, upload-due outbox rows. No OAuth Secret is needed
    anymore — the uploader authenticates with treg's own platform refresh token, not a per-org
    connection (see `ads_enabled`, which sets it)."""
    org = Org(name="t", slug="t-upload-batch", ad_gclid="CLICK_BATCH",
              ad_click_at=utcnow_naive() - timedelta(days=1))
    db.add(org)
    await db.commit()
    await db.refresh(org)
    rows = [
        AdConversion(org_id=org.id, action=action, attempts=attempts,
                     created_at=utcnow_naive() - timedelta(hours=12))
        for action in actions
    ]
    db.add_all(rows)
    await db.commit()
    return org, rows


def _field_violation(index: int, reason: str, description: str = "event rejected") -> dict:
    """A `google.rpc.BadRequest.FieldViolation` naming one row of a REJECTED (non-200) request."""
    return {"field": f"events[{index}].adIdentifiers", "reason": reason, "description": description}


def _rejected(*violations: dict, message: str = "There was a problem with the request") -> dict:
    """The standard Google API error envelope Data Manager returns on a non-200 — the shape
    `_partial_failure_errors` parses. No `results`/`partialFailureError` here: Data Manager has no
    per-event success list on a 200, and no partial-failure shape at all (see adsconv.py)."""
    return {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": message,
                       "details": [{"fieldViolations": list(violations)}] if violations else []}}


async def test_drain_only_dead_letters_the_row_the_rejection_actually_names(clients, ads_enabled):
    """A 400 rejects the WHOLE request — nothing in it was ingested — but Google's fieldViolations
    can still say WHICH row's data caused the rejection. That row is a dead-letter candidate once it
    hits the attempt ceiling; its innocent batch-mate keeps retrying regardless of its own attempt
    count, because the rejection was never about ITS data."""
    body = _rejected(_field_violation(1, "INVALID_HEX_ENCODING", "bad gclid"))
    client = FakeAdsClient(FakeAdsResponse(body, status_code=400))
    async with session_maker() as db:
        _org, rows = await _seed_upload_rows(
            db, actions=(adsconv.ACTION_SIGNUP, adsconv.ACTION_PAID), attempts=7
        )
        result = await adsconv.drain_once(db, client)
        for row in rows:
            await db.refresh(row)

        assert result == {"sent": 0, "retried": 1, "failed": 1, "status": 400}
        # row 0 (index 0): not named in any violation -> always retried, ceiling or not.
        assert rows[0].uploaded_at is None
        assert rows[0].failed_at is None
        assert rows[0].next_attempt_at is not None
        # row 1 (index 1): named, and this call pushes it to the attempt ceiling -> dead-lettered.
        assert rows[1].uploaded_at is None
        assert rows[1].next_attempt_at is None
        assert rows[1].failed_at is not None
        assert "INVALID_HEX_ENCODING" in rows[1].error


async def test_http_failure_keeps_retrying_past_row_attempt_ceiling(clients, ads_enabled):
    client = FakeAdsClient(FakeAdsResponse({"error": {"code": 503, "message": "unavailable"}},
                                            status_code=503))
    async with session_maker() as db:
        _org, (row,) = await _seed_upload_rows(db, actions=(adsconv.ACTION_SIGNUP,), attempts=7)

        first = await adsconv.drain_once(db, client)
        await db.refresh(row)
        assert first["retried"] == 1
        assert row.attempts == 8
        assert row.failed_at is None
        assert row.next_attempt_at is not None

        # Make the scheduled retry due without sleeping. A second transport failure still must not
        # dead-letter the conversion merely because its historical attempt count is now above 8:
        # nothing named THIS row, so it is never a dead-letter candidate on this response alone.
        row.next_attempt_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
        db.add(row)
        await db.commit()
        second = await adsconv.drain_once(db, client)
        await db.refresh(row)
        assert second["retried"] == 1
        assert row.attempts == 9
        assert row.failed_at is None
        assert row.next_attempt_at is not None


async def test_permanent_row_failure_is_dead_lettered_after_attempt_ceiling(clients, ads_enabled):
    body = _rejected(_field_violation(0, "INVALID_HEX_ENCODING"))
    client = FakeAdsClient(FakeAdsResponse(body, status_code=400))
    async with session_maker() as db:
        _org, (row,) = await _seed_upload_rows(db, actions=(adsconv.ACTION_SIGNUP,), attempts=7)
        result = await adsconv.drain_once(db, client)
        await db.refresh(row)

        assert result["failed"] == 1
        assert row.attempts == 8
        assert row.uploaded_at is None
        assert row.next_attempt_at is None
        assert row.failed_at is not None
        assert "INVALID_HEX_ENCODING" in row.error


async def test_unstructured_success_response_does_not_dead_letter_rows(clients, ads_enabled):
    """A 200 with no `requestId` is not proof anything was accepted — unlike the old API, there is
    no per-row `results` list to fall back to, so the whole batch is retried."""
    client = FakeAdsClient(FakeAdsResponse({}))
    async with session_maker() as db:
        _org, (row,) = await _seed_upload_rows(db, actions=(adsconv.ACTION_SIGNUP,), attempts=7)
        result = await adsconv.drain_once(db, client)
        await db.refresh(row)

        assert result["retried"] == 1
        assert row.attempts == 8
        assert row.uploaded_at is None
        assert row.failed_at is None
        assert row.next_attempt_at is not None
        assert row.error == "200: missing requestId"


async def test_drain_acknowledges_rows_despite_nonfatal_field_warnings(clients, ads_enabled):
    """A `fieldWarnings` entry on a 200 is explicitly non-rejecting per Data Manager's contract —
    the record was still ingested, just with part of its data ignored — so the row is uploaded, not
    retried or dead-lettered. This is Data Manager's replacement for the old API's
    CLICK_CONVERSION_ALREADY_EXISTS acknowledge-anyway path: there is no error to special-case
    because Google now just accepts (and dedupes by `transactionId`) rather than rejecting."""
    body = {
        "requestId": "req-warn-1",
        "fieldWarnings": [
            {"fieldPath": "events[0].userData", "reason": "PARTIAL_DATA_IGNORED",
             "description": "some identifiers ignored"},
        ],
    }
    client = FakeAdsClient(FakeAdsResponse(body))
    async with session_maker() as db:
        _org, (row,) = await _seed_upload_rows(db, actions=(adsconv.ACTION_SIGNUP,))
        result = await adsconv.drain_once(db, client)
        await db.refresh(row)

        assert result["sent"] == 1
        assert row.uploaded_at is not None
        assert row.failed_at is None
        assert "PARTIAL_DATA_IGNORED" in row.error  # kept for operator visibility only


async def test_token_exchange_uses_the_platform_refresh_token_and_ads_client(ads_enabled):
    """The exchange must be a `grant_type=refresh_token` POST redeemed with the SAME OAuth client
    the refresh token was issued against (`google_ads_client_id`/`_secret`) — never the shared
    Google login client, and never anything derived from a customer's own OAuth connection. This is
    the crux of the option-A rework: treg's marketing uploader and a customer's Ads connection are
    two different credentials for two different purposes."""
    client = FakeAdsClient(FakeAdsResponse({"requestId": "unused"}))
    headers = await adsconv._auth_headers(client)
    assert headers["Authorization"] == "Bearer tok-test"
    assert len(client.token_calls) == 1
    url, kwargs = client.token_calls[0]
    assert url == adsconv.GOOGLE_TOKEN_URL
    assert kwargs["data"] == {
        "grant_type": "refresh_token",
        "refresh_token": ads_enabled.ads_conv_refresh_token,
        "client_id": ads_enabled.google_ads_client_id,
        "client_secret": ads_enabled.google_ads_client_secret,
    }


async def test_token_exchange_failure_raises_a_clear_error(ads_enabled):
    """A failed exchange must raise, not silently produce a broken Authorization header —
    `drain_once` runs inside `worker`'s try/except, so raising here just retries next pass rather
    than crashing the loop or sending an unauthenticated request."""
    client = FakeAdsClient(
        FakeAdsResponse({"requestId": "unused"}),
        token_response=FakeAdsResponse({"error": "invalid_grant"}, status_code=400),
    )
    with pytest.raises(RuntimeError, match="token refresh failed"):
        await adsconv._auth_headers(client)
    assert len(client.token_calls) == 1


async def test_access_token_is_cached_and_not_re_exchanged_within_its_lifetime(clients, ads_enabled):
    """The whole point of caching: the worker drains every 300s and a token lives ~3599s, so a
    second drain in the same window must reuse the cached token rather than exchange again. This
    test genuinely fails without the cache — remove it and `_auth_headers` calls
    `_exchange_refresh_token` on every drain, so `token_calls` would be 2, not 1, after the second
    drain below."""
    client = FakeAdsClient(FakeAdsResponse({"requestId": "req-cache-1"}))
    async with session_maker() as db:
        org = Org(name="t", slug="t-token-cache", ad_gclid="C",
                  ad_click_at=utcnow_naive() - timedelta(days=1))
        db.add(org)
        await db.commit()
        await db.refresh(org)

        row_a = AdConversion(org_id=org.id, action=adsconv.ACTION_SIGNUP,
                             created_at=utcnow_naive() - timedelta(hours=12))
        db.add(row_a)
        await db.commit()
        first = await adsconv.drain_once(db, client)
        assert first["sent"] == 1
        assert len(client.token_calls) == 1  # first drain: exchanged once

        row_b = AdConversion(org_id=org.id, action=adsconv.ACTION_FIRST_CALL,
                             created_at=utcnow_naive() - timedelta(hours=12))
        db.add(row_b)
        await db.commit()
        second = await adsconv.drain_once(db, client)
        assert second["sent"] == 1

        assert len(client.calls) == 2                # two ingest POSTs, one per drain
        assert len(client.token_calls) == 1           # still only ONE exchange, ever
        auth1 = client.calls[0][1]["headers"]["Authorization"]
        auth2 = client.calls[1][1]["headers"]["Authorization"]
        assert auth1 == auth2 == "Bearer tok-test"    # the second drain reused the cached token


async def test_expired_cached_token_triggers_a_fresh_exchange(monkeypatch, ads_enabled):
    """The cache is time-bounded, not permanent: once within `_TOKEN_SKEW_S` of expiring (or
    already expired), `_auth_headers` must exchange again rather than keep serving a token Google
    would reject."""
    monkeypatch.setattr(adsconv, "_cached_access_token", "stale-token", raising=False)
    monkeypatch.setattr(adsconv, "_token_expires_at", time.time() - 1, raising=False)
    client = FakeAdsClient(
        FakeAdsResponse({"requestId": "unused"}),
        token_response=FakeAdsResponse({"access_token": "fresh-token", "expires_in": 3599}),
    )
    headers = await adsconv._auth_headers(client)
    assert headers["Authorization"] == "Bearer fresh-token"
    assert len(client.token_calls) == 1


# ---- Enhanced Conversions for Leads: hashed-email identifiers ---------------------------------


@pytest.fixture
def user_data_enabled(ads_enabled, monkeypatch):
    monkeypatch.setattr(ads_enabled, "ads_conv_user_data", True, raising=False)
    assert adsconv.user_data_enabled() is True
    return ads_enabled


def test_email_normalisation_follows_googles_rules():
    # Trim + lowercase everywhere; Gmail additionally ignores dots in the local part, so the two
    # spellings of one inbox must hash to ONE identifier or Google matches only one of them.
    assert adsconv.normalize_email("  J.Z@Gmail.com ") == "jz@gmail.com"
    assert adsconv.normalize_email("Bob.Smith@Example.COM") == "bob.smith@example.com"
    assert adsconv.normalize_email("first.last@googlemail.com") == "firstlast@googlemail.com"
    assert adsconv.hash_email("J.Z@gmail.com") == hashlib.sha256(b"jz@gmail.com").hexdigest()
    assert adsconv.hash_email("jz@gmail.com") == adsconv.hash_email("  J.Z@GMAIL.COM ")


def test_user_data_requires_the_uploader_to_be_enabled(ads_disabled, monkeypatch):
    monkeypatch.setattr(ads_disabled, "ads_conv_user_data", True, raising=False)
    assert adsconv.user_data_enabled() is False


def test_build_payload_without_user_data_flag_carries_click_ids_only(ads_enabled):
    org = Org(id=1, name="t", slug="t", ad_gclid="CLICK1")
    row = AdConversion(id=1, org_id=1, action=adsconv.ACTION_SIGNUP, created_at=utcnow_naive())
    event = adsconv.build_payload([row], {1: org}, {1: "lead@example.com"})["events"][0]
    assert event["adIdentifiers"] == {"gclid": "CLICK1"}
    assert "userData" not in event and "consent" not in event
    assert "encoding" not in adsconv.build_payload([row], {1: org}, {1: "lead@example.com"})


def test_build_payload_attaches_hashed_email_next_to_the_click_id(user_data_enabled):
    org = Org(id=1, name="t", slug="t", ad_gclid="CLICK1")
    row = AdConversion(id=1, org_id=1, action=adsconv.ACTION_SIGNUP, created_at=utcnow_naive())
    event = adsconv.build_payload([row], {1: org}, {1: "Lead@Example.com"})["events"][0]
    assert event["adIdentifiers"] == {"gclid": "CLICK1"}
    assert event["userData"] == {"userIdentifiers": [{"emailAddress": adsconv.hash_email("lead@example.com")}]}
    # Google will not use an EEA identifier without a consent block; personalisation is never granted.
    assert event["consent"] == {"adUserData": "CONSENT_GRANTED", "adPersonalization": "CONSENT_DENIED"}
    # The raw address must never appear anywhere in the wire body.
    assert "lead@example.com" not in json.dumps(adsconv.build_payload([row], {1: org}, {1: "lead@example.com"})).lower()


def test_build_payload_identifies_an_unattributed_team_by_hashed_email_alone(user_data_enabled):
    org = Org(id=2, name="t", slug="t2")  # organic: no click id at all
    row = AdConversion(id=2, org_id=2, action=adsconv.ACTION_FIRST_CALL, created_at=utcnow_naive())
    payload = adsconv.build_payload([row], {2: org}, {2: "lead@example.com"})
    event = payload["events"][0]
    assert "adIdentifiers" not in event
    assert event["userData"]["userIdentifiers"] == [{"emailAddress": adsconv.hash_email("lead@example.com")}]
    assert payload["destinations"][0]["productDestinationId"] == "7723667017"
    # Data Manager rejects any request carrying userData without saying how the hashes are
    # encoded (live validateOnly, 2026-09-21); a click-only request must not carry it.
    assert payload["encoding"] == "HEX"
    # Neither a click id nor an email: not an event.
    empty = adsconv.build_payload([row], {2: org}, {})
    assert empty["events"] == [] and "encoding" not in empty


async def test_queue_records_an_unattributed_signup_when_user_data_is_enabled(clients, user_data_enabled):
    await reset_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
        r = await verified_signup(c, json={"email": "lead@example.com"})  # no treg_ad cookie
        assert r.status_code == 200, r.text
        org_id = r.json()["org_id"]
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        assert org.ad_gclid is None
        rows = (await db.execute(select(AdConversion).where(AdConversion.org_id == org_id))).scalars().all()
        assert [r.action for r in rows] == [adsconv.ACTION_SIGNUP]


async def test_queue_skips_a_team_whose_only_member_is_an_agent(clients, user_data_enabled):
    # An agent identity lives on AGENT_DOMAIN: unroutable, matches nobody at Google, must not
    # fill the outbox with rows that can only dead-letter.
    async with session_maker() as db:
        org = Org(name="t", slug="t-agent-only")
        bot = User(email="agent-t-bot@agents.treg.local")
        db.add(org); db.add(bot)
        await db.commit(); await db.refresh(org); await db.refresh(bot)
        db.add(Membership(user_id=bot.id, org_id=org.id, role="member", token_hash="x"))
        await db.commit()
        assert await adsconv.queue(db, org, adsconv.ACTION_FIRST_CALL) is False
        assert await adsconv.human_owner_emails(db, {org.id}) == {}


async def test_human_owner_is_the_earliest_human_member(clients, user_data_enabled):
    async with session_maker() as db:
        org = Org(name="t", slug="t-owner")
        bot = User(email="agent-t-x@agents.treg.local")
        human = User(email="Creator@Example.com")
        later = User(email="teammate@example.com")
        db.add_all([org, bot, human, later])
        await db.commit()
        for u in (org, bot, human, later):
            await db.refresh(u)
        db.add(Membership(user_id=bot.id, org_id=org.id, role="member", token_hash="a"))
        db.add(Membership(user_id=human.id, org_id=org.id, role="owner", token_hash="b"))
        db.add(Membership(user_id=later.id, org_id=org.id, role="member", token_hash="c"))
        await db.commit()
        assert await adsconv.human_owner_emails(db, {org.id}) == {org.id: "Creator@Example.com"}


async def test_drain_uploads_the_hashed_email_for_an_unattributed_team(clients, user_data_enabled):
    await reset_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
        r = await verified_signup(c, json={"email": "lead@example.com"})
        org_id = r.json()["org_id"]
    fake = FakeAdsClient(FakeAdsResponse({"requestId": "req-1"}))
    async with session_maker() as db:
        result = await adsconv.drain_once(db, fake)
    assert result["sent"] == 1, result
    event = fake.calls[0][1]["json"]["events"][0]
    assert "adIdentifiers" not in event
    assert event["userData"]["userIdentifiers"] == [{"emailAddress": adsconv.hash_email("lead@example.com")}]
    assert "lead@example.com" not in json.dumps(fake.calls[0][1]["json"])
    async with session_maker() as db:
        row = (await db.execute(select(AdConversion).where(AdConversion.org_id == org_id))).scalars().one()
        assert row.uploaded_at is not None


async def test_drain_dead_letters_a_row_with_neither_click_nor_human(clients, user_data_enabled):
    async with session_maker() as db:
        org = Org(name="t", slug="t-nobody")  # no members at all, no click id
        db.add(org); await db.commit(); await db.refresh(org)
        db.add(AdConversion(org_id=org.id, action=adsconv.ACTION_SIGNUP)); await db.commit()
        fake = FakeAdsClient(FakeAdsResponse({"requestId": "req-1"}))
        result = await adsconv.drain_once(db, fake)
        assert result["sent"] == 0 and result["failed"] == 1
        assert fake.calls == []
        row = (await db.execute(select(AdConversion).where(AdConversion.org_id == org.id))).scalars().one()
        assert row.failed_at is not None and "no human creator email" in row.error


async def test_flag_off_parks_email_only_rows_instead_of_dead_lettering(clients, user_data_enabled, monkeypatch):
    # Queued with the flag on, drained after an operator turns it off: the row must survive for the
    # flag coming back, not be failed for good.
    await reset_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
        org_id = (await verified_signup(c, json={"email": "lead@example.com"})).json()["org_id"]
    monkeypatch.setattr(user_data_enabled, "ads_conv_user_data", False)
    fake = FakeAdsClient(FakeAdsResponse({"requestId": "req-1"}))
    async with session_maker() as db:
        result = await adsconv.drain_once(db, fake)
        assert result == {"sent": 0, "failed": 0} and fake.calls == []
        row = (await db.execute(select(AdConversion).where(AdConversion.org_id == org_id))).scalars().one()
        assert row.failed_at is None and row.attempts == 0 and row.next_attempt_at is not None
        row.next_attempt_at = None  # the park expires
        db.add(row); await db.commit()
    monkeypatch.setattr(user_data_enabled, "ads_conv_user_data", True)
    async with session_maker() as db:
        assert (await adsconv.drain_once(db, fake))["sent"] == 1


async def test_terms_not_accepted_keeps_retrying_instead_of_dead_lettering(clients, user_data_enabled):
    # Google's exact refusal (validateOnly, 2026-09-21) until an operator accepts the Customer Data
    # Terms in the Ads UI. It is "not yet", not "never": the rows must outlive the attempt ceiling.
    await reset_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
        org_id = (await verified_signup(c, json={"email": "lead@example.com"})).json()["org_id"]
    async with session_maker() as db:
        row = (await db.execute(select(AdConversion).where(AdConversion.org_id == org_id))).scalars().one()
        row.attempts = 50  # far past _MAX_ATTEMPTS
        db.add(row); await db.commit()
    refusal = _rejected(_field_violation(0, "INVALID_ARGUMENT",
        "The destination account hasn't agreed to the terms for enhanced conversions."))
    fake = FakeAdsClient(FakeAdsResponse(refusal, status_code=400))
    async with session_maker() as db:
        result = await adsconv.drain_once(db, fake)
        assert result["retried"] == 1 and result["failed"] == 0, result
        row = (await db.execute(select(AdConversion).where(AdConversion.org_id == org_id))).scalars().one()
        assert row.failed_at is None and row.next_attempt_at is not None
        assert "terms for enhanced conversions" in row.error


# ---- the browser tag: advanced consent mode + the web signup action ------------------------------

_WEB = Path(adsconv.__file__).parent / "web"


def test_gtag_queues_denied_consent_defaults_before_loading_googles_script():
    js = (_WEB / "gtag.js").read_text()
    # Advanced consent mode: the defaults are pushed BEFORE the tag script is appended and before
    # config, otherwise Google treats the first hits as consented and sets cookies.
    assert js.index("'consent', 'default'") < js.index("googletagmanager.com/gtag/js") < js.index("'config', TAG_ID")
    for signal in ("ad_storage", "ad_user_data", "ad_personalization", "analytics_storage"):
        assert f"{signal}: 'denied'" in js
    assert "window.tregSignupConversion = function" in js
    assert "AW-18392771132/0usqCIeQrO0cELzUrcJE" in js.replace("TAG_ID + '/", "AW-18392771132/")
    assert "transaction_id: tid" in js and "'treg-web-signup-'" in js


def test_dashboard_loads_the_tag_conversion_only_and_fires_on_first_team():
    front = Path(adsconv.__file__).resolve().parents[2] / "frontend"
    html = (front / "index.html").read_text()
    tag = '<script src="/gtag.js" data-conversion-only defer></script>'
    assert tag in html
    # A classic script before the module entry: gtag.js reads its own attribute through
    # document.currentScript, which is null inside a module, and it must follow ad-click capture.
    assert html.index('<script src="/adtrack.js" defer></script>') < html.index(tag) \
        < html.index('<script type="module"')
    # The first run fires it right after the team the server made for the account exists, keyed by
    # the new org id.
    welcome = (front / "src" / "state" / "onboarding.js").read_text()
    assert "window.tregSignupConversion(o.org_id||o.org)" in welcome
    assert welcome.index("this.api('/onboard/team'") < welcome.index("if(!o.created) return;") \
        < welcome.index("window.tregSignupConversion(")

@pytest.mark.parametrize('door', ['start', 'verify', 'google', 'github'])
@pytest.mark.parametrize('cookie,enabled,expected', [
    ('wbraid|PHONE|landing', True, True), ('', True, False),
    ('gclid||landing', True, False), ('junk', True, False),
    ('gclid|PHONE|landing', False, False),
])
async def test_auth_stashes_click(clients, ads_enabled, monkeypatch, door, cookie, enabled, expected):
    from treg.application import auth
    from treg.models import AdClickStash
    monkeypatch.setattr(adsconv, 'enabled', lambda: enabled)
    email = 'stash@example.com'

    async def start(*args, **kwargs):
        return {'ok': True}

    async def verify(*args, **kwargs):
        return SimpleNamespace(email=email, token='test', session_cookie='test')

    async def social(*args, **kwargs):
        return SimpleNamespace(user=User(id=1, email=email), cli_state=None)

    monkeypatch.setattr(auth, 'start_email_login', start)
    monkeypatch.setattr(auth, 'verify_email_login', verify)
    monkeypatch.setattr(auth, 'complete_google_login', social)
    monkeypatch.setattr(auth, 'complete_github_login', social)
    clients.cookies.set('treg_ad', cookie)
    if door in ('start', 'verify'):
        response = await clients.post('/auth/email/' + door, json={'email': ' STASH@example.com ', 'code': '123456'})
    else:
        response = await clients.get('/auth/' + door + '/callback')
    assert response.status_code in (200, 302), response.text
    async with session_maker() as db:
        stash = await db.get(AdClickStash, email)
        # /start has not proven the email yet, so it never stashes.
        expected = expected and door != 'start'
        assert (stash is not None) == expected
        if expected:
            from treg.application.signup import _ad_attribution_from
            assert _ad_attribution_from(stash.raw_cookie) == ('wbraid', 'PHONE', 'landing')


@pytest.mark.parametrize('cookie,age,expected', [('', 0, 'LATEST'), ('gclid|BROWSER|here', 0, 'BROWSER'), ('', 91, None)])
async def test_signup_stash_fallback(clients, ads_enabled, cookie, age, expected):
    from treg.application.signup import stash_ad_click
    from treg.models import AdClickStash
    email = 'tim@superdesign.dev'
    await stash_ad_click(email.upper(), 'gclid|OLD|original')
    await stash_ad_click(' ' + email + ' ', 'gbraid|LATEST|original')
    async with session_maker() as db:
        stash = await db.get(AdClickStash, email)
        stash.updated_at = utcnow_naive() - timedelta(days=age)
        db.add(stash)
        await db.commit()
    clients.cookies.set('treg_ad', cookie)
    response = await clients.post('/orgs', json={'name': 'cross device'})
    assert response.status_code == 200, response.text
    async with session_maker() as db:
        org = await db.get(Org, response.json()['org_id'])
        assert org.ad_gclid == expected
        if expected == 'LATEST':
            assert org.ad_click_id_type == 'gbraid'
            assert org.ad_landing == 'original'
        # Once a team holds a click, the email association is deleted; an unused expired row
        # waits for the purge.
        assert (await db.get(AdClickStash, email) is None) == (expected is not None)


async def test_login_redirect_preserves_click(clients):
    response = await clients.get('/login?gclid=PHONE&utm_content=original')
    assert response.headers['location'] == '/app?gclid=PHONE&utm_content=original'
    from treg.routers.auth import _login_page_html
    assert '<script src="/adtrack.js" defer></script>' in _login_page_html('a' * 32, session_email=None, github=True, google=True)


async def test_stash_caps_fields_and_disabled_lookup(clients, ads_enabled, monkeypatch):
    from treg.application.signup import _ad_attribution_from, _ad_cookie_for, stash_ad_click
    from treg.models import AdClickStash
    await stash_ad_click('a.b+tag@gmail.com', 'wbraid|' + 'x' * 400 + '|' + '😀' * 100)
    async with session_maker() as db:
        stash = await db.get(AdClickStash, 'a.b+tag@gmail.com')
        assert _ad_attribution_from(stash.raw_cookie) == ('wbraid', 'x' * 255, '😀' * 64)
        assert len(stash.raw_cookie) <= 4096
        assert await _ad_cookie_for(db, 'ab@gmail.com', '') == ''
        monkeypatch.setattr(adsconv, 'enabled', lambda: False)
        assert await _ad_cookie_for(db, 'a.b+tag@gmail.com', '') == ''


@pytest.mark.parametrize('cookie,parsed', [
    ('gclid|ID|a|b', ('gclid', 'ID', 'a|b')),      # a "|" inside the landing keeps the click
    ('ID|landing', ('gclid', 'ID', 'landing')),     # legacy two-part cookie
    ('junk', ('', '', '')), ('x|ID|landing', ('', '', '')),
])
def test_ad_cookie_landing_may_contain_a_pipe(ads_enabled, cookie, parsed):
    from treg.application.signup import _ad_attribution_from
    assert _ad_attribution_from(cookie) == parsed


async def test_purge_deletes_only_expired_stash_rows(clients, ads_enabled):
    from treg.application.signup import purge_expired_ad_clicks, stash_ad_click
    from treg.models import AdClickStash
    for email, age in (('fresh@example.com', 89), ('old@example.com', 91)):
        await stash_ad_click(email, 'gclid|CLICK|landing')
        async with session_maker() as db:
            stash = await db.get(AdClickStash, email)
            stash.updated_at = utcnow_naive() - timedelta(days=age)
            db.add(stash)
            await db.commit()
    assert await purge_expired_ad_clicks() == {'deleted': 1}
    async with session_maker() as db:
        assert await db.get(AdClickStash, 'fresh@example.com') is not None
        assert await db.get(AdClickStash, 'old@example.com') is None
    assert await purge_expired_ad_clicks() == {'deleted': 0}


async def test_stash_failure_does_not_break_signin(clients, ads_enabled, monkeypatch, caplog):
    from treg.application import auth, signup

    async def succeeded(*args, **kwargs):
        return SimpleNamespace(email='private@example.com', token='test', session_cookie='test')

    def broken_session():
        raise RuntimeError('SQL parameters: private@example.com PRIVATE_CLICK')

    monkeypatch.setattr(auth, 'verify_email_login', succeeded)
    monkeypatch.setattr(signup, 'session_maker', broken_session)
    clients.cookies.set('treg_ad', 'gclid|PRIVATE_CLICK|original')
    response = await clients.post('/auth/email/verify', json={'email': 'private@example.com', 'code': '123456'})
    assert response.status_code == 200, response.text
    assert 'ad click stash failed; sign-in continues' in caplog.text
    assert 'private@example.com' not in caplog.text
    assert 'PRIVATE_CLICK' not in caplog.text
