"""Fish Audio shared-key tenancy, lifecycle and pricing contracts."""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from treg import audit
from treg.application.call import resolve as call_resolution
from treg.application.call import service as call_service
from treg.application.call.types import UpstreamResponse
from treg.config import get_settings
from treg.domain import provider_resources
from treg.infra.db import _engine, session_maker
from treg.models import ProviderResource


@pytest.fixture
def fishaudio_platform_on(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_FISHAUDIO", "PLATFORM-FISH-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "fishaudio")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _response(status: int, body: bytes = b"{}", headers: tuple = ()):
    async def stream():
        yield body

    async def close():
        return None

    return UpstreamResponse(status, headers, stream(), close)


async def _org_id(clients: AsyncClient) -> int:
    return (await clients.get("/orgs")).json()[0]["org_id"]


async def _add_voice(clients: AsyncClient, voice_id: str, name: str = "Narrator") -> None:
    async with session_maker() as db:
        db.add(ProviderResource(
            org_id=await _org_id(clients), provider="fishaudio", resource_kind="voice",
            upstream_id=voice_id, display_name=name, created_by="tim@superdesign.dev",
            source_call_id="call-test",
        ))
        await db.commit()


async def test_voice_create_is_private_and_becomes_an_org_resource(
    clients: AsyncClient, fishaudio_platform_on, monkeypatch,
):
    seen = []

    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        seen.append((request.method, upstream_url))
        return _response(201, b'{"_id":"voice-created","title":"Narrator"}')

    monkeypatch.setattr(call_service, "relay", relay)
    response = await clients.post(
        "/call/fishaudio.voices.create",
        data={"type": "tts", "title": "Narrator", "train_mode": "fast", "visibility": "private"},
        files=[("voices", ("voice.wav", b"RIFF-test", "audio/wav"))],
    )
    assert response.status_code == 201, response.text
    listed = await clients.get(
        f"/orgs/{await _org_id(clients)}/provider-resources?provider=fishaudio&kind=voice"
    )
    assert listed.headers["X-Treg-Resource-Source"] == "platform"
    rows = listed.json()
    assert [(row["upstream_id"], row["display_name"], row["status"]) for row in rows] == [
        ("voice-created", "Narrator", "active")
    ]
    assert seen == [("POST", "https://api.fish.audio/model")]


async def test_unified_voice_list_uses_byok_and_normalizes_without_holding_db(
    clients: AsyncClient, fishaudio_platform_on, monkeypatch,
):
    await clients.post("/secrets", json={"name": "fishaudio", "value": "OWN-FISH-KEY"})
    await audit.drain()
    checked_out = []

    async def relay(*args, **kwargs):
        checked_out.append(_engine.pool.checkedout())
        return _response(200, b'{"items":[{"_id":"account-voice","title":"Account narrator",'
                              b'"state":"created","created_at":"2026-09-22T00:00:00Z"}]}')

    monkeypatch.setattr(call_service, "relay", relay)
    response = await clients.get(
        f"/orgs/{await _org_id(clients)}/provider-resources?provider=fishaudio&kind=voice"
    )
    assert response.status_code == 200, response.text
    assert response.headers["X-Treg-Resource-Source"] == "byok"
    assert response.json() == [{
        "id": "account-voice",
        "provider": "fishaudio",
        "kind": "voice",
        "upstream_id": "account-voice",
        "display_name": "Account narrator",
        "created_by": None,
        "source_call_id": None,
        "status": "active",
        "created_at": "2026-09-22T00:00:00Z",
        "updated_at": None,
        "deleted_at": None,
    }]
    assert checked_out == [0]


