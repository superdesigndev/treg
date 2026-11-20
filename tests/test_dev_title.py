"""A local dev server marks every page's tab title, so it is told apart from treg.to at a glance."""

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.testclient import TestClient

from treg.bootstrap_http import _DevTitleMiddleware
from treg.config import Settings


def _client() -> TestClient:
    app = FastAPI()
    app.add_middleware(_DevTitleMiddleware)
    app.get("/page")(lambda: HTMLResponse("<html><head><title>Catalog</title></head></html>"))
    app.get("/data")(lambda: JSONResponse({"title": "<title>x</title>"}))
    return TestClient(app)


def test_html_titles_are_marked_and_their_length_follows():
    r = _client().get("/page")
    assert "<title>[dev] Catalog</title>" in r.text
    assert int(r.headers["content-length"]) == len(r.content)


def test_other_responses_pass_through_untouched():
    assert _client().get("/data").json() == {"title": "<title>x</title>"}


def test_only_a_local_sqlite_server_on_a_loopback_url_is_dev():
    local = "sqlite+aiosqlite:///./treg.db"
    assert Settings(database_url=local, public_url="http://127.0.0.1:18790").local_dev
    assert not Settings(database_url=local, public_url="https://treg.to").local_dev
    assert not Settings(database_url="postgresql+asyncpg://db/treg", public_url="http://localhost:8000").local_dev
