from __future__ import annotations

import json
import os

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from treg import crypto
from treg.application import credential_reencrypt
from treg.application.call import service as call_service
from treg.config import get_settings
from treg.infra import credential_cipher
from treg.infra.db import reset_db, session_maker
from treg.models import Org, Secret
from treg.application.call.types import UpstreamResponse

KEY_ARN = "arn:aws:kms:us-west-2:123456789012:key/test"


class _InvalidCiphertext(Exception):
    pass


class _FakeKms:
    """KMS as treg uses it: a data key per seal, unwrapped only under the same key and context."""

    def __init__(self):
        self.keys: dict[bytes, tuple[bytes, dict, str]] = {}
        self.calls: list[tuple[str, dict]] = []

    def generate_data_key(self, *, KeyId, KeySpec, EncryptionContext):
        assert KeySpec == "AES_256"
        self.calls.append(("generate", dict(EncryptionContext)))
        key, wrapped = os.urandom(32), os.urandom(24)
        self.keys[wrapped] = (key, dict(EncryptionContext), KeyId)
        return {"Plaintext": key, "CiphertextBlob": wrapped}

    def decrypt(self, *, CiphertextBlob, KeyId, EncryptionContext):
        self.calls.append(("decrypt", dict(EncryptionContext)))
        key, context, key_id = self.keys[CiphertextBlob]
        if context != EncryptionContext or key_id != KeyId:
            raise _InvalidCiphertext
        return {"Plaintext": key}

    def count(self, kind: str) -> int:
        return sum(1 for name, _ in self.calls if name == kind)


@pytest.fixture
def kms(monkeypatch):
    fake = _FakeKms()
    monkeypatch.setattr(credential_cipher, "_client", lambda key_id: fake)
    monkeypatch.setenv("TREG_KMS_KEY_ID", KEY_ARN)
    monkeypatch.setenv("TREG_KMS_ENCRYPT_NEW", "true")
    get_settings.cache_clear()
    yield fake
    get_settings.cache_clear()


def _writes_off(monkeypatch):
    monkeypatch.setenv("TREG_KMS_ENCRYPT_NEW", "false")
    get_settings.cache_clear()


async def test_a_kms_sealed_value_opens_only_for_its_own_team(kms):
    sealed = await credential_cipher.seal("sk-live-Q7", org_id=7)

    assert sealed.startswith("kms1:")
    assert "sk-live-Q7" not in sealed
    assert sealed != await credential_cipher.seal("sk-live-Q7", org_id=7)
    assert await credential_cipher.unseal(sealed, org_id=7) == "sk-live-Q7"
    assert kms.calls[0] == ("generate", {"treg:purpose": "credential", "treg:org": "7"})
    with pytest.raises(_InvalidCiphertext):
        await credential_cipher.unseal(sealed, org_id=8)


async def test_a_fernet_value_still_opens_without_calling_kms(kms):
    assert await credential_cipher.unseal(crypto.encrypt("legacy-key"), org_id=7) == "legacy-key"
    assert kms.calls == []


async def test_writes_stay_fernet_until_switched_on(kms, monkeypatch):
    _writes_off(monkeypatch)
    sealed = await credential_cipher.seal("sk-live-Q7", org_id=7)

    assert not credential_cipher.is_kms_sealed(sealed)
    assert crypto.decrypt(sealed) == "sk-live-Q7"
    assert kms.calls == []


async def test_a_server_without_the_key_refuses_a_kms_value(kms, monkeypatch):
    sealed = await credential_cipher.seal("sk-live-Q7", org_id=7)
    monkeypatch.delenv("TREG_KMS_KEY_ID")
    monkeypatch.delenv("TREG_KMS_ENCRYPT_NEW")
    get_settings.cache_clear()

    with pytest.raises(credential_cipher.CredentialCipherError, match="TREG_KMS_KEY_ID"):
        await credential_cipher.unseal(sealed, org_id=7)


def test_kms_settings_refuse_an_incomplete_configuration(monkeypatch):
    monkeypatch.setenv("TREG_KMS_ENCRYPT_NEW", "true")
    with pytest.raises(ValueError, match="requires kms_key_id"):
        get_settings.__wrapped__()
    monkeypatch.setenv("TREG_KMS_KEY_ID", "alias/treg-secrets")
    with pytest.raises(ValueError, match="ARN"):
        get_settings.__wrapped__()