async def test_unified_voice_list_follows_fish_pagination(
    clients: AsyncClient, fishaudio_platform_on, monkeypatch,
):
    await clients.post("/secrets", json={"name": "fishaudio", "value": "OWN-FISH-KEY"})
    seen_pages = []

    async def relay(request, *args, **kwargs):
        page = dict(request.query_items)["page_number"]
        seen_pages.append(page)
        body = (
            b'{"items":[{"_id":"voice-one","title":"One"}],"has_more":true}'
            if page == "1" else
            b'{"items":[{"_id":"voice-two","title":"Two"}],"has_more":false}'
        )
        return _response(200, body)

    monkeypatch.setattr(call_service, "relay", relay)
    response = await clients.get(
        f"/orgs/{await _org_id(clients)}/provider-resources?provider=fishaudio&kind=voice"
    )
    assert response.status_code == 200, response.text
    assert [row["upstream_id"] for row in response.json()] == ["voice-one", "voice-two"]
    assert seen_pages == ["1", "2"]


async def test_platform_resource_source_never_substitutes_byok_account_rows(
    clients: AsyncClient, fishaudio_platform_on, monkeypatch,
):
    await _add_voice(clients, "team-voice", "Team narrator")
    await clients.post("/secrets", json={"name": "fishaudio", "value": "OWN-FISH-KEY"})
    called = False

    async def relay(*args, **kwargs):
        nonlocal called
        called = True
        return _response(200, b'{"items":[{"_id":"account-voice"}]}')

    monkeypatch.setattr(call_service, "relay", relay)
    response = await clients.get(
        f"/orgs/{await _org_id(clients)}/provider-resources?source=platform"
    )
    assert response.status_code == 200, response.text
    assert response.headers["X-Treg-Resource-Source"] == "platform"
    assert [row["upstream_id"] for row in response.json()] == ["team-voice"]
    assert called is False


async def test_voice_create_holds_no_db_connection_during_fish_io(
    clients: AsyncClient, fishaudio_platform_on, monkeypatch,
):
    await audit.drain()
    checked_out = []

    async def relay(*args, **kwargs):
        checked_out.append(_engine.pool.checkedout())
        return _response(201, b'{"_id":"voice-no-held-session"}')

    monkeypatch.setattr(call_service, "relay", relay)
    response = await clients.post(
        "/call/fishaudio.voices.create",
        data={"type": "tts", "title": "No held session", "train_mode": "fast",
              "visibility": "private"},
        files=[("voices", ("voice.wav", b"RIFF-test", "audio/wav"))],
    )
    assert response.status_code == 201, response.text
    assert checked_out == [0]


async def test_platform_voice_create_rejects_non_private_before_relay(
    clients: AsyncClient, fishaudio_platform_on, monkeypatch,
):
    called = False

    async def relay(*args, **kwargs):
        nonlocal called
        called = True
        return _response(201, b'{"_id":"must-not-exist"}')

    monkeypatch.setattr(call_service, "relay", relay)
    response = await clients.post(
        "/call/fishaudio.voices.create",
        data={"type": "tts", "title": "Public", "train_mode": "fast", "visibility": "public"},
        files=[("voices", ("voice.wav", b"RIFF-test", "audio/wav"))],
    )
    assert response.status_code == 400
    assert called is False


async def test_unknown_and_cross_org_voice_ids_are_identical_403_without_upstream(
    clients: AsyncClient, fishaudio_platform_on, monkeypatch,
):
    await _add_voice(clients, "voice-team-one")
    called = 0

    async def relay(*args, **kwargs):
        nonlocal called
        called += 1
        return _response(200)

    monkeypatch.setattr(call_service, "relay", relay)
    unknown = await clients.patch(
        "/call/fishaudio.voices.update?id=unknown",
        json={"title": "Nope", "visibility": "private"})
    other = (await clients.post("/orgs", json={"name": "Other Fish Team"})).json()
    clients.headers["X-Treg-Token"] = other["token"]
    clients.headers.pop("X-Treg-Org", None)
    cross_org = await clients.patch(
        "/call/fishaudio.voices.update?id=voice-team-one",
        json={"title": "Nope", "visibility": "private"})
    assert unknown.status_code == cross_org.status_code == 403
    assert unknown.json()["detail"] == cross_org.json()["detail"]
    assert called == 0


