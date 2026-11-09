"""Pricing imports must never silently lose their private evidence."""
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def ingest():
    path = Path(__file__).parents[1] / "scripts/catalog_ingest.py"
    spec = importlib.util.spec_from_file_location("catalog_evidence_ingest", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_evidence_directory_fails(ingest, monkeypatch):
    monkeypatch.delenv("TREG_CATALOG_EVIDENCE_DIR", raising=False)
    with pytest.raises(ValueError, match="TREG_CATALOG_EVIDENCE_DIR"):
        ingest._anyapi_measured()


def test_missing_snapshot_fails(ingest, monkeypatch, tmp_path):
    monkeypatch.setenv("TREG_CATALOG_EVIDENCE_DIR", str(tmp_path))
    with pytest.raises(FileNotFoundError, match="Missing pricing evidence"):
        ingest._anyapi_measured()


def test_supplied_snapshot_preserves_prices(ingest, monkeypatch, tmp_path):
    monkeypatch.setenv("TREG_CATALOG_EVIDENCE_DIR", str(tmp_path))
    prices = {"example.search": {"calls": 8, "p90_usd": 0.04}}
    (tmp_path / "anyapi_measured_charges.json").write_text(json.dumps({
        "skus": prices, "as_of": "2025-01-01", "window_days": 30,
    }))
    assert ingest._anyapi_measured() == prices


def test_a_reingest_writes_the_generated_cost_note_over_the_carried_cost(ingest, monkeypatch, tmp_path):
    monkeypatch.setattr(ingest, "CATALOG", tmp_path)
    row = {"id": "anyapi.web.x", "method": "POST", "path": "/v1/run/web.x", "added": "2026-09-19"}
    old_cost = {"type": "per_success", "value": 0.0036, "currency": "USD", "unit": "call",
                "source": "observed", "note": ingest.anyapi_cost_note(2, 0.036)}
    (tmp_path / "anyapi.extended.yaml").write_text(json.dumps(
        {"provider": "anyapi", "endpoints": [row | {"cost": old_cost, "verified": "2026-09-19"}]}))
    fresh = row | {"cost": {**old_cost, "value": 0.0012, "source": "rate_card_api",
                            "note": ingest.anyapi_cost_note(1, 0.0012)}}
    ingest.carry_verification("anyapi", [fresh])
    assert fresh["cost"] == {**old_cost, "note": "One source serves this, at up to $0.0012 a call. The charge "
                             "follows the answer's `costUsd`, not the listed price. Send X-Treg-Route-Max-Cost "
                             "to refuse any source dearer than your ceiling."}