async def _own_tool(clients: AsyncClient, value: str) -> int:
    secret = (await clients.post("/secrets", json={"name": "own-key", "value": value})).json()
    r = await clients.post("/tools", json={
        "name": "own-tool", "base_url": "http://upstream",
        "bindings": [{"secret_id": secret["id"], "injector": "env",
                      "name": "Authorization", "format": "Bearer {secret}"}],
    })
    assert r.status_code == 200, r.text
    return secret["id"]


async def test_an_own_key_call_injects_a_kms_sealed_credential(clients: AsyncClient, kms):
    secret_id = await _own_tool(clients, "sk-own-Q7")
    async with session_maker() as db:
        assert (await db.get(Secret, secret_id)).value.startswith("kms1:")

    r = await clients.get("/call/own-tool/resource")

    assert r.status_code == 200, r.text
    assert r.json()["auth"] == "Bearer sk-own-Q7"
    assert kms.count("decrypt") == 1


def _echoing_relay(body: bytes):
    async def _relay(request, upstream_url, tool, secrets, client, drop_params=None,
                     force_identity=False):
        async def _stream():
            yield body

        async def _close():
            return None

        return UpstreamResponse(400, ((b"content-type", b"application/json"),), _stream(), _close)

    return _relay


async def test_a_call_opens_its_credential_once_however_often_masking_reads_it(
    clients: AsyncClient, kms, monkeypatch,
):
    await _own_tool(clients, "sk-own-Q7")
    monkeypatch.setattr(call_service, "relay", _echoing_relay(b'{"error":"bad key","got":"sk-own-Q7"}'))

    r = await clients.get("/call/own-tool/resource")

    assert r.status_code == 400
    assert "sk-own-Q7" not in r.text
    assert kms.count("decrypt") == 1


async def test_listing_connections_opens_no_credential(clients: AsyncClient, kms):
    state = (await clients.post("/oauth/start", json={
        "name": "conn", "client_id": "cid", "client_secret": "csec",
        "auth_uri": "http://provider/auth", "token_uri": "http://upstream/token", "scopes": ["s"],
    })).json()["state"]
    await clients.get(f"/oauth/callback?code=AUTHCODE&state={state}")
    secret_id = (await clients.get(f"/oauth/status/{state}")).json()["secret_id"]
    async with session_maker() as db:
        stored = await db.get(Secret, secret_id)
        assert stored.value.startswith("kms1:") and stored.refreshable is True
    opened_before = kms.count("decrypt")

    connections = (await clients.get("/connections")).json()

    assert next(c for c in connections if c["id"] == secret_id)["refreshable"] is True
    assert kms.count("decrypt") == opened_before


async def _org(db, slug: str, *, demo: bool = False) -> Org:
    org = Org(name=slug, slug=slug, demo=demo)
    db.add(org)
    await db.flush()
    return org


async def test_reencryption_reseals_every_real_credential_and_records_refreshable(kms, monkeypatch):
    await reset_db()
    _writes_off(monkeypatch)
    oauth_blob = json.dumps({"access_token": "a", "refresh_token": "r", "client_id": "c",
                             "client_secret": "s"})
    async with session_maker() as db:
        team, demo = await _org(db, "reseal-team"), await _org(db, "reseal-demo", demo=True)
        rows = [Secret(org_id=team.id, name="key", value=crypto.encrypt("sk-team")),
                Secret(org_id=team.id, name="conn", kind="oauth", value=crypto.encrypt(oauth_blob)),
                Secret(org_id=demo.id, name="echo-key", value=crypto.encrypt("demo-value"))]
        db.add_all(rows)
        await db.commit()
        ids = [row.id for row in rows]

    with pytest.raises(credential_reencrypt.KmsWritesOff):
        await credential_reencrypt.reencrypt()
    preview = await credential_reencrypt.reencrypt(dry_run=True)
    assert kms.calls == []

    monkeypatch.setenv("TREG_KMS_ENCRYPT_NEW", "true")
    get_settings.cache_clear()
    result = await credential_reencrypt.reencrypt(batch_size=1)

    assert result["error"] is None
    assert result["resealed"] == preview["resealed"] >= 2
    async with session_maker() as db:
        key, conn, demo_seed = [await db.get(Secret, i) for i in ids]
    assert await credential_cipher.unseal(key.value, org_id=key.org_id) == "sk-team"
    assert await credential_cipher.unseal(conn.value, org_id=conn.org_id) == oauth_blob
    assert conn.refreshable is True
    assert crypto.decrypt(demo_seed.value) == "demo-value"
    assert (await credential_reencrypt.reencrypt())["resealed"] == 0


