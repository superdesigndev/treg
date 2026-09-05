"""The bulkhead: three pools against one database, and the wiring that keeps them apart.

Found live 2026-09-03 — one admin browser tab polling `/admin/archive/panel` held a third of the
single 15-slot pool for two hours, and ~10,300 real `/call/` requests were refused as saturated.
Sizing would not have prevented it and neither would a semaphore: a semaphore bounds only the module
that remembers to take one (`audit.py` did, `archive.py` did not), while a pool bounds every module
routed to it. So these tests pin the ROUTING — which maker each class of work reaches for — because
that is the part a future writer can silently get wrong.

`EXPECTED_MAKERS` below is a closed set on purpose: a module that starts opening sessions has to be
classified here, so "nobody thought about which pool this belongs on" fails rather than defaults.
"""

from __future__ import annotations

import ast
import functools
import pathlib

import pytest
from fastapi import routing as fastapi_routing

from treg import bootstrap
from treg.infra import db as infra_db

SRC = pathlib.Path(infra_db.__file__).parent.parent   # src/treg/

API, ADMIN, BACKGROUND = "session_maker", "admin_session_maker", "background_session_maker"

# Every module that opens a session, and the pool(s) it is allowed to reach for. Modules with two
# entries genuinely serve two classes of work and each is justified where it is imported.
EXPECTED_MAKERS: dict[str, set[str]] = {
    # Request path.
    "api.py": {API}, "mcp.py": {API}, "routers/resources.py": {API},
    "application/auth.py": {API}, "application/billing.py": {API}, "application/connect.py": {API},
    "application/asynctasks.py": {API},
    "application/referrals.py": {API}, "application/signup.py": {API},
    "application/onboard/__init__.py": {API},
    "application/call/authorize.py": {API}, "application/call/idempotency.py": {API},
    "application/call/intake.py": {API}, "application/call/overflow.py": {API},
    "application/call/reserve.py": {API}, "application/call/resolve.py": {API},
    "application/call/route.py": {API}, "application/call/service.py": {API},
    "application/call/settle.py": {API},
    "domain/capacity/marks.py": {API}, "domain/capacity/routes_view.py": {API},
    "domain/capacity/view.py": {API},
    # `treg-worker` is its own process; it shares the API pool because nothing else is running in it.
    "worker.py": {API},
    # Staff pages take their pool through `Depends(get_admin_session)`, not a maker import; the one
    # maker here is the retention sweep, which is background work and must not nest inside a request.
    "routers/admin.py": {BACKGROUND},
    # Off-request writers.
    "audit.py": {BACKGROUND},
    "bootstrap.py": {BACKGROUND},
    # `lookup` is on the API pool inside a caller's /call/; every write here is background.
    "archive.py": {API, BACKGROUND},
}


def _maker_names(tree: ast.AST) -> set[str]:
    """Both spellings. `from ...infra.db import session_maker` is the common one, but `application/
    auth.py` reaches it as `database.session_maker` off a module import — a form the first version
    of this guard missed entirely, which is the whole failure mode it exists to prevent."""
    names = {alias.name
             for node in ast.walk(tree)
             if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("db")
             for alias in node.names if alias.name.endswith("session_maker")}
    names |= {node.attr for node in ast.walk(tree)
              if isinstance(node, ast.Attribute) and node.attr.endswith("session_maker")}
    return names


@functools.cache
def _modules_opening_sessions() -> dict[str, ast.AST]:
    out = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        if _maker_names(tree):
            out[path.relative_to(SRC).as_posix()] = tree
    return out


def test_every_module_that_opens_a_session_is_classified():
    """The guard that outlives this PR. A new module reaching for a pool must say which one and be
    listed above; the previous version of this test was parametrized over a single file and would
    have noticed none of it."""
    assert set(_modules_opening_sessions()) == set(EXPECTED_MAKERS)


@pytest.mark.parametrize("module_file", sorted(EXPECTED_MAKERS))
def test_each_module_reaches_only_for_its_own_pools(module_file):
    assert _maker_names(_modules_opening_sessions()[module_file]) == EXPECTED_MAKERS[module_file]


def test_the_three_pools_are_separate_engines_on_a_real_database():
    """On Postgres each class of work gets its own connections; on sqlite there is no pool to
    protect and file-level write locks cannot be shared, so all three alias one engine."""
    engines = {infra_db._engine, infra_db._admin_engine, infra_db._background_engine}
    assert len(engines) == (1 if infra_db._is_sqlite else 3)


def test_no_minor_pool_can_outgrow_its_slots():
    """Overflow is exactly the escape hatch a bulkhead must not have, so it is 0 on both minor
    pools; the API keeps its overflow because refusing a real call is the cost this exists to avoid."""
    assert infra_db.POOL_SPECS["admin"]["max_overflow"] == 0
    assert infra_db.POOL_SPECS["background"]["max_overflow"] == 0
    assert infra_db.POOL_SPECS["api"]["max_overflow"] > 0


