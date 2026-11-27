"""TREG_NO_CACHE_PROVIDERS: a deployment keeps nothing from the providers it names.

Every endpoint of a named provider loads as `cache: forbidden` and `response_retention: forbidden`,
so the archive never stores or replays its answers and no body reaches call events or failure
evidence. Other providers keep their catalog declarations; empty names nobody.
"""

from __future__ import annotations

from pathlib import Path

from treg import archive
from treg.config import get_settings
from treg.domain.catalog import store

_ROW = {"id": "people.search", "capability": "people.search", "method": "GET", "path": "/search",
        "cache": "transient"}


def _loaded(monkeypatch, value: str | None, provider: str) -> dict:
    if value is None:
        monkeypatch.delenv("TREG_NO_CACHE_PROVIDERS", raising=False)
    else:
        monkeypatch.setenv("TREG_NO_CACHE_PROVIDERS", value)
    get_settings.cache_clear()
    try:
        return store._normalize({**_ROW, "id": f"{provider}.{_ROW['id']}"}, provider, Path("."))
    finally:
        get_settings.cache_clear()


def test_named_provider_keeps_nothing_and_others_are_untouched(monkeypatch):
    before = _loaded(monkeypatch, None, "apollo")
    assert archive.storable(before) and before["response_retention"] is None
    assert _loaded(monkeypatch, "", "apollo")["cache"] == "transient"

    after = _loaded(monkeypatch, " Apollo , ", "apollo")
    assert archive.policy(after) == archive.CACHE_FORBIDDEN and after["response_retention"] == "forbidden"
    other = _loaded(monkeypatch, " Apollo , ", "hunter")
    assert (other["cache"], other["response_retention"]) == ("transient", None)
