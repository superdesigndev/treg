from __future__ import annotations

from datetime import timedelta

import pytest
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from treg import crypto
from treg.application import fernet_rotation
from treg.config import get_settings
from treg.infra.db import reset_db, session_maker
from treg.models import ArenaRun, Org, WebArenaRun
from treg.timeutil import utcnow_naive

OLD, NEW = Fernet.generate_key().decode(), Fernet.generate_key().decode()


@pytest.fixture
async def database():
    await reset_db()


@pytest.fixture
def secret_key(monkeypatch, database):
    def use(value: str) -> None:
        monkeypatch.setenv("TREG_SECRET_KEY", value)
        get_settings.cache_clear()

    yield use
    get_settings.cache_clear()


def test_a_key_list_encrypts_with_the_first_and_opens_with_any(secret_key):
    secret_key(OLD)
    old_token = crypto.encrypt("x")
    secret_key(f"{NEW},{OLD}")

    assert crypto.decrypt(old_token) == "x"
    assert not crypto.is_current(old_token)
    assert crypto.is_current(crypto.encrypt("y"))
    secret_key(NEW)
    with pytest.raises(InvalidToken):
        crypto.decrypt(old_token)


async def _arena_rows(payloads: list[str]) -> list[str]:
    now = utcnow_naive()
    async with session_maker() as db:
        org = Org(name="rotation", slug=f"rotation-{now.timestamp()}")
        db.add(org)
        await db.flush()
        ids = []
        for i, payload in enumerate(payloads):
            run_id = f"run-{now.timestamp()}-{i}"
            db.add(ArenaRun(id=run_id, org_id=org.id, user_id=1, request_key=run_id, fingerprint="f",
                            capability="c", mode="m", payload=payload, deadline_at=now,
                            expires_at=now + timedelta(days=30)))
            ids.append(run_id)
        db.add(WebArenaRun(id=f"web-{now.timestamp()}", org_id=org.id, user_id=1, task="t", mode="m",
                           state="done", payload=payloads[0], deadline_at=now,
                           expires_at=now + timedelta(days=30)))
        await db.commit()
    return ids


async def _payload(run_id: str) -> str:
    async with session_maker() as db:
        return (await db.execute(select(ArenaRun.payload).where(ArenaRun.id == run_id))).scalar_one()


async def test_rotation_moves_every_payload_to_the_first_key(secret_key):
    secret_key(OLD)
    ids = await _arena_rows([crypto.encrypt('{"a": 1}'), crypto.encrypt('{"b": 2}')])
    secret_key(f"{NEW},{OLD}")

    preview = await fernet_rotation.rotate(dry_run=True)
    assert preview["arenarun"]["rotated"] >= 2 and not crypto.is_current(await _payload(ids[0]))

    result = await fernet_rotation.rotate(batch_size=1)

    assert result["error"] is None
    assert result["arenarun"]["unreadable"] == 0 and result["webarenarun"]["rotated"] >= 1
    secret_key(NEW)
    assert [crypto.decrypt(await _payload(i)) for i in ids] == ['{"a": 1}', '{"b": 2}']
    assert (await fernet_rotation.rotate())["arenarun"]["rotated"] == 0


async def test_rotation_leaves_a_payload_updated_while_it_ran(secret_key):
    secret_key(OLD)
    [run_id] = await _arena_rows([crypto.encrypt('{"v": 1}')])
    secret_key(f"{NEW},{OLD}")
    updated = crypto.encrypt('{"v": 2}')
    opened = 0

    class _UpdatedFirst:
        """The first batch's write session, entered just after a request rewrote the run."""

        async def __aenter__(self):
            async with session_maker() as db:
                (await db.get(ArenaRun, run_id)).payload = updated
                await db.commit()
            self.session = session_maker()
            return await self.session.__aenter__()

        async def __aexit__(self, *exc):
            return await self.session.__aexit__(*exc)

    def racing_factory():
        nonlocal opened
        opened += 1
        return _UpdatedFirst() if opened == 2 else session_maker()

    result = await fernet_rotation.rotate(session_factory=racing_factory)

    assert result["arenarun"]["changed"] >= 1
    assert await _payload(run_id) == updated
