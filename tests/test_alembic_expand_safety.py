"""Architecture guard for expand-only Alembic upgrades and explicit rollback floors."""

from __future__ import annotations

import ast
from pathlib import Path


VERSIONS = Path(__file__).parents[1] / "src" / "treg" / "alembic" / "versions"
ADDITIVE_OPERATIONS = frozenset({
    "add_column",
    "bulk_insert",
    "create_check_constraint",
    "create_foreign_key",
    "create_index",
    "create_table",
    "create_unique_constraint",
    "f",
})


def _module_contract(tree: ast.Module) -> bool:
    for statement in tree.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        if any(isinstance(target, ast.Name) and target.id == "contract" for target in targets):
            return isinstance(statement.value, ast.Constant) and statement.value.value is True
    return False


def _upgrade_function(tree: ast.Module) -> ast.FunctionDef:
    functions = [
        statement
        for statement in tree.body
        if isinstance(statement, ast.FunctionDef) and statement.name == "upgrade"
    ]
    assert len(functions) == 1, "every Alembic revision must define exactly one upgrade()"
    return functions[0]


def _alembic_operations(upgrade: ast.FunctionDef) -> list[tuple[str, int]]:
    batch_names: set[str] = set()
    for node in ast.walk(upgrade):
        if not isinstance(node, ast.withitem) or not isinstance(node.context_expr, ast.Call):
            continue
        function = node.context_expr.func
        if (
            isinstance(function, ast.Attribute)
            and isinstance(function.value, ast.Name)
            and function.value.id == "op"
            and function.attr == "batch_alter_table"
            and isinstance(node.optional_vars, ast.Name)
        ):
            batch_names.add(node.optional_vars.id)

    operations: list[tuple[str, int]] = []
    for node in ast.walk(upgrade):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if not isinstance(owner, ast.Name):
            continue
        if owner.id == "op":
            if node.func.attr != "batch_alter_table":
                operations.append((node.func.attr, node.lineno))
        elif owner.id in batch_names:
            operations.append((node.func.attr, node.lineno))
    return operations


def test_alembic_upgrades_are_expand_only_or_declare_a_rollback_floor():
    failures: list[str] = []

    for path in sorted(VERSIONS.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        contract = _module_contract(tree)
        docstring = ast.get_docstring(tree) or ""

        non_additive = [
            (operation, line)
            for operation, line in _alembic_operations(_upgrade_function(tree))
            if operation not in ADDITIVE_OPERATIONS
        ]

        if non_additive and not contract:
            details = ", ".join(f"{operation} at line {line}" for operation, line in non_additive)
            failures.append(f"{path.name}: non-additive upgrade operation(s): {details}")
        if contract and "rollback floor" not in docstring.lower():
            failures.append(f"{path.name}: contract revision docstring lacks 'rollback floor'")

    assert not failures, (
        "\n".join(failures)
        + "\nMark the revision with module-level contract = True and include 'rollback floor' in "
        "its docstring. This declares a rollback floor."
    )