async def test_cross_org_tts_voice_is_denied_without_public_lookup(
    clients: AsyncClient, fishaudio_platform_on, monkeypatch,
):
    await _add_voice(clients, "private-team-one")
    other = (await clients.post("/orgs", json={"name": "Other Fish TTS Team"})).json()
    clients.headers["X-Treg-Token"] = other["token"]
    clients.headers.pop("X-Treg-Org", None)
    called = False

    async def relay(*args, **kwargs):
        nonlocal called
        called = True
        return _response(200)

    monkeypatch.setattr(call_service, "relay", relay)
    response = await clients.post(
        "/call/fishaudio.tts.s2-1-pro",
        headers={"model": "s2.1-pro"},
        json={"text": "Must stay private", "reference_id": "private-team-one"},
    )
    assert response.status_code == 403
    assert called is False


async def test_tts_checks_every_voice_in_an_array(
    clients: AsyncClient, fishaudio_platform_on, monkeypatch,
):
    await _add_voice(clients, "voice-a")
    calls = []

    async def relay(request, upstream_url, *args, **kwargs):
        calls.append((request.method, upstream_url))
        return _response(200, b'{"_id":"not-ours","visibility":"private","licensed":false}')

    monkeypatch.setattr(call_service, "relay", relay)
    response = await clients.post(
        "/call/fishaudio.tts.s2-1-pro",
        headers={"model": "s2.1-pro"},
        json={"text": "Two voices", "reference_id": ["voice-a", "not-ours"]},
    )
    assert response.status_code == 403
    assert calls == [("GET", "https://api.fish.audio/model/not-ours")]


async def test_tts_accepts_a_live_verified_public_community_voice_without_holding_db(
    clients: AsyncClient, fishaudio_platform_on, monkeypatch,
):
    await audit.drain()
    calls = []
    checked_out = []

    async def relay(request, upstream_url, *args, **kwargs):
        calls.append((request.method, upstream_url))
        checked_out.append(_engine.pool.checkedout())
        if request.method == "GET":
            return _response(
                200,
                b'{"_id":"public-voice","visibility":"public","licensed":false}',
            )
        return _response(200, b"audio", ((b"content-type", b"audio/mpeg"),))

    monkeypatch.setattr(call_service, "relay", relay)
    response = await clients.post(
        "/call/fishaudio.tts.s2-1-pro",
        headers={"model": "s2.1-pro"},
        json={"text": "Public voice", "reference_id": "public-voice"},
    )
    assert response.status_code == 200, response.text
    assert response.content == b"audio"
    assert calls == [
        ("GET", "https://api.fish.audio/model/public-voice"),
        ("POST", "https://api.fish.audio/v1/tts"),
    ]
    assert checked_out == [0, 0]


