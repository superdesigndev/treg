"""Executable boundaries for the Stage 4 call runtime."""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

from treg import bootstrap
from treg.application import billing
from treg.application.call import authorize, overflow, reserve, service, settle
from treg.domain import money
from treg.domain.capacity import marks as capacity_marks


_SRC = Path(__file__).parents[1] / "src" / "treg"

_DATAPLANE_DERIVED_WRITES = {
    "auto_topup_task": (
        (reserve._platform_reserve, "billing.maybe_schedule_autotopup"),
        (billing.maybe_schedule_autotopup, "loop.create_task"),
    ),
    "public_demo_ratestore_hit": (
        (authorize.authorize_call, "publicdemo_policy.enforce_public_demo_ip_cap"),
    ),
    "sandbox_ratestore_hit": (
        (authorize.enforce_public_demo_limit, "publicdemo_policy.enforce_public_demo_ip_cap"),
    ),
    "first_call_adconversion_outbox": (
        (settle._record_first_call, "adsconv.queue"),
    ),
    "lazy_stale_hold_reap": (
        (money.reserve_in_transaction, "reap_stale_holds"),
        (money.reap_stale_holds, "release"),
    ),
    # Plan §4.1 / refactor plan §1.6 "platform-account capacity marks": a confirmed balance/quota
    # signature on treg's own key marks the provider exhausted in ratestore (the shared Ephemeral
    # table) AFTER the settle, so the next call is refused before a hold exists.
    "capacity_exhausted_mark": (
        (settle._note_capacity_signal, "capacity_marks.strike"),
        (settle._note_capacity_recovery, "capacity_marks.clear"),
        (overflow._maybe_overflow_attempt, "capacity_marks.strike"),
        (capacity_marks.strike, "ratestore.kv_put"),
        (capacity_marks.clear, "ratestore.kv_pop"),
    ),
    # Plan §4.3 step 5: the overflow child's settle folds the aggregator's daily spend delta into
    # the SAME transaction; shadow mode records the probe's cost on its own short session.
    "overflow_spend_in_settle": (
        (settle._platform_settle, "overflow_spend_ledger.add_in_transaction"),
        (overflow._record_shadow, "overflow_spend_ledger.add_in_transaction"),
        (overflow._finish_budget, "overflow_spend_ledger.add_in_transaction"),
        (overflow._preserve_unknown_budget, "overflow_spend_ledger.add_in_transaction"),
    ),
    "overflow_budget_reservation": (
        (overflow._maybe_overflow_attempt, "overflow_spend_ledger.reserve_in_transaction"),
        (overflow._release_budget, "overflow_spend_ledger.release_reservation_in_transaction"),
    ),
    "async_result_ownership": (
        (service._execute_call, "async_task_app.remember_result_from_poll"),
    ),
    "async_resource_ownership": (
        (service._execute_call, "async_task_app.remember_platform_resources"),
    ),
}
_EXPECTED_DATAPLANE_WRITES = frozenset({
    "auto_topup_task",
    "public_demo_ratestore_hit",
    "sandbox_ratestore_hit",
    "first_call_adconversion_outbox",
    "lazy_stale_hold_reap",
    "capacity_exhausted_mark",
    "overflow_spend_in_settle",
    "overflow_budget_reservation",
    "async_result_ownership",
    "async_resource_ownership",
})
_DERIVED_WRITE_FILES = {
    _SRC / "application" / "billing.py": {"loop.create_task"},
    _SRC / "application" / "call" / "authorize.py": {
        "publicdemo_policy.enforce_public_demo_ip_cap",
    },
    _SRC / "application" / "call" / "reserve.py": {"billing.maybe_schedule_autotopup"},
    _SRC / "application" / "call" / "settle.py": {
        "adsconv.queue", "capacity_marks.strike", "capacity_marks.clear",
        "overflow_spend_ledger.add_in_transaction",
    },
    _SRC / "application" / "call" / "overflow.py": {
        "capacity_marks.strike", "overflow_spend_ledger.add_in_transaction",
        "overflow_spend_ledger.reserve_in_transaction",
        "overflow_spend_ledger.release_reservation_in_transaction",
    },
    _SRC / "application" / "call" / "service.py": {
        "async_task_app.remember_result_from_poll",
        "async_task_app.remember_platform_resources",
    },
    _SRC / "domain" / "capacity" / "marks.py": {"ratestore.kv_put", "ratestore.kv_pop"},
    _SRC / "domain" / "governance" / "publicdemo.py": {
        "ratestore.sweep", "ratestore.rate_check",
    },
    _SRC / "domain" / "money" / "__init__.py": {"reap_stale_holds", "release"},
}
_EXPECTED_DERIVED_WRITE_SITES = {
    ("application/billing.py", "maybe_schedule_autotopup", "loop.create_task"),
    ("application/call/authorize.py", "authorize_call",
     "publicdemo_policy.enforce_public_demo_ip_cap"),
    ("application/call/authorize.py", "enforce_public_demo_limit",
     "publicdemo_policy.enforce_public_demo_ip_cap"),
    ("application/call/reserve.py", "_platform_reserve",
     "billing.maybe_schedule_autotopup"),
    ("application/call/settle.py", "_record_first_call", "adsconv.queue"),
    ("application/call/settle.py", "_note_capacity_signal", "capacity_marks.strike"),
    ("application/call/settle.py", "_note_capacity_recovery", "capacity_marks.clear"),
    ("application/call/settle.py", "_platform_settle", "overflow_spend_ledger.add_in_transaction"),
    ("application/call/settle.py", "_close", "overflow_spend_ledger.add_in_transaction"),
    ("application/call/overflow.py", "_maybe_overflow_attempt", "capacity_marks.strike"),
    ("application/call/overflow.py", "_record_shadow", "overflow_spend_ledger.add_in_transaction"),
    ("application/call/overflow.py", "_finish_budget", "overflow_spend_ledger.add_in_transaction"),
    ("application/call/overflow.py", "_preserve_unknown_budget", "overflow_spend_ledger.add_in_transaction"),
    ("application/call/overflow.py", "_maybe_overflow_attempt",
     "overflow_spend_ledger.reserve_in_transaction"),
    ("application/call/overflow.py", "_release_budget",
     "overflow_spend_ledger.release_reservation_in_transaction"),
    ("application/call/service.py", "_execute_call",
     "async_task_app.remember_result_from_poll"),
    ("application/call/service.py", "_execute_call",
     "async_task_app.remember_platform_resources"),
    ("domain/capacity/marks.py", "strike", "ratestore.kv_put"),
    ("domain/capacity/marks.py", "clear", "ratestore.kv_pop"),
    ("domain/governance/publicdemo.py", "enforce_public_demo_ip_cap", "ratestore.rate_check"),
    ("domain/governance/publicdemo.py", "enforce_public_demo_ip_cap", "ratestore.sweep"),
    ("domain/money/__init__.py", "reap_stale_holds", "release"),
    ("domain/money/__init__.py", "reserve_in_transaction", "reap_stale_holds"),
}


