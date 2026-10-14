"""The graph of a steps recipe — derived from the references, never declared by hand.

An edge goes from the step a reference names to the step that names it. A step that names no
other step has no parent and starts at once. Refused at load: a reference to a step that does
not exist, and any cycle. The `wave` of a step is its depth in the graph (longest path from a
root), used for the trace; the runner schedules dynamically (every ready step, four at a time),
which never runs a step before its wave.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import refs
from .manifest import ManifestError


@dataclass(frozen=True)
class Graph:
    order: tuple[str, ...]                  # a topological order, for determinism
    parents: dict[str, frozenset[str]]      # step → the steps it reads
    wave: dict[str, int]                    # step → depth, 0 for a root
    positions: dict[str, str]               # "0" → first step's name (the $0 alias)
    steps: dict[str, dict[str, Any]] = field(default_factory=dict)


def build(steps: list[dict[str, Any]]) -> Graph:
    names = [s["name"] for s in steps]
    by_name = {s["name"]: s for s in steps}
    positions = {str(i): n for i, n in enumerate(names)}
    parents: dict[str, set[str]] = {}
    for i, step in enumerate(steps):
        own_item = step.get("as")
        roots: set[str] = set()
        roots |= refs.roots_in(step.get("input", {}))
        for key in ("for_each", "skip_if_empty"):
            if step.get(key):
                roots |= refs.roots_in(step[key])
        deps: set[str] = set()
        for root in roots:
            if root == "input" or root == own_item:
                continue
            target = positions.get(root) if root.isdigit() else root
            if target is None or target not in by_name:
                raise ManifestError(f"steps[{i}]", f"references ${root}, which is not a step or an input")
            if target == step["name"]:
                raise ManifestError(f"steps[{i}]", f"step {target!r} references itself")
            deps.add(target)
        parents[step["name"]] = deps

    # Kahn's algorithm: an order exists exactly when there is no cycle.
    remaining = {n: set(p) for n, p in parents.items()}
    order: list[str] = []
    wave: dict[str, int] = {}
    while remaining:
        ready = [n for n in names if n in remaining and not remaining[n]]
        if not ready:
            cyc = ", ".join(sorted(remaining))
            raise ManifestError("steps", f"a cycle: these steps wait on each other: {cyc}")
        for n in ready:
            wave[n] = max((wave[p] + 1 for p in parents[n]), default=0)
            order.append(n)
            del remaining[n]
        for n in remaining:
            remaining[n] -= set(ready)
    return Graph(order=tuple(order), parents={n: frozenset(p) for n, p in parents.items()},
                 wave=wave, positions=positions, steps=by_name)
