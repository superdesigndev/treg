from __future__ import annotations

import json

import yaml

from scripts import catalog_verify_extended as verify

ROW = "demo.x.search"
PRICED = {"value": "0.0102", "currency": "USD", "per": 1, "unit": "call"}


def _merge(tmp_path, monkeypatch, *batches: dict) -> dict:
    monkeypatch.setattr(verify, "CATALOG", tmp_path)
    monkeypatch.setattr(verify, "ROOT", tmp_path)
    file = tmp_path / "demo.extended.yaml"
    file.write_text(yaml.safe_dump({"provider": {"id": "demo"}, "endpoints": [
        {"id": ROW, "verified": "2026-07-28", "price": PRICED,
         "provenance": {"source": "observed", "checked": "2026-07-28", "confidence": "verified"}}]}))
    for n, res in enumerate(batches):
        path = tmp_path / f"batch{n}.json"
        path.write_text(json.dumps({"service": "demo", "results": [{"id": ROW, **res}]}))
        verify.merge_results("demo", [path])
    return yaml.safe_load(file.read_text())["endpoints"][0]


def _observed(value: str, day: str, before: str | None, checked: str) -> dict:
    return {"verified": day, "price": {**PRICED, "value": value},
            "provenance": {"source": "observed", "checked": day, "confidence": "verified"},
            "repriced_from": {"value": before, "checked": checked}}


def test_a_batch_that_observed_no_cost_never_reprices_a_row(tmp_path, monkeypatch):
    # an older verifier exported the row's price as it stood, same-day provenance included
    stale = {"verified": "2026-11-26", "price": PRICED,
             "provenance": {"source": "observed", "checked": "2026-11-26", "confidence": "verified"}}
    row = _merge(tmp_path, monkeypatch, _observed("0", "2026-11-26", "0.0102", "2026-07-28"), {"skipped": "budget"}, stale)
    assert row["price"]["value"] == "0" and "settle" in row and row["settle"] is None
    old_free = {**stale, "price": {**PRICED, "value": "0"}}
    row = _merge(tmp_path, monkeypatch, old_free)
    assert row["price"]["value"] == "0.0102" and "settle" not in row


def test_a_replayed_batch_never_undoes_a_later_observation(tmp_path, monkeypatch):
    free = _observed("0", "2026-11-25", "0.0102", "2026-07-28")
    priced = _observed("0.015", "2026-11-26", "0", "2026-11-25")
    row = _merge(tmp_path, monkeypatch, free, priced, free)
    assert row["price"]["value"] == "0.015" and "settle" not in row
    # the price came back to the one the free batch replaced, but from a later observation
    back = _observed("0.0102", "2026-11-26", "0", "2026-11-25")
    row = _merge(tmp_path, monkeypatch, free, back, free)
    assert row["price"]["value"] == "0.0102" and row["provenance"]["checked"] == "2026-11-26"
    assert "settle" not in row


def test_an_observed_price_brings_its_settle_with_it(tmp_path, monkeypatch):
    row = _merge(tmp_path, monkeypatch, _observed("0", "2026-11-26", "0.0102", "2026-07-28"))
    assert row["price"]["value"] == "0" and row["settle"] is None
