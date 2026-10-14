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


def test_supplied_snapshot_preserves_prices_and_window(ingest, monkeypatch, tmp_path):
    monkeypatch.setenv("TREG_CATALOG_EVIDENCE_DIR", str(tmp_path))
    prices = {"example.search": {"calls": 8, "p90_usd": 0.04}}
    (tmp_path / "anyapi_measured_charges.json").write_text(json.dumps({
        "skus": prices, "as_of": "2025-01-01", "window_days": 30,
    }))
    assert ingest._anyapi_measured() == prices
    assert ingest._anyapi_measured_window() == ("2025-01-01", 30)
