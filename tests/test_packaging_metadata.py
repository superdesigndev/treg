"""Packaging metadata stays aligned with LICENSE and public messaging."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_pyproject_license_classifier_is_apache_not_proprietary() -> None:
    assert "License :: OSI Approved :: Apache Software License" in PYPROJECT
    assert "Proprietary License" not in PYPROJECT