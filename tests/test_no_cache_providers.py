"""TREG_NO_CACHE_PROVIDERS: a deployment keeps nothing from the providers it names.

Every endpoint of a named provider loads as `cache: forbidden` and `response_retention: forbidden`,
so the archive never stores or replays its answers and no body reaches call events or failure
evidence. Other providers keep their catalog declarations; empty names nobody.
"""

from __future__ import annotations

import pytest

from treg import archive
from treg.config import get_settings
from treg.domain.catalog import store


def _load(monkeypatch, value: str | None) -> store.Catalog:
    if value is None:
        monkeypatch.delenv("TREG_NO_CACHE_PROVIDERS", raising=False)
    else:
        monkeypatch.setenv("TREG_NO_CACHE_PROVIDERS", value)
    get_settings.cache_clear()
    return store.load(refresh=True)


@pytest.fixture(autouse=True)
def _reset():
    yield
    get_settings.cache_clear()
    store.load(refresh=True)


def _of(catalog: store.Catalog, provider: str) -> list[dict]:
    return [e for e in catalog.by_id.values() if e.get("provider") == provider]


def test_named_provider_keeps_nothing_and_others_are_untouched(monkeypatch):
    before = _load(monkeypatch, None)
    assert any(archive.storable(e) for e in _of(before, "apollo"))
    hunter_before = {e["id"]: (e.get("cache"), e.get("response_retention")) for e in _of(before, "hunter")}

    after = _load(monkeypatch, " Apollo , ")
    apollo = _of(after, "apollo")
    assert apollo and all(
        archive.policy(e) == archive.CACHE_FORBIDDEN and e["response_retention"] == "forbidden" for e in apollo
    )
    assert {e["id"]: (e.get("cache"), e.get("response_retention")) for e in _of(after, "hunter")} == hunter_before