def _call_names(source: str) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(textwrap.dedent(source))):
        if not isinstance(node, ast.Call):
            continue
        parts = []
        current = node.func
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        if parts:
            names.add(".".join(reversed(parts)))
    return names


def _forbidden_imports(source: str, forbidden: tuple[str, ...]) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(source)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            if any(name == root or name.startswith(root + ".") for root in forbidden):
                found.add(name)
    return found


def _package_forbidden_imports(package: Path, forbidden: tuple[str, ...]) -> set[str]:
    return set().union(*(
        _forbidden_imports(path.read_text(), forbidden) for path in package.rglob("*.py")
    ))


def _transaction_calls(source: str) -> set[str]:
    return _call_names(source) & {"db.commit", "db.rollback"}


def _validate_write_allowlist(allowlist) -> None:
    assert set(allowlist) == _EXPECTED_DATAPLANE_WRITES
    for anchors in allowlist.values():
        for owner, expected_call in anchors:
            assert expected_call in _call_names(inspect.getsource(owner))


def _derived_write_sites(overrides: dict[Path, str] | None = None) -> set[tuple[str, str, str]]:
    sites = set()
    for path, markers in _DERIVED_WRITE_FILES.items():
        tree = ast.parse((overrides or {}).get(path, path.read_text()))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in _call_names(ast.unparse(node)) & markers:
                sites.add((str(path.relative_to(_SRC)), node.name, call))
    return sites


