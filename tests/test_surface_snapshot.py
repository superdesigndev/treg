"""Mechanical guard for the pre-refactor FastAPI application surface."""

from __future__ import annotations

import pytest

from scripts.dump_surface import SNAPSHOT_FILES, render_snapshots


UPDATE_MESSAGE = (
    "Surface snapshot changed. If this is an intentional behavior change, run "
    "uv run --frozen python scripts/dump_surface.py to regenerate the baselines and commit "
    "them in the same PR as the code change."
)


@pytest.mark.parametrize("name", SNAPSHOT_FILES)
def test_surface_snapshot(name: str, rendered_snapshots: dict[str, bytes]) -> None:
    if not SNAPSHOT_FILES[name].exists():
        pytest.fail(f"{UPDATE_MESSAGE}\nMissing snapshot: {name}")
    expected = SNAPSHOT_FILES[name].read_bytes()
    actual = rendered_snapshots[name]
    if actual == expected:
        return

    mismatch = next(
        (
            index
            for index, pair in enumerate(zip(actual, expected, strict=False))
            if pair[0] != pair[1]
        ),
        min(len(actual), len(expected)),
    )
    pytest.fail(f"{UPDATE_MESSAGE}\nSnapshot: {name}\nFirst differing byte: {mismatch}")


@pytest.fixture(scope="module")
def rendered_snapshots() -> dict[str, bytes]:
    return render_snapshots()