@pytest.mark.parametrize("licensed", ["true", "false"])
async def test_public_voice_discovery_is_a_free_platform_action(
    clients: AsyncClient, fishaudio_platform_on, monkeypatch, licensed,
):
    await audit.drain()
    observed = []

    async def relay(request, upstream_url, *args, **kwargs):
        observed.append((upstream_url, dict(request.query_items), _engine.pool.checkedout()))
        return _response(
            200,
            b'{"total":1,"items":[{"_id":"public-voice","visibility":"public",'
            b'"licensed":true}],"has_more":false}',
            ((b"content-type", b"application/json"),),
        )

    monkeypatch.setattr(call_service, "relay", relay)
    response = await clients.get(
        "/call/fishaudio.voices.discover",
        params={"self": "false", "licensed": licensed, "page_size": "3", "page_number": "1"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["_id"] == "public-voice"
    assert observed == [(
        "https://api.fish.audio/model",
        {"self": "false", "licensed": licensed, "page_size": "3", "page_number": "1"},
        0,
    )]


@pytest.mark.parametrize("reference_id", [None, "voice-a", ["voice-a", "voice-b"]])
async def test_tts_accepts_default_scalar_and_owned_array_references(
    clients: AsyncClient, fishaudio_platform_on, monkeypatch, reference_id,
):
    await _add_voice(clients, "voice-a")
    await _add_voice(clients, "voice-b")
    called = 0

    async def relay(*args, **kwargs):
        nonlocal called
        called += 1
        return _response(200, b"audio", ((b"content-type", b"audio/mpeg"),))

    monkeypatch.setattr(call_service, "relay", relay)
    body = {"text": "Owned voice"}
    if reference_id is not None:
        body["reference_id"] = reference_id
    response = await clients.post(
        "/call/fishaudio.tts.s2-1-pro", headers={"model": "s2.1-pro"}, json=body,
    )
    assert response.status_code == 200, response.text
    assert response.content == b"audio"
    assert called == 1


async def test_platform_model_header_must_be_one_unambiguous_value(
    clients: AsyncClient, fishaudio_platform_on, monkeypatch,
):
    called = False

    async def relay(*args, **kwargs):
        nonlocal called
        called = True
        return _response(200, b"audio")

    monkeypatch.setattr(call_service, "relay", relay)
    response = await clients.post(
        "/call/fishaudio.tts.s2-1-pro",
        headers=[("model", "s1"), ("model", "s2.1-pro")],
        json={"text": "ambiguous"},
    )
    assert response.status_code == 400
    assert called is False


async def test_byok_bypasses_platform_voice_ownership(
    clients: AsyncClient, fishaudio_platform_on, monkeypatch,
):
    await clients.post("/secrets", json={"name": "fishaudio", "value": "OWN-FISH-KEY"})
    called = 0

    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        nonlocal called
        called += 1
        return _response(200, b'{"_id":"foreign-account-voice"}')

    monkeypatch.setattr(call_service, "relay", relay)
    response = await clients.patch(
        "/call/fishaudio.voices.update?id=foreign-account-voice", json={"title": "Mine"})
    assert response.status_code == 200, response.text
    assert called == 1


async def test_voice_update_and_delete_change_local_lifecycle_only_after_fish(
    clients: AsyncClient, fishaudio_platform_on, monkeypatch,
):
    await _add_voice(clients, "voice-life", "Old name")
    statuses = iter((200, 404, 404))

    async def relay(*args, **kwargs):
        return _response(next(statuses))

    monkeypatch.setattr(call_service, "relay", relay)
    updated = await clients.patch(
        "/call/fishaudio.voices.update?id=voice-life",
        json={"title": "New name", "visibility": "private"},
    )
    deleted = await clients.delete("/call/fishaudio.voices.delete?id=voice-life")
    retried = await clients.delete("/call/fishaudio.voices.delete?id=voice-life")
    assert updated.status_code == 200
    assert deleted.status_code == retried.status_code == 404
    async with session_maker() as db:
        row = (await db.execute(select(ProviderResource).where(
            ProviderResource.upstream_id == "voice-life"
        ))).scalars().one()
        assert row.display_name == "New name"
        assert row.status == provider_resources.DELETED and row.deleted_at is not None


async def test_create_persistence_failure_deletes_the_unmanaged_fish_voice(
    clients: AsyncClient, fishaudio_platform_on, monkeypatch,
):
    calls = []

    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        calls.append((request.method, upstream_url))
        if request.method == "DELETE":
            return _response(204, b"")
        return _response(201, b'{"_id":"orphan-candidate"}')

    async def fail_register(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(call_service, "relay", relay)
    monkeypatch.setattr(provider_resources, "register", fail_register)
    response = await clients.post(
        "/call/fishaudio.voices.create",
        data={"type": "tts", "title": "Unsafe", "train_mode": "fast", "visibility": "private"},
        files=[("voices", ("voice.wav", b"RIFF-test", "audio/wav"))],
    )
    assert response.status_code == 502
    assert calls == [
        ("POST", "https://api.fish.audio/model"),
        ("DELETE", "https://api.fish.audio/model/orphan-candidate"),
    ]


@pytest.mark.parametrize(("text", "expected"), [
    ("abc", 45),
    ("漢字", 90),
    ("🙂", 60),
])
def test_tts_estimate_uses_utf8_bytes(text: str, expected: int):
    cost = {"usd": 15 / 1_000_000, "unit": "utf8_byte"}
    assert call_resolution._platform_estimate_micro(cost, {}, json.dumps({"text": text}).encode()) == expected