def test_call_runtime_import_edges_point_inward() -> None:
    call_forbidden = ("treg.api", "treg.bootstrap", "treg.routers", "fastapi", "starlette")
    upstream_forbidden = call_forbidden
    assert _package_forbidden_imports(_SRC / "application" / "call", call_forbidden) == set()
    assert _package_forbidden_imports(_SRC / "infra" / "upstream", upstream_forbidden) == set()
    async_forbidden = ("treg.api", "treg.routers", "treg.application", "treg.audit")
    assert _package_forbidden_imports(_SRC / "domain" / "asynctasks", async_forbidden) == set()


def test_catalog_access_router_only_translates_the_application_result() -> None:
    tree = ast.parse((_SRC / "routers" / "call.py").read_text())
    owner = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "catalog_endpoint_access"
    )
    body = "\n".join(ast.unparse(statement) for statement in owner.body)
    assert _call_names(body) == {
        "get_catalog_endpoint_access",
        "_translate_call_failure",
    }


@pytest.mark.parametrize(
    ("package", "forbidden", "mutation"),
    [
        ("application/call", ("treg.api",), "from treg.api import app\n"),
        ("application/call", ("fastapi",), "from fastapi import Request\n"),
        ("infra/upstream", ("treg.routers",), "from treg.routers import call\n"),
        ("domain/asynctasks", ("treg.application",), "from treg.application import asynctasks\n"),
    ],
)
def test_import_edge_contracts_reject_mutations(package, forbidden, mutation) -> None:
    assert _package_forbidden_imports(_SRC / package, forbidden) == set()
    assert _forbidden_imports(mutation, forbidden)


def test_startup_manifests_keep_dataplane_and_control_work_separate() -> None:
    assert bootstrap.ROLE_BACKGROUND_TASKS == {
        "all": ("treg.adsconv.worker",),
        "dataplane": (),
        "control": ("treg.adsconv.worker",),
    }
    for checks in bootstrap.ROLE_STARTUP_CHECKS.values():
        assert "treg.api._backfill_provider_extra_tools" not in checks
        assert "treg.api._bootstrap_single_user" not in checks
    assert "treg.mcp.mcp_lifespan" in bootstrap.ROLE_STARTUP_CHECKS["dataplane"]
    assert "treg.mcp.mcp_lifespan" not in bootstrap.ROLE_STARTUP_CHECKS["control"]


def test_dataplane_derived_write_allowlist_is_explicit_and_live() -> None:
    _validate_write_allowlist(_DATAPLANE_DERIVED_WRITES)
    assert _derived_write_sites() == _EXPECTED_DERIVED_WRITE_SITES


def test_dataplane_write_allowlist_rejects_an_unlisted_mutation() -> None:
    mutated = dict(_DATAPLANE_DERIVED_WRITES)
    mutated["unreviewed_request_write"] = ((settle._record_first_call, "db.commit"),)
    with pytest.raises(AssertionError):
        _validate_write_allowlist(mutated)
    path = _SRC / "application" / "call" / "settle.py"
    source = path.read_text() + "\nasync def unreviewed_write(db, org):\n    await adsconv.queue(db, org, 'x')\n"
    assert _derived_write_sites({path: source}) != _EXPECTED_DERIVED_WRITE_SITES


@pytest.mark.parametrize(
    "owner",
    [
        money.reserve_in_transaction,
        # The private bodies, not the public delegating wrappers: settle_in_transaction and
        # release_in_transaction are 3-line pass-throughs, so inspecting them proves nothing —
        # a commit injected into the real logic sailed past the wrapper-keyed version of this test.
        money._settle_in_transaction,
        money._release_in_transaction,
        money.settle_in_transaction,
        money.release_in_transaction,
        # The funding primitives are their own real bodies - no committing wrapper exists to hide
        # behind, so listing them here scans the actual logic (the lesson from the wrapper-keyed
        # version above). Their savepoint (`begin_nested`) is invisible to this scanner on purpose:
        # it confines a lost idempotency race, it does not end the caller's transaction.
        money.grant,
        money.topup,
    ],
)
def test_call_money_transaction_primitives_never_commit(owner) -> None:
    source = inspect.getsource(owner)
    assert _transaction_calls(source) == set()
    mutated = source.rstrip() + "\n    await db.commit()\n"
    assert _transaction_calls(mutated) == {"db.commit"}


def test_lazy_reap_keeps_its_independent_committing_boundary() -> None:
    assert "reap_stale_holds" in _call_names(inspect.getsource(money.reserve_in_transaction))
    assert "release" in _call_names(inspect.getsource(money.reap_stale_holds))
    assert "db.commit" in _call_names(inspect.getsource(money.release))