def test_the_background_pool_fits_every_consumer_not_just_the_throttled_ones():
    """Sizing the pool below its real concurrent demand makes the bounds fight: the loser waits
    `pool_timeout` and then DROPS its row, which for audit is the failure evidence a burst just
    produced.

    The first version of this test asserted `pool_size >= audit_sem + archive_sem` and passed at 8
    while four more consumers — including `adsconv`, which holds a slot across two Google round
    trips — drew on the same slots. A guard that names a property it does not check is worse than
    no guard, so this walks `BACKGROUND_CONSUMERS` and the two semaphores must match their entries."""
    from treg import archive, audit

    consumers = infra_db.BACKGROUND_CONSUMERS
    assert consumers["audit._write"] == audit._MAX_CONCURRENT_WRITES
    assert consumers["archive._store/_touch"] == archive._MAX_CONCURRENT_WRITES
    assert infra_db.POOL_SPECS["background"]["pool_size"] >= sum(consumers.values())


def test_every_background_session_site_is_named_in_the_consumer_list():
    """The list sizes the pool, so a consumer missing from it is a row silently dropped under load.
    Cross-checked against the modules classified above rather than trusted."""
    background_modules = {m for m, pools in EXPECTED_MAKERS.items() if BACKGROUND in pools}
    named = " ".join(infra_db.BACKGROUND_CONSUMERS)
    for module in background_modules:
        stem = pathlib.Path(module).stem
        assert stem in named or stem in {"bootstrap", "admin"}, (
            f"{module} opens background sessions but names no entry in BACKGROUND_CONSUMERS")


def test_the_spec_is_what_the_engines_were_actually_built_with():
    """The spec is only documentation unless it reaches SQLAlchemy."""
    if infra_db._is_sqlite:
        pytest.skip("sqlite pools are not sized")
    for name, engine in (("api", infra_db._engine), ("admin", infra_db._admin_engine),
                         ("background", infra_db._background_engine)):
        assert engine.pool.size() == infra_db.POOL_SPECS[name]["pool_size"], name
        assert engine.pool._max_overflow == infra_db.POOL_SPECS[name]["max_overflow"], name


def _override(raw: str) -> dict[str, dict[str, int]]:
    base = {"admin": {"pool_size": 2, "max_overflow": 0}}
    return infra_db._apply_overrides({k: dict(v) for k, v in base.items()}, raw)


def test_a_bad_override_is_ignored_rather_than_fatal():
    """This knob gets reached for during an incident, by someone in a hurry. A typo in it must not
    be what stops the server from booting — but the good half of the line still applies."""
    patched = _override("admin.pool_size=9,nonsense,ghost.pool_size=4")
    assert patched["admin"]["pool_size"] == 9


@pytest.mark.parametrize("raw", ["admin.poolsize=16", "admin.pool_sizes=16", "admin.x=4"])
def test_a_misspelled_override_field_is_rejected_not_invented(raw):
    """`specs[pool][field] = ...` happily creates a key nothing reads. The previous version of this
    test used `admin.x=y`, which passed only because `int("y")` raises — it never checked the field
    name at all. An operator who mistypes mid-incident must not be left believing they resized it."""
    assert _override(raw) == {"admin": {"pool_size": 2, "max_overflow": 0}}


@pytest.mark.parametrize("raw,field", [
    ("admin.pool_size=0", "pool_size"),
    ("admin.pool_size=-1", "pool_size"),
    ("admin.max_overflow=-1", "max_overflow"),
])
def test_an_override_cannot_smuggle_in_unlimited_connections(raw, field):
    """SQLAlchemy reads `pool_size=0` and `max_overflow=-1` as UNLIMITED, not as "off" — and
    `pool_size=0` silently sets `_max_overflow=-1` too. Someone typing `-1` to mean "no overflow"
    would uncap connections against the ~100 ceiling: the 2026-08-15 outage, entered through the
    knob added to prevent outages."""
    assert _override(raw)["admin"][field] == {"pool_size": 2, "max_overflow": 0}[field]


def test_the_admin_gate_and_its_handlers_share_one_dependency():
    """FastAPI caches dependencies per request BY IDENTITY, so a gate on `get_session` under a
    handler on `get_admin_session` puts admin traffic back on the API pool through the back door.
    The symptom would be a doubled connection count, not a failure — hence a test."""
    app = bootstrap.create_app()
    admin_routes = [r for r in app.routes
                    if isinstance(r, fastapi_routing.APIRoute) and r.path.startswith("/admin")]
    assert admin_routes, "expected /admin routes to be mounted"
    offenders = []
    for route in admin_routes:
        makers = {d.call for d in route.dependant.dependencies}
        makers |= {d.call for parent in route.dependant.dependencies for d in parent.dependencies}
        if infra_db.get_session in makers:
            offenders.append(route.path)
    assert offenders == []
