"""Several treg-owned accounts behind one provider (`TREG_PLATFORM_ACCOUNTS`, application/call/keyslots.py):
platform calls spread across them, objects stay on the account that created them, and every
account's credential is masked."""

from __future__ import annotations

import json

import httpx
import pytest
from httpx import AsyncClient

from treg import api as A, oauth_providers
from treg.application import asynctasks as task_app
from treg.application.call import keyslots
from treg.application.call.evidence import _secret_renderings
from treg.application.call.types import GatewayFailed, ResolutionFailed, UpstreamResponse
from treg.config import PRIMARY_SLOT, Settings, get_settings
from treg.infra.upstream.limiter import limiter
from treg.models import AsyncTaskRecord, Tool
from treg.timeutil import utcnow_naive

from test_marketplace_call import EP, _dropleads_response, platform_on  # noqa: F401

TIKHUB_B = "PLATFORM-TIKHUB-KEY-B"
ACCOUNTS = {"tikhub": {"b": {"platform_key_tikhub": TIKHUB_B}}}


@pytest.fixture
def two_tikhub_accounts(platform_on, monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_ACCOUNTS", json.dumps(ACCOUNTS))
    get_settings.cache_clear()


def _settings(**env) -> Settings:
    return Settings(_env_file=None, **env)


# ---- configuration ------------------------------------------------------------------------------

def test_the_primary_account_comes_first_and_labels_name_the_rest():
    s = _settings(platform_key_tomba="T1", platform_key_tomba_secret="S1", platform_accounts=json.dumps(
        {"tomba": {"b": {"platform_key_tomba": "T2", "platform_key_tomba_secret": "S2"}}}))
    slots = s.platform_slots("tomba")
    assert [x.slot for x in slots] == [PRIMARY_SLOT, "b"]
    assert s.platform_credential("platform_key_tomba_secret", "tomba/b") == "S2"
    assert s.platform_credential("platform_key_tomba_secret", None) == "S1", "the primary is the fields"
    assert s.platform_credential("platform_key_tomba", "tomba/gone") == "", "never another account's"
    assert set(s.platform_credential_values("platform_key_tomba")) == {"T1", "T2"}


def test_extra_accounts_without_a_primary_key_serve_nothing():
    s = _settings(platform_accounts=json.dumps({"tikhub": {"b": {"platform_key_tikhub": "X"}}}))
    assert s.platform_slots("tikhub") == ()


@pytest.mark.parametrize("raw", [
    "[]",
    '{"tikhub": [{"platform_key_tikhub": "x"}]}',
    '{"tikhub": {"primary": {"platform_key_tikhub": "x"}}}',
    '{"tikhub": {"B": {"platform_key_tikhub": "x"}}}',
    '{"tikhub": {"b": {"token": "x"}}}',
    '{"tikhub": {"b": {"platform_key_tikhub": ""}}}',
    '{"tikhub": {"b": {"platform_key_hunter": "x"}}}',
    '{"tikhub": {"b": {"platform_key_tikhub": "x"}}, "Tikhub": {"c": {"platform_key_tikhub": "y"}}}',
])
def test_a_malformed_accounts_value_refuses_to_start(raw):
    with pytest.raises(ValueError):
        _settings(platform_accounts=raw)


@pytest.mark.parametrize("accounts, refused", [
    ({"tomba": {"b": {"platform_key_tomba": "T2", "platform_key_tomba_secret": "S2"}}}, None),
    ({"tomba": {"b": {"platform_key_tomba": "T2"}}}, "platform_key_tomba_secret"),
    ({"nosuchprovider": {"b": {"platform_key_nosuchprovider": "Y"}}}, "no platform key"),
    ({"hunter": {"b": {"platform_key_hunter": "H2"}}}, "TREG_PLATFORM_KEY_HUNTER is not set"),
])
def test_an_account_the_call_path_cannot_use_refuses_to_start(monkeypatch, accounts, refused):
    monkeypatch.setenv("TREG_PLATFORM_ACCOUNTS", json.dumps(accounts))
    monkeypatch.setenv("TREG_PLATFORM_KEY_TOMBA", "T1")
    monkeypatch.setenv("TREG_PLATFORM_KEY_TOMBA_SECRET", "S1")
    get_settings.cache_clear()
    try:
        if refused is None:
            keyslots.check_accounts()
        else:
            with pytest.raises(RuntimeError, match=refused):
                keyslots.check_accounts()
    finally:
        get_settings.cache_clear()


# ---- choosing an account ------------------------------------------------------------------------

def test_an_owner_pins_its_account_and_a_retired_one_is_refused(two_tikhub_accounts):
    def pinned(**kw):
        return keyslots.pinned("tikhub", **kw)

    assert pinned(owners=("b",)) == "b"
    assert pinned(owners=(None,)) == PRIMARY_SLOT, "a row from before pools is the primary's"
    assert pinned() is None, "no owner, no key: any account, picked before the relay"
    with pytest.raises(ResolutionFailed) as gone:
        pinned(owners=("c",))
    assert gone.value.status_code == 410 and "nothing was charged" in str(gone.value.detail)
    with pytest.raises(ResolutionFailed) as split:
        pinned(owners=("b", None))
    assert split.value.kind == "platform_account_split" and split.value.status_code == 409


def test_an_idempotency_key_always_lands_on_the_same_account(two_tikhub_accounts):
    picks = {keyslots.pinned("tikhub", idempotency_key=f"1\x1fk{i}") for i in range(40)}
    assert picks == {PRIMARY_SLOT, "b"}, "keys spread across accounts"
    for i in range(5):
        key = f"1\x1fretry-{i}"
        assert len({keyslots.pinned("tikhub", idempotency_key=key) for _ in range(5)}) == 1


def test_the_primary_account_keeps_the_providers_own_binding_and_bucket(two_tikhub_accounts):
    provider = oauth_providers.get("tikhub")
    assert all("account" not in b for b in oauth_providers.platform_bindings(provider, PRIMARY_SLOT))
    assert keyslots.rate_bucket("tikhub", EP, PRIMARY_SLOT)[0] == "tikhub"
    assert keyslots.rate_bucket("tikhub", EP, "b")[0] == "tikhub#b"


async def test_platform_calls_spread_across_the_accounts(clients: AsyncClient, two_tikhub_accounts,
                                                         monkeypatch, posthog_events):
    seen: list[str] = []

    def upstream(request):
        seen.append(request.headers["authorization"])
        return _dropleads_response(200, {"comments": []})

    async with AsyncClient(transport=httpx.MockTransport(upstream)) as vendor:
        monkeypatch.setattr(A.app.state, "http", vendor)
        for i in range(4):
            r = await clients.get(f"/call/{EP}?aweme_id={i}")
            assert r.status_code == 200, r.text
    assert sorted(seen) == sorted(["Bearer PLATFORM-TIKHUB-KEY", f"Bearer {TIKHUB_B}"] * 2)
    slots = [e["properties"]["key_slot"] for e in await posthog_events()]
    assert sorted(slots) == sorted([PRIMARY_SLOT, PRIMARY_SLOT, "b", "b"])


async def test_a_busy_account_hands_the_next_call_to_an_idle_one(clients: AsyncClient, two_tikhub_accounts):
    from test_capacity_smoothing import _publish_rate
    from treg.domain.capacity.view import view as capacity_view
    await _publish_rate("tikhub", 1, 60.0)  # slow enough that the spent token cannot refill mid-test
    await capacity_view.load()
    await limiter.acquire(*keyslots.rate_bucket("tikhub", EP, PRIMARY_SLOT))
    for _ in range(4):  # the primary's token is spent; "b" has one
        assert _assigned() == "b"


def _assigned() -> str:
    from treg.application.call.resolve import MarketplaceCall
    mk = MarketplaceCall(tool=Tool(org_id=1, name=EP, owner="t", base_url="", host="", bindings=[]),
                         upstream="", consumed=set(), endpoint_id=EP, provider="tikhub", tier="platform")
    keyslots.assign(mk)
    return mk.key_slot


def test_ties_rotate_per_provider(two_tikhub_accounts):
    """Another provider's calls interleaving must not pin this one to the same account."""
    picks = []
    for _ in range(6):
        picks.append(_assigned())
        next(keyslots._rotation.setdefault("hunter", __import__("itertools").count()))
    assert set(picks) == {PRIMARY_SLOT, "b"}


async def test_an_account_out_of_capacity_is_skipped_and_kept_out(clients: AsyncClient, two_tikhub_accounts,
                                                                monkeypatch):
    """Account b runs dry: its lock is its own, the primary keeps serving, and the primary's 2xx
    does not clear b's strikes."""
    from treg.domain.capacity import marks
    from treg.domain.capacity.view import view as capacity_view
    for _ in range(2):
        await marks.strike("tikhub", endpoint_id=EP, kind="balance", resets_at=None, slot="b",
                           immediate=True)
    await capacity_view.load(force=True)
    assert keyslots.exhausted("tikhub", EP, "b") and not keyslots.exhausted("tikhub", EP, PRIMARY_SLOT)
    seen: list[str] = []

    def upstream(request):
        seen.append(request.headers["authorization"])
        return _dropleads_response(200, {"comments": []})

    async with AsyncClient(transport=httpx.MockTransport(upstream)) as vendor:
        monkeypatch.setattr(A.app.state, "http", vendor)
        for i in range(3):
            assert (await clients.get(f"/call/{EP}?aweme_id={i}")).status_code == 200
    assert set(seen) == {"Bearer PLATFORM-TIKHUB-KEY"}
    await capacity_view.load(force=True)
    assert keyslots.exhausted("tikhub", EP, "b"), "the primary's answers are not b's recovery"


def test_a_malformed_accounts_value_never_echoes_a_credential():
    from pydantic import ValidationError
    with pytest.raises(ValidationError) as bad:
        _settings(platform_accounts='{"tikhub": {"B": {"platform_key_tikhub": "sk_test_123"}}}')
    assert "sk_test_123" not in str(bad.value)
    assert "sk_test_123" not in repr(_settings(
        platform_key_tikhub="P", platform_accounts=json.dumps(ACCOUNTS)).platform_slots("tikhub"))


async def test_a_burst_arriving_together_spreads_over_the_accounts(
    clients: AsyncClient, two_tikhub_accounts, monkeypatch,
):
    """Every call of a burst resolves before any reaches the relay; the account is picked where its
    token is taken, so the burst does not all read the same idle account."""
    import asyncio
    seen: list[str] = []

    def upstream(request):
        seen.append(request.headers["authorization"])
        return _dropleads_response(200, {"comments": []})

    from test_capacity_smoothing import _publish_rate
    await _publish_rate("tikhub", 4, 1.0)
    async with AsyncClient(transport=httpx.MockTransport(upstream)) as vendor:
        monkeypatch.setattr(A.app.state, "http", vendor)
        assert (await clients.get(f"/call/{EP}?aweme_id=warm")).status_code == 200
        seen.clear()
        limiter.reset()
        # The primary is busy and "b" idle as the burst resolves: read at resolution, every call
        # would pick "b" and queue there while the primary sat unused.
        await limiter.acquire(*keyslots.rate_bucket("tikhub", EP, PRIMARY_SLOT))
        rs = await asyncio.gather(*(clients.get(f"/call/{EP}?aweme_id={i}") for i in range(6)))
    assert [r.status_code for r in rs] == [200] * 6
    assert seen.count("Bearer PLATFORM-TIKHUB-KEY") >= 2 and seen.count(f"Bearer {TIKHUB_B}") >= 2


# ---- objects stay on their account --------------------------------------------------------------

async def test_an_async_task_is_polled_on_the_account_that_accepted_it(two_tikhub_accounts, monkeypatch):
    seen = {}

    async def fake_relay(request, url, tool, *args, **kwargs):
        seen["account"] = [b.get("account") for b in tool.bindings]

        async def stream():
            yield b'{"status": "Success"}'

        async def close():
            return None

        return UpstreamResponse(200, (), stream(), close)

    monkeypatch.setattr(task_app, "relay", fake_relay)

    def row(slot):
        return AsyncTaskRecord(
            call_id="q-1", org_id=1, provider="tikhub", endpoint_id=EP, task_id="t-1",
            reserved_micro=1, next_check_at=utcnow_naive(), key_slot=slot,
            descriptor={"poll": {"endpoint": EP, "param": {"in": "queryParams", "name": "aweme_id"}}})

    await task_app._poll(row("b"), None)
    assert set(seen["account"]) == {"tikhub/b"}
    await task_app._poll(row(None), None)
    assert set(seen["account"]) == {None}, "the primary injects the deployment's own field"
    with pytest.raises(ResolutionFailed):
        await task_app._poll(row("retired"), None)


async def test_the_relay_refuses_an_account_that_is_not_configured(two_tikhub_accounts):
    from treg.application.call.types import UpstreamRequest
    from treg.infra.upstream.relay import relay
    provider = oauth_providers.get("tikhub")
    tool = Tool(org_id=1, name=EP, owner="t", base_url=provider.base_url, host="api.tikhub.io",
                bindings=[{**b, "account": "tikhub/retired"} for b in oauth_providers.platform_bindings(provider)])

    async def empty():
        if False:
            yield b""

    request = UpstreamRequest(method="GET", raw_headers=(), query_items=(), body_stream=empty,
                              has_body=False)
    with pytest.raises(GatewayFailed) as failed:
        await relay(request, provider.base_url + "/x", tool, [], httpx.AsyncClient())
    assert failed.value.kind == "injection_failed"


def test_error_bodies_mask_every_accounts_credential(two_tikhub_accounts):
    provider = oauth_providers.get("tikhub")
    tool = Tool(org_id=1, name=EP, owner="t", base_url=provider.base_url, host="api.tikhub.io",
                bindings=oauth_providers.platform_bindings(provider, "b"))
    renderings = _secret_renderings(tool, {})
    assert TIKHUB_B in renderings and "PLATFORM-TIKHUB-KEY" in renderings


async def test_concurrency_places_are_per_account_and_a_full_one_is_avoided(
    clients: AsyncClient, two_tikhub_accounts, monkeypatch,
):
    """The primary's only place is taken by another team: the call goes to b instead of waiting
    on, or being refused by, a full account."""
    from treg.domain.capacity.fair_share import fair_share
    from treg.domain.capacity.view import view as capacity_view
    monkeypatch.setattr(capacity_view, "concurrency", lambda provider: 1 if provider == "tikhub" else 0)
    monkeypatch.setenv("TREG_FAIR_SHARE_WAIT_S", "0.2")
    get_settings.cache_clear()
    fair_share.reset()
    seen: list[str] = []

    def upstream(request):
        seen.append(request.headers["authorization"])
        return _dropleads_response(200, {"comments": []})

    other_team = await fair_share.acquire("tikhub", -1, 1)
    try:
        async with AsyncClient(transport=httpx.MockTransport(upstream)) as vendor:
            monkeypatch.setattr(A.app.state, "http", vendor)
            for i in range(3):
                assert (await clients.get(f"/call/{EP}?aweme_id={i}")).status_code == 200
    finally:
        other_team.release()
        fair_share.reset()
    assert seen == [f"Bearer {TIKHUB_B}"] * 3


def test_every_process_does_not_start_its_rotation_on_the_primary(two_tikhub_accounts):
    """Each web worker's first call on a provider: with every counter starting at 0, all of them
    went to the primary and low traffic across several workers never reached account b."""
    firsts = set()
    for _ in range(40):
        keyslots._rotation.clear()  # a fresh process
        firsts.add(_assigned())
    assert firsts == {PRIMARY_SLOT, "b"}
