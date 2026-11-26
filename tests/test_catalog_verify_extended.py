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


def test_a_batch_that_observed_no_cost_never_reprices_a_row(tmp_path, monkeypatch):
    free = {"verified": "2026-11-26", "price": {**PRICED, "value": "0"},
            "provenance": {"source": "observed", "checked": "2026-11-26", "confidence": "verified"}}
    skipped = {"skipped": "budget"}
    # an older verifier exported the row's price as it stood, though it called nothing
    stale = {"skipped": "budget", "price": PRICED,
             "provenance": {"source": "observed", "checked": "2026-07-28", "confidence": "verified"}}
    row = _merge(tmp_path, monkeypatch, free, skipped, stale)
    assert row["price"]["value"] == "0" and "settle" in row and row["settle"] is None


def test_an_observed_price_brings_its_settle_with_it(tmp_path, monkeypatch):
    free = {"verified": "2026-11-26", "price": {**PRICED, "value": "0"},
            "provenance": {"source": "observed", "checked": "2026-11-26", "confidence": "verified"}}
    priced = {"verified": "2026-11-27", "price": PRICED,
              "provenance": {"source": "observed", "checked": "2026-11-27", "confidence": "verified"}}
    row = _merge(tmp_path, monkeypatch, free, priced)
    assert row["price"]["value"] == "0.0102" and "settle" not in row
