"""`treg host`: reference-file hosting for AIGC endpoints (docs/context/architecture/media.md)."""
from datetime import timedelta

from httpx import AsyncClient
from sqlalchemy import update

from treg.application import media as media_app
from treg.infra.db import session_maker
from treg.models import Media, _now

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


async def test_host_then_fetch_roundtrip_is_byte_exact_and_public(clients: AsyncClient):
    r = await clients.post("/media", content=PNG, headers={"content-type": "image/png"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["url"].endswith("/m/" + body["token"]) and body["size"] == len(PNG)
    # The vendor's fetcher holds no treg token: the URL alone must serve the bytes.
    anon = AsyncClient(transport=clients._transport, base_url="http://registry")
    g = await anon.get(f"/m/{body['token']}")
    assert g.status_code == 200 and g.content == PNG and g.headers["content-type"] == "image/png"
    assert g.headers["content-security-policy"] == "sandbox" and g.headers["x-content-type-options"] == "nosniff"


async def test_hosting_needs_a_member_token(clients: AsyncClient):
    anon = AsyncClient(transport=clients._transport, base_url="http://registry")
    r = await anon.post("/media", content=PNG, headers={"content-type": "image/png"})
    assert r.status_code == 401


async def test_only_media_types_are_hosted(clients: AsyncClient):
    r = await clients.post("/media", content=b"<html>", headers={"content-type": "text/html"})
    assert r.status_code == 415
    r = await clients.post("/media", content=b"{}", headers={"content-type": "application/json"})
    assert r.status_code == 415
    # An SVG is an image that carries script; inline on our origin it would be stored XSS.
    r = await clients.post("/media", content=b"<svg onload=alert(1)/>", headers={"content-type": "image/svg+xml"})
    assert r.status_code == 415


async def test_the_per_file_cap_and_daily_quota_refuse_before_storing(clients: AsyncClient, monkeypatch):
    monkeypatch.setattr(media_app, "MAX_BYTES", 100)
    r = await clients.post("/media", content=b"x" * 101, headers={"content-type": "audio/mpeg"})
    assert r.status_code == 413  # refused on Content-Length, before the body is read
    async def chunked():
        yield b"x" * 60
        yield b"x" * 60
    r = await clients.post("/media", content=chunked(), headers={"content-type": "audio/mpeg"})
    assert r.status_code == 413  # chunked, no Content-Length: refused mid-stream at the cap
    monkeypatch.setattr(media_app, "DAILY_ORG_BYTES", 150)
    assert (await clients.post("/media", content=b"x" * 100, headers={"content-type": "audio/mpeg"})).status_code == 201
    r = await clients.post("/media", content=b"x" * 100, headers={"content-type": "audio/mpeg"})
    assert r.status_code == 429


async def test_expired_media_is_gone_and_swept_by_the_next_upload(clients: AsyncClient):
    tok = (await clients.post("/media", content=PNG, headers={"content-type": "image/png"})).json()["token"]
    async with session_maker() as db:
        await db.execute(update(Media).where(Media.token == tok).values(expires_at=_now() - timedelta(seconds=1)))
        await db.commit()
    assert (await clients.get(f"/m/{tok}")).status_code == 404
    await clients.post("/media", content=PNG, headers={"content-type": "image/png"})
    assert await media_app.get(tok) is None
    async with session_maker() as db:
        assert (await db.execute(Media.__table__.select().where(Media.token == tok))).first() is None


# --- bodies in the object store (`media_body_write = "r2"`) ---------------------------------------

import asyncio  # noqa: E402
from collections import Counter  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import event  # noqa: E402

from treg.config import get_settings  # noqa: E402
from treg.infra import db as infra_db  # noqa: E402
from treg.infra import object_store  # noqa: E402
from treg.infra.object_store import R2ObjectStore  # noqa: E402


class _BucketSDK:
    """The obstore calls the adapter makes, on a dict. Every call counts whether its own task holds
    a database connection, and the fixture fails on one (AGENTS.md rule 3). Other tasks may."""
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.fail = False
        self.owned: Counter = Counter()  # connections checked out, by the task that holds them
        self.held = 0

    def _io(self):
        if self.owned[asyncio.current_task()]:
            self.held += 1
        if self.fail:
            raise ConnectionError("bucket down")

    class Result:
        def __init__(self, sdk, body):
            self.sdk, self.body, self.meta = sdk, body, {"size": len(body)}

        async def stream(self, min_chunk_size):
            for i in range(0, len(self.body), 7):
                self.sdk._io()
                yield self.body[i:i + 7]

    async def put_async(self, path, body, *, attributes, use_multipart):
        self._io()
        self.objects[path] = bytes(body)

    async def get_async(self, path):
        self._io()
        if path not in self.objects:
            raise FileNotFoundError(path)
        return self.Result(self, self.objects[path])

    async def delete_async(self, path):
        self._io()
        self.objects.pop(path, None)


@pytest.fixture
def bucket(monkeypatch):
    sdk = _BucketSDK()
    def checkout(connection, record, proxy):
        record.info["media_test_owner"] = owner = asyncio.current_task()
        sdk.owned[owner] += 1
    def checkin(connection, record):
        sdk.owned[record.info.pop("media_test_owner", None)] -= 1
    engines = {engine.sync_engine for engine in infra_db._engines}
    for engine in engines:
        event.listen(engine, "checkout", checkout)
        event.listen(engine, "checkin", checkin)
    media_app.use_store(R2ObjectStore(sdk, media_app.MAX_BYTES))
    monkeypatch.setattr(get_settings(), "media_body_write", "r2")
    yield sdk
    media_app.use_store(None)
    for engine in engines:
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)
    assert not sdk.held, "object I/O held a database connection"


async def _row(token: str) -> Media:
    async with session_maker() as db:
        return (await db.execute(Media.__table__.select().where(Media.token == token))).first()


async def test_db_mode_keeps_the_body_in_the_row(clients: AsyncClient):
    tok = (await clients.post("/media", content=PNG, headers={"content-type": "image/png"})).json()["token"]
    row = await _row(tok)
    assert row.body == PNG and row.object_key is None


async def test_r2_mode_stores_the_body_in_the_bucket_under_an_opaque_key(clients: AsyncClient, bucket):
    r = await clients.post("/media", content=PNG, headers={"content-type": "image/png"})
    assert r.status_code == 201, r.text
    tok = r.json()["token"]
    row = await _row(tok)
    assert row.body is None and row.object_key.startswith(media_app.KEY_PREFIX)
    assert bucket.objects == {row.object_key: PNG}
    # The key names neither the team nor the URL's token.
    assert tok not in row.object_key and f"/{row.org_id}/" not in row.object_key
    anon = AsyncClient(transport=clients._transport, base_url="http://registry")
    g = await anon.get(f"/m/{tok}")
    assert g.status_code == 200 and g.content == PNG and g.headers["content-type"] == "image/png"
    assert g.headers["content-length"] == str(len(PNG))
    assert g.headers["cache-control"] == "public, max-age=3600" and g.headers["x-robots-tag"] == "noindex"
    assert g.headers["content-security-policy"] == "sandbox" and g.headers["x-content-type-options"] == "nosniff"


async def test_r2_mode_still_serves_rows_whose_bytes_are_in_the_database(clients: AsyncClient, bucket, monkeypatch):
    monkeypatch.setattr(get_settings(), "media_body_write", "db")
    legacy = (await clients.post("/media", content=PNG, headers={"content-type": "image/png"})).json()["token"]
    monkeypatch.setattr(get_settings(), "media_body_write", "r2")
    fresh = (await clients.post("/media", content=b"ID3" * 9, headers={"content-type": "audio/mpeg"})).json()["token"]
    assert (await clients.get(f"/m/{legacy}")).content == PNG
    assert (await clients.get(f"/m/{fresh}")).content == b"ID3" * 9
    # And back: switching to "db" keeps serving what the bucket holds.
    monkeypatch.setattr(get_settings(), "media_body_write", "db")
    assert (await clients.get(f"/m/{fresh}")).content == b"ID3" * 9


async def test_r2_mode_keeps_every_bound(clients: AsyncClient, bucket, monkeypatch):
    monkeypatch.setattr(media_app, "MAX_BYTES", 100)
    assert (await clients.post("/media", content=b"x" * 101, headers={"content-type": "audio/mpeg"})).status_code == 413
    r = await clients.post("/media", content=b"<svg/>", headers={"content-type": "image/svg+xml"})
    assert r.status_code == 415
    monkeypatch.setattr(media_app, "DAILY_ORG_BYTES", 150)
    assert (await clients.post("/media", content=b"x" * 100, headers={"content-type": "audio/mpeg"})).status_code == 201
    assert (await clients.post("/media", content=b"x" * 100, headers={"content-type": "audio/mpeg"})).status_code == 429
    assert len(bucket.objects) == 1  # a refused upload never reaches the bucket


async def test_expiry_sweep_deletes_the_object_too(clients: AsyncClient, bucket):
    tok = (await clients.post("/media", content=PNG, headers={"content-type": "image/png"})).json()["token"]
    key = (await _row(tok)).object_key
    async with session_maker() as db:
        await db.execute(update(Media).where(Media.token == tok).values(expires_at=_now() - timedelta(seconds=1)))
        await db.commit()
    assert (await clients.get(f"/m/{tok}")).status_code == 404
    await clients.post("/media", content=PNG, headers={"content-type": "image/png"})
    assert await _row(tok) is None
    await media_app.drain()  # objects are deleted off the request path
    assert key not in bucket.objects and len(bucket.objects) == 1


async def test_a_missing_object_is_404_and_an_unreachable_bucket_503(clients: AsyncClient, bucket):
    tok = (await clients.post("/media", content=PNG, headers={"content-type": "image/png"})).json()["token"]
    bucket.fail = True
    assert (await clients.get(f"/m/{tok}")).status_code == 503
    assert (await clients.post("/media", content=PNG, headers={"content-type": "image/png"})).status_code == 503
    async with session_maker() as db:  # the failed upload left no row behind
        assert (await db.execute(Media.__table__.select())).all() == [await _row(tok)]
    bucket.fail = False
    bucket.objects.clear()
    assert (await clients.get(f"/m/{tok}")).status_code == 404


async def test_r2_mode_without_a_bucket_refuses_at_startup_and_on_upload(clients: AsyncClient, monkeypatch):
    monkeypatch.setattr(get_settings(), "media_body_write", "r2")
    monkeypatch.setattr(get_settings(), "archive_object_store_endpoint", "")
    with pytest.raises(RuntimeError):
        media_app.validate_configuration()
    r = await clients.post("/media", content=PNG, headers={"content-type": "image/png"})
    assert r.status_code == 503


@pytest.mark.parametrize("mode", ["db", "r2"])
async def test_concurrent_uploads_stay_within_the_daily_quota(clients: AsyncClient, bucket, monkeypatch, mode):
    monkeypatch.setattr(get_settings(), "media_body_write", mode)
    monkeypatch.setattr(media_app, "DAILY_ORG_BYTES", 250)
    results = await asyncio.gather(*(clients.post("/media", content=b"x" * 100, headers={"content-type": "audio/mpeg"})
                                     for _ in range(6)))
    assert sorted(r.status_code for r in results) == [201, 201, 429, 429, 429, 429]
    async with session_maker() as db:
        assert len((await db.execute(Media.__table__.select())).all()) == 2
    assert len(bucket.objects) == (2 if mode == "r2" else 0)


async def test_each_upload_sweeps_a_bounded_batch(clients: AsyncClient, bucket, monkeypatch):
    monkeypatch.setattr(media_app, "SWEEP_BATCH", 2)
    toks = [(await clients.post("/media", content=PNG, headers={"content-type": "image/png"})).json()["token"]
            for _ in range(5)]
    async with session_maker() as db:
        await db.execute(update(Media).values(expires_at=_now() - timedelta(seconds=1)))
        await db.commit()
    await clients.post("/media", content=PNG, headers={"content-type": "image/png"})
    await media_app.drain()
    left = [t for t in toks if await _row(t) is not None]
    assert len(left) == 3 and len(bucket.objects) == 4  # 3 expired left + the new upload


async def test_db_mode_never_touches_the_bucket(clients: AsyncClient, bucket, monkeypatch):
    tok = (await clients.post("/media", content=PNG, headers={"content-type": "image/png"})).json()["token"]
    async with session_maker() as db:
        await db.execute(update(Media).where(Media.token == tok).values(expires_at=_now() - timedelta(seconds=1)))
        await db.commit()
    monkeypatch.setattr(get_settings(), "media_body_write", "db")
    calls = []
    monkeypatch.setattr(bucket, "_io", lambda: calls.append(1))
    assert (await clients.post("/media", content=PNG, headers={"content-type": "image/png"})).status_code == 201
    await media_app.drain()
    assert await _row(tok) is None and calls == []  # the expired row's object is left to the lifecycle rule


async def test_a_failed_put_removes_only_its_own_row_even_when_the_id_was_reused(clients: AsyncClient, bucket, monkeypatch):
    st = media_app.store()

    async def reused_then_failed(name, body):
        # The pending row is deleted (its team deleted) and SQLite hands its id to a new upload.
        async with session_maker() as db:
            pending = (await db.execute(Media.__table__.select())).one()
            await db.execute(Media.__table__.delete())
            await db.execute(Media.__table__.insert().values(
                id=pending.id, token="someone-else", org_id=pending.org_id, content_type="image/png",
                size=len(PNG), body=PNG, created_at=pending.created_at, expires_at=pending.expires_at))
            await db.commit()
        raise object_store.ObjectStoreError("store_error")
    monkeypatch.setattr(st, "put_named", reused_then_failed)
    assert (await clients.post("/media", content=PNG, headers={"content-type": "image/png"})).status_code == 503
    assert (await clients.get("/m/someone-else")).content == PNG