async def test_reencryption_leaves_a_credential_replaced_while_it_ran(kms, monkeypatch):
    await reset_db()
    _writes_off(monkeypatch)
    async with session_maker() as db:
        team = await _org(db, "reseal-race")
        row = Secret(org_id=team.id, name="key", value=crypto.encrypt("old-key"))
        db.add(row)
        await db.commit()
        secret_id = row.id
    monkeypatch.setenv("TREG_KMS_ENCRYPT_NEW", "true")
    get_settings.cache_clear()
    seal = credential_cipher.seal
    replaced = crypto.encrypt("rotated-key")

    async def seal_then_rotate(plaintext, *, org_id):
        sealed = await seal(plaintext, org_id=org_id)
        if plaintext == "old-key":
            async with session_maker() as db:
                (await db.get(Secret, secret_id)).value = replaced
                await db.commit()
        return sealed

    monkeypatch.setattr(credential_cipher, "seal", seal_then_rotate)
    result = await credential_reencrypt.reencrypt()

    assert result["changed"] >= 1
    async with session_maker() as db:
        assert (await db.execute(select(Secret.value).where(Secret.id == secret_id))).scalar_one() == replaced


async def test_no_database_connection_waits_on_kms_when_credentials_are_written(
    clients: AsyncClient, kms, monkeypatch,
):
    from treg.infra import db as infra_db

    held: list[int] = []
    seal = credential_cipher.seal

    async def watching_seal(plaintext, *, org_id):
        held.append(infra_db._engine.pool.checkedout())
        return await seal(plaintext, org_id=org_id)

    monkeypatch.setattr(credential_cipher, "seal", watching_seal)
    created = (await clients.post("/secrets", json={"name": "k", "value": "sk-1"})).json()
    await clients.patch(f"/secrets/{created['id']}", json={"value": "sk-2"})
    r = await clients.post("/skills", json={
        "name": "sealed-skill", "secrets": [{"local_name": "a", "value": "sk-a"},
                                            {"local_name": "b", "value": "sk-b"}],
        "tools": [{"name": "sealed-tool", "base_url": "http://upstream",
                   "bindings": [{"secret": "a", "injector": "env", "location": "header",
                                 "name": "Authorization", "format": "Bearer {secret}"}]}],
    })

    assert r.status_code == 200, r.text
    assert len(held) == 4
    assert held == [0, 0, 0, 0]


async def test_no_database_connection_waits_on_kms_when_a_run_grant_refreshes(
    clients: AsyncClient, kms, monkeypatch,
):
    from treg.infra import db as infra_db

    stale = json.dumps({"access_token": "old", "refresh_token": "r", "client_id": "cid",
                        "client_secret": "csec", "token_uri": "http://upstream/token", "expires_at": 0})
    r = await clients.post("/skills", json={
        "name": "grant-skill", "recipe": "# grant-skill\n",
        "secrets": [{"local_name": "conn", "kind": "oauth", "value": stale}],
        "tools": [{"name": "grant-skill", "base_url": "https://api.example.com",
                   "cli": {"bin": "sh", "enabled": True,
                           "inject": [{"secret": "conn", "via": "env", "name": "TOKEN",
                                       "secret_field": "access_token"}]}}],
    })
    assert r.status_code == 200, r.text
    held: list[int] = []
    unseal = credential_cipher.unseal

    async def watching_unseal(value, *, org_id):
        held.append(infra_db._engine.pool.checkedout())
        return await unseal(value, org_id=org_id)

    monkeypatch.setattr(credential_cipher, "unseal", watching_unseal)
    grant = await clients.post("/tools/grant-skill/grant", json={"argv": []})

    assert grant.status_code == 200, grant.text
    assert held and set(held) == {0}
