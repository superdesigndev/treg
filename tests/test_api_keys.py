"""Managed API keys keep authentication separate from membership authorization."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from treg import audit, cli, crypto
from treg.api import app
from treg.domain.identity import api_keys as managed_keys
from treg.infra.db import reset_db, session_maker
from treg.models import ApiKey, ApiKeyEvent, Membership, User


def _h(token: str) -> dict[str, str]:
    return {"X-Treg-Token": token}


@pytest.fixture
def sent_otps(monkeypatch):
    """Capture sign-in codes through the email boundary, as a real inbox would."""
    from treg import email as email_mod

    sent = {}

    async def _capture(email: str, code: str, ttl_minutes: int = 10) -> bool:
        sent[email] = code
        return True

    monkeypatch.setattr(email_mod, "send_otp", _capture)
    return sent


async def _start_code(client: AsyncClient, email: str, sent_otps: dict[str, str]) -> str:
    started = await client.post("/auth/email/start", json={"email": email})
    return started.json().get("dev_code") or sent_otps[email]


async def test_new_human_team_has_only_default_key_with_visible_prefix(sent_otps):
    await reset_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as client:
        code = await _start_code(client, "fresh-owner@example.dev", sent_otps)
        identity = (await client.post("/auth/email/verify", json={
            "email": "fresh-owner@example.dev", "code": code,
        })).json()["token"]
        created = await client.post(
            "/orgs", headers=_h(identity), json={"name": "Fresh team"},
        )
        assert created.status_code == 200, created.text
        default_token = created.json()["token"]
        rows = (await client.get(
            f"/orgs/{created.json()['org_id']}/api-keys", headers=_h(default_token),
        )).json()

        assert [row["kind"] for row in rows] == [managed_keys.DEFAULT_KIND]
        assert rows[0]["safe_prefix"] == default_token[:12]
        assert (await client.get("/tools", headers=_h(default_token))).status_code == 200

        invite = (await client.post(
            f"/orgs/{created.json()['org_id']}/invites",
            headers=_h(default_token),
            json={"email": "fresh-member@example.dev", "role": "member"},
        )).json()
        accepted = await client.post("/invites/accept", json={
            "email": "fresh-member@example.dev", "code": invite["code"],
        })
        member_token = accepted.json()["token"]
        member_rows = (await client.get(
            f"/orgs/{created.json()['org_id']}/api-keys", headers=_h(member_token),
        )).json()
        assert [row["kind"] for row in member_rows] == [managed_keys.DEFAULT_KIND]
        assert member_rows[0]["safe_prefix"] == member_token[:12]


async def test_additional_key_is_shown_once_and_keeps_membership_access(clients):
    me = (await clients.get("/auth/me")).json()
    org_id = me["org_id"]
    created = await clients.post(f"/orgs/{org_id}/api-keys", json={"name": "Laptop"})
    assert created.status_code == 200, created.text
    assert created.headers["cache-control"] == "no-store"
    body = created.json()
    secret = body["secret"]
    assert secret.startswith("treg_")
    assert body["safe_prefix"] == secret[:12]

    listed = (await clients.get(f"/orgs/{org_id}/api-keys")).json()
    assert any(row["id"] == body["id"] and row["name"] == "Laptop" for row in listed)
    assert all("secret" not in row and "key_hash" not in row for row in listed)
    assert (await clients.get("/auth/me", headers=_h(secret))).json()["email"] == me["email"]

    async with session_maker() as db:
        row = await db.get(ApiKey, body["id"])
        assert row.key_hash == crypto.hash_token(secret)
        assert secret not in repr(row)


async def test_disable_enable_revoke_cannot_fall_back_to_membership_hash(clients):
    org_id = (await clients.get("/auth/me")).json()["org_id"]
    body = (await clients.post(
        f"/orgs/{org_id}/api-keys", json={"name": "Deploy"},
    )).json()
    secret, key_id = body["secret"], body["id"]

    assert (await clients.post(f"/orgs/{org_id}/api-keys/{key_id}/disable")).status_code == 200
    disabled = await clients.get("/tools", headers=_h(secret))
    assert disabled.status_code == 401 and "disabled" in disabled.text
    assert (await clients.post(f"/orgs/{org_id}/api-keys/{key_id}/enable")).status_code == 200
    assert (await clients.get("/tools", headers=_h(secret))).status_code == 200

    assert (await clients.post(f"/orgs/{org_id}/api-keys/{key_id}/revoke")).status_code == 200
    assert (await clients.get("/tools", headers=_h(secret))).status_code == 401
    assert (await clients.post(f"/orgs/{org_id}/api-keys/{key_id}/enable")).status_code == 409


async def test_member_and_admin_permissions_are_distinct(clients):
    org_id = (await clients.get("/auth/me")).json()["org_id"]
    member_token = crypto.new_token()
    viewer_token = crypto.new_token()
    async with session_maker() as db:
        member = User(email="member@example.dev")
        viewer = User(email="viewer@example.dev")
        db.add(member); db.add(viewer); await db.flush()
        db.add(Membership(user_id=member.id, org_id=org_id, role="member",
                          token_hash=crypto.hash_token(member_token)))
        db.add(Membership(user_id=viewer.id, org_id=org_id, role="viewer",
                          token_hash=crypto.hash_token(viewer_token)))
        await db.commit()

    assert (await clients.post(
        f"/orgs/{org_id}/api-keys", headers=_h(viewer_token), json={"name": "No"},
    )).status_code == 403
    made = await clients.post(
        f"/orgs/{org_id}/api-keys", headers=_h(member_token), json={"name": "Member key"},
    )
    assert made.status_code == 200, made.text
    key_id = made.json()["id"]
    own = (await clients.get(f"/orgs/{org_id}/api-keys", headers=_h(member_token))).json()
    assert own and {row["identity"] for row in own} == {"member@example.dev"}
    all_rows = (await clients.get(f"/orgs/{org_id}/api-keys")).json()
    assert {"member@example.dev", "viewer@example.dev"}.issubset(
        {row["identity"] for row in all_rows})
    assert (await clients.post(
        f"/orgs/{org_id}/api-keys/{key_id}/rotate",
    )).status_code == 403
    assert (await clients.post(
        f"/orgs/{org_id}/api-keys/{key_id}/disable",
    )).status_code == 200


async def test_rotate_is_owner_only_for_agents_and_assignee_only_for_humans(clients):
    org_id = (await clients.get("/auth/me")).json()["org_id"]
    human = (await clients.post(
        f"/orgs/{org_id}/api-keys", json={"name": "CI human"},
    )).json()
    rotated = await clients.post(f"/orgs/{org_id}/api-keys/{human['id']}/rotate")
    assert rotated.status_code == 200
    assert rotated.headers["cache-control"] == "no-store"
    assert (await clients.get("/tools", headers=_h(human["secret"]))).status_code == 401
    assert (await clients.get("/tools", headers=_h(rotated.json()["secret"]))).status_code == 200
    visible = (await clients.get(f"/orgs/{org_id}/api-keys")).json()
    assert human["id"] not in {row["id"] for row in visible}
    hidden = (await clients.get(f"/orgs/{org_id}/api-keys?include_hidden=true")).json()
    assert next(row for row in hidden if row["id"] == human["id"])["deleted_at"] is not None

    agent_response = await clients.post(f"/orgs/{org_id}/agents", json={"name": "worker"})
    assert agent_response.headers["cache-control"] == "no-store"
    agent = agent_response.json()
    keys = (await clients.get(f"/orgs/{org_id}/api-keys")).json()
    agent_key = next(row for row in keys if row["user_id"] == agent["user_id"] and row["state"] == "active")
    replacement = await clients.post(f"/orgs/{org_id}/api-keys/{agent_key['id']}/rotate")
    assert replacement.status_code == 200
    assert replacement.headers["cache-control"] == "no-store"
    assert (await clients.get("/tools", headers=_h(agent["token"]))).status_code == 401
    assert (await clients.get("/tools", headers=_h(replacement.json()["secret"]))).status_code == 200


async def test_database_allows_only_one_current_agent_key(clients):
    org_id = (await clients.get("/auth/me")).json()["org_id"]
    agent = (await clients.post(f"/orgs/{org_id}/agents", json={"name": "one-key"})).json()
    async with session_maker() as db:
        membership = (await db.execute(select(Membership).where(
            Membership.user_id == agent["user_id"], Membership.org_id == org_id,
        ))).scalar_one()
        db.add(ApiKey(
            org_id=org_id, membership_id=membership.id, identity_label=agent["email"],
            kind="agent", name="Conflicting key", key_hash=crypto.hash_token("conflict"),
            state="active", created_by="owner",
        ))
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_concurrent_human_rotation_has_one_winner_and_one_replacement(clients):
    org_id = (await clients.get("/auth/me")).json()["org_id"]
    old = (await clients.post(
        f"/orgs/{org_id}/api-keys", json={"name": "Concurrent human"},
    )).json()

    responses = await asyncio.gather(*(
        clients.post(f"/orgs/{org_id}/api-keys/{old['id']}/rotate") for _ in range(2)
    ))
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(response for response in responses if response.status_code == 200)
    assert winner.headers["cache-control"] == "no-store"

    async with session_maker() as db:
        old_row = await db.get(ApiKey, old["id"])
        rows = (await db.execute(select(ApiKey).where(
            ApiKey.membership_id == old_row.membership_id,
            ApiKey.name == "Concurrent human",
        ))).scalars().all()
    assert old_row.state == "revoked" and old_row.replacement_key_id is not None
    assert old_row.deleted_at is not None
    assert [row.state for row in rows].count("active") == 1
    assert len(rows) == 2


async def test_concurrent_agent_rotation_has_one_winner_and_no_500(clients):
    org_id = (await clients.get("/auth/me")).json()["org_id"]
    agent = (await clients.post(
        f"/orgs/{org_id}/agents", json={"name": "concurrent-agent"},
    )).json()
    keys = (await clients.get(f"/orgs/{org_id}/api-keys")).json()
    old = next(row for row in keys if row["user_id"] == agent["user_id"] and row["state"] == "active")

    responses = await asyncio.gather(*(
        clients.post(f"/orgs/{org_id}/api-keys/{old['id']}/rotate") for _ in range(2)
    ))
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(response for response in responses if response.status_code == 200)
    assert winner.headers["cache-control"] == "no-store"

    async with session_maker() as db:
        old_row = await db.get(ApiKey, old["id"])
        rows = (await db.execute(select(ApiKey).where(
            ApiKey.membership_id == old["membership_id"], ApiKey.kind == "agent",
        ))).scalars().all()
    assert old_row.state == "revoked" and old_row.replacement_key_id is not None
    assert old_row.deleted_at is not None
    assert sum(row.state in ("active", "disabled") for row in rows) == 1
    assert len(rows) == 2


async def test_last_used_writer_never_blocks_same_key_authentication(clients, monkeypatch):
    org_id = (await clients.get("/auth/me")).json()["org_id"]
    key = (await clients.post(
        f"/orgs/{org_id}/api-keys", json={"name": "Parallel use"},
    )).json()
    await managed_keys.drain_last_used()
    managed_keys._last_used_claims.clear()
    started = asyncio.Event()
    release = asyncio.Event()
    writes = 0

    async def blocked_writer(key_id, seen_at):
        nonlocal writes
        writes += 1
        started.set()
        await release.wait()
        return True

    monkeypatch.setattr(managed_keys, "_write_last_used", blocked_writer)
    responses = await asyncio.wait_for(asyncio.gather(*(
        clients.get("/tools", headers=_h(key["secret"])) for _ in range(4)
    )), timeout=2)
    assert [response.status_code for response in responses] == [200, 200, 200, 200]
    await asyncio.wait_for(started.wait(), timeout=1)
    assert writes == 1
    release.set()
    await managed_keys.drain_last_used()


async def test_last_used_is_persisted_as_throttled_display_metadata(clients):
    org_id = (await clients.get("/auth/me")).json()["org_id"]
    key = (await clients.post(
        f"/orgs/{org_id}/api-keys", json={"name": "Last used"},
    )).json()
    await managed_keys.drain_last_used()
    managed_keys._last_used_claims.clear()
    assert (await clients.get("/tools", headers=_h(key["secret"]))).status_code == 200
    await managed_keys.drain_last_used()
    async with session_maker() as db:
        row = await db.get(ApiKey, key["id"])
        first = row.last_used_at
    assert first is not None

    assert (await clients.get("/tools", headers=_h(key["secret"]))).status_code == 200
    await managed_keys.drain_last_used()
    async with session_maker() as db:
        row = await db.get(ApiKey, key["id"])
        assert row.last_used_at == first


async def test_human_admin_rules_for_rename_hide_and_activity(clients):
    org_id = (await clients.get("/auth/me")).json()["org_id"]
    member_token = crypto.new_token()
    admin_token = crypto.new_token()
    async with session_maker() as db:
        member = User(email="key-member@example.dev")
        admin = User(email="key-admin@example.dev")
        db.add(member)
        db.add(admin)
        await db.flush()
        db.add(Membership(
            user_id=member.id, org_id=org_id, role="member",
            token_hash=crypto.hash_token(member_token),
        ))
        db.add(Membership(
            user_id=admin.id, org_id=org_id, role="admin",
            token_hash=crypto.hash_token(admin_token),
        ))
        await db.commit()

    made = (await clients.post(
        f"/orgs/{org_id}/api-keys", headers=_h(member_token), json={"name": "Member owned"},
    )).json()
    key_id = made["id"]
    for headers in ({}, _h(admin_token)):
        rows = (await clients.get(f"/orgs/{org_id}/api-keys", headers=headers)).json()
        row = next(item for item in rows if item["id"] == key_id)
        assert row["can_rename"] is False and row["can_hide"] is False
        assert row["can_disable"] is True and row["can_revoke"] is True
        assert (await clients.get(
            f"/orgs/{org_id}/api-keys/{key_id}/events", headers=headers,
        )).status_code == 200
        assert (await clients.patch(
            f"/orgs/{org_id}/api-keys/{key_id}", headers=headers, json={"name": "No"},
        )).status_code == 403
        assert (await clients.post(
            f"/orgs/{org_id}/api-keys/{key_id}/hide", headers=headers,
        )).status_code == 403

    renamed = await clients.patch(
        f"/orgs/{org_id}/api-keys/{key_id}", headers=_h(member_token),
        json={"name": "Member renamed"},
    )
    assert renamed.status_code == 200
    assert (await clients.post(f"/orgs/{org_id}/api-keys/{key_id}/revoke")).status_code == 200
    assert (await clients.post(
        f"/orgs/{org_id}/api-keys/{key_id}/hide", headers=_h(member_token),
    )).status_code == 200


async def test_agent_key_uses_readable_name_and_admin_metadata_actions(clients):
    org_id = (await clients.get("/auth/me")).json()["org_id"]
    agent = (await clients.post(
        f"/orgs/{org_id}/agents", json={"name": "Readable Worker"},
    )).json()
    rows = (await clients.get(f"/orgs/{org_id}/api-keys")).json()
    row = next(item for item in rows if item["user_id"] == agent["user_id"])
    assert row["assigned_name"] == "readable-worker"
    assert row["identity"].endswith("@agents.treg.local")
    assert row["created_at"] and row["created_by"]
    assert row["can_rename"] is True and row["can_hide"] is True
    assert (await clients.patch(
        f"/orgs/{org_id}/api-keys/{row['id']}", json={"name": "Renamed agent key"},
    )).status_code == 200
    assert (await clients.post(
        f"/orgs/{org_id}/api-keys/{row['id']}/hide",
    )).status_code == 409
    assert (await clients.post(
        f"/orgs/{org_id}/api-keys/{row['id']}/revoke",
    )).status_code == 200
    assert (await clients.post(
        f"/orgs/{org_id}/api-keys/{row['id']}/hide",
    )).status_code == 200


async def test_secret_bearing_default_legacy_agent_and_public_responses_are_no_store(clients):
    me = (await clients.get("/auth/me")).json()
    org_id = me["org_id"]
    orgs = (await clients.get("/orgs")).json()
    org = next(row for row in orgs if row["org_id"] == org_id)

    default = await clients.get("/auth/cli-token", headers={"X-Treg-Org": org["slug"]})
    assert default.json()["token"] and default.headers["cache-control"] == "no-store"
    legacy = await clients.post("/orgs", json={"name": "No store legacy"})
    assert legacy.json()["token"] and legacy.headers["cache-control"] == "no-store"
    registered = await clients.post("/users", json={"email": "no-store-signup@example.dev"})
    assert registered.json()["token"] and registered.headers["cache-control"] == "no-store"
    invite = await clients.post(f"/orgs/{org_id}/invites", json={
        "email": "no-store-invite@example.dev", "role": "member",
    })
    accepted = await clients.post("/invites/accept", json={
        "email": "no-store-invite@example.dev", "code": invite.json()["code"],
    })
    assert accepted.json()["token"] and accepted.headers["cache-control"] == "no-store"
    agent = await clients.post(f"/orgs/{org_id}/agents", json={"name": "no-store-agent"})
    assert agent.json()["token"] and agent.headers["cache-control"] == "no-store"
    public = await clients.post(f"/orgs/{org_id}/public-token")
    assert public.json()["token"] and public.headers["cache-control"] == "no-store"
    assert (await clients.delete(f"/orgs/{org_id}/public-token")).status_code == 200


async def test_identity_token_responses_are_no_store(clients, sent_otps):
    revoked = await clients.post("/auth/revoke-tokens")
    assert revoked.json()["token"] and revoked.headers["cache-control"] == "no-store"
    pairing = (await clients.post("/auth/cli/start")).json()
    pending = await clients.get("/auth/cli/poll", params={"login_id": pairing["login_id"]})
    assert pending.headers["cache-control"] == "no-store"
    code = await _start_code(clients, "no-store-login@example.dev", sent_otps)
    verified = await clients.post("/auth/email/verify", json={
        "email": "no-store-login@example.dev", "code": code,
    })
    assert verified.json()["token"] and verified.headers["cache-control"] == "no-store"


async def test_cli_login_accepts_active_managed_key_and_rejects_disabled_and_revoked(
    clients, monkeypatch, tmp_path,
):
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "config.json")
    org_id = (await clients.get("/auth/me")).json()["org_id"]
    made = (await clients.post(
        f"/orgs/{org_id}/api-keys", json={"name": "CLI managed"},
    )).json()
    secret = made["secret"]
    active_who = await clients.get("/auth/me", headers=_h(secret))
    active_orgs = await clients.get("/orgs", headers=_h(secret))

    class StubClient:
        def __init__(self, responses):
            self.responses = responses

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, path):
            return next(self.responses)

    active_responses = iter((active_who, active_orgs))
    monkeypatch.setattr(cli, "_client", lambda cfg: StubClient(active_responses))
    cli.cmd_login(
        SimpleNamespace(token=secret, email=None),
        {"base_url": "http://registry", "token": None, "active_org": None},
    )
    assert cli._load_config()["token"] == secret

    await managed_keys.drain_last_used()
    assert (await clients.post(
        f"/orgs/{org_id}/api-keys/{made['id']}/disable",
    )).status_code == 200
    disabled_who = await clients.get("/auth/me", headers=_h(secret))
    disabled_responses = iter((disabled_who,))
    monkeypatch.setattr(cli, "_client", lambda cfg: StubClient(disabled_responses))
    with pytest.raises(SystemExit):
        cli.cmd_login(
            SimpleNamespace(token=secret, email=None),
            {"base_url": "http://registry", "token": None, "active_org": None},
        )
    assert cli._load_config()["token"] == secret

    assert (await clients.post(
        f"/orgs/{org_id}/api-keys/{made['id']}/enable",
    )).status_code == 200
    assert (await clients.post(
        f"/orgs/{org_id}/api-keys/{made['id']}/revoke",
    )).status_code == 200
    revoked_who = await clients.get("/auth/me", headers=_h(secret))
    revoked_responses = iter((revoked_who,))
    monkeypatch.setattr(cli, "_client", lambda cfg: StubClient(revoked_responses))
    with pytest.raises(SystemExit):
        cli.cmd_login(
            SimpleNamespace(token=secret, email=None),
            {"base_url": "http://registry", "token": None, "active_org": None},
        )


async def test_activity_has_key_snapshot_and_key_filter(clients):
    org_id = (await clients.get("/auth/me")).json()["org_id"]
    created = (await clients.post(
        f"/orgs/{org_id}/api-keys", json={"name": "Audit key"},
    )).json()
    secret, key_id = created["secret"], created["id"]
    assert (await clients.post("/agents/checkin", headers=_h(secret))).status_code == 200

    calls = (await clients.get(f"/calls?api_key_id={key_id}")).json()
    assert len(calls) == 1
    assert calls[0]["api_key_id"] == key_id
    assert calls[0]["api_key_name"] == "Audit key"
    assert calls[0]["api_key_prefix"] == secret[:12]
    assert (await clients.get("/calls?api_key_id=999999")).json() == []

    events = (await clients.get(f"/orgs/{org_id}/api-keys/{key_id}/events")).json()
    assert events[0]["action"] == "created"
    assert secret not in str(events)


async def test_membership_removal_keeps_safe_key_and_activity_history(clients):
    org_id = (await clients.get("/auth/me")).json()["org_id"]
    token = crypto.new_token()
    async with session_maker() as db:
        user = User(email="departing@example.dev")
        db.add(user); await db.flush()
        membership = Membership(user_id=user.id, org_id=org_id, role="member",
                                token_hash=crypto.hash_token(token))
        db.add(membership); await db.commit()
        user_id = user.id
    key = (await clients.post(
        f"/orgs/{org_id}/api-keys", headers=_h(token), json={"name": "Departing key"},
    )).json()
    assert (await clients.post("/agents/checkin", headers=_h(key["secret"]))).status_code == 200
    assert (await clients.delete(f"/orgs/{org_id}/members/{user_id}")).status_code == 200
    assert (await clients.get("/tools", headers=_h(key["secret"]))).status_code in (401, 403)

    retained = (await clients.get(f"/orgs/{org_id}/api-keys")).json()
    row = next(item for item in retained if item["id"] == key["id"])
    assert row["membership_id"] is None and row["state"] == "revoked"
    activity = (await clients.get(f"/calls?api_key_id={key['id']}")).json()
    assert len(activity) == 1 and activity[0]["api_key_name"] == "Departing key"


async def test_more_keys_do_not_bypass_membership_daily_cap(clients):
    org_id = (await clients.get("/auth/me")).json()["org_id"]
    token = crypto.new_token()
    async with session_maker() as db:
        user = User(email="capped@example.dev")
        db.add(user); await db.flush()
        db.add(Membership(user_id=user.id, org_id=org_id, role="member",
                          token_hash=crypto.hash_token(token), daily_call_cap=1))
        await db.commit()
    extra = (await clients.post(
        f"/orgs/{org_id}/api-keys", headers=_h(token), json={"name": "Second key"},
    )).json()["secret"]
    secret_id = (await clients.post(
        "/secrets", json={"name": "cap-key", "value": "demo"},
    )).json()["id"]
    assert (await clients.post("/tools", json={
        "name": "cap-test", "base_url": "http://upstream", "secret_id": secret_id,
    })).status_code == 200

    assert (await clients.get("/call/cap-test/ok", headers=_h(token))).status_code == 200
    await audit.drain()
    assert (await clients.get("/call/cap-test/ok", headers=_h(extra))).status_code == 429


async def test_default_signed_key_can_be_disabled_for_only_one_team(sent_otps):
    await reset_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as client:
        code = await _start_code(client, "owner@example.dev", sent_otps)
        identity = (await client.post(
            "/auth/email/verify", json={"email": "owner@example.dev", "code": code},
        )).json()["token"]
        first = (await client.post("/orgs", headers=_h(identity), json={"name": "First"})).json()
        second = (await client.post("/orgs", headers=_h(identity), json={"name": "Second"})).json()
        pinned = first["token"]
        keys = (await client.get(
            f"/orgs/{first['org_id']}/api-keys", headers=_h(first["token"]),
        )).json()
        default = next(row for row in keys if row["kind"] == "default_human")
        assert (await client.post(
            f"/orgs/{first['org_id']}/api-keys/{default['id']}/disable",
            headers=_h(first["token"]),
        )).status_code == 200
        assert (await client.get("/tools", headers=_h(pinned))).status_code == 401
        assert (await client.get("/auth/me", headers=_h(pinned))).status_code == 401
        assert (await client.get("/tools", headers=_h(second["token"]))).status_code == 200
        second_key = (await client.post(
            f"/orgs/{second['org_id']}/api-keys", headers=_h(second["token"]),
            json={"name": "Second team only"},
        )).json()
        assert (await client.post(
            f"/orgs/{second['org_id']}/api-keys/{second_key['id']}/disable",
            headers=_h(first["token"]),
        )).status_code == 401


async def test_default_key_rotation_is_team_specific_and_not_revocable(sent_otps):
    await reset_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as client:
        code = await _start_code(client, "rotate-owner@example.dev", sent_otps)
        identity = (await client.post("/auth/email/verify", json={
            "email": "rotate-owner@example.dev", "code": code,
        })).json()["token"]
        first = (await client.post("/orgs", headers=_h(identity), json={"name": "First"})).json()
        second = (await client.post("/orgs", headers=_h(identity), json={"name": "Second"})).json()
        first_token = first["token"]
        second_token = second["token"]
        rows = (await client.get(
            f"/orgs/{first['org_id']}/api-keys", headers=_h(first_token),
        )).json()
        default = next(row for row in rows if row["kind"] == managed_keys.DEFAULT_KIND)
        assert default["can_rotate"] is True
        assert default["can_revoke"] is False

        rotated = await client.post(
            f"/orgs/{first['org_id']}/api-keys/{default['id']}/rotate",
            headers=_h(first_token),
        )
        assert rotated.status_code == 200, rotated.text
        replacement = rotated.json()["secret"]
        assert replacement != first_token
        assert (await client.get("/tools", headers=_h(first_token))).json() == {"detail": "revoked key"}
        assert (await client.get("/tools", headers=_h(replacement))).status_code == 200
        assert (await client.get("/tools", headers=_h(second_token))).status_code == 200
        refused = await client.post(
            f"/orgs/{first['org_id']}/api-keys/{default['id']}/revoke",
            headers=_h(replacement),
        )
        assert refused.status_code == 409

        disabled = await client.post(
            f"/orgs/{first['org_id']}/api-keys/{default['id']}/disable",
            headers={"X-Treg-Org": first["org"]},
        )
        assert disabled.status_code == 200, disabled.text
        issued = (await client.get(
            "/auth/cli-token", headers={"X-Treg-Org": first["org"]},
        )).json()
        assert issued["default_key_id"] == default["id"]
        assert issued["default_key_state"] == managed_keys.DISABLED


async def test_key_audit_rows_never_contain_plaintext(clients):
    org_id = (await clients.get("/auth/me")).json()["org_id"]
    created = (await clients.post(
        f"/orgs/{org_id}/api-keys", json={"name": "No secret event"},
    )).json()
    async with session_maker() as db:
        events = (await db.execute(select(ApiKeyEvent).where(
            ApiKeyEvent.key_id == created["id"],
        ))).scalars().all()
    assert events and created["secret"] not in repr(events)
