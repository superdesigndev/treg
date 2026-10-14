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
