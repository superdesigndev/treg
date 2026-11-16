"""The server's entry point serves the FastAPI app itself, and importing the package runs no code."""

from __future__ import annotations

import ast
from pathlib import Path

from fastapi import FastAPI
from uvicorn.importer import import_from_string

_PACKAGE = Path(__file__).resolve().parents[1] / "src" / "treg"


def _served_app_string() -> str:
    tree = ast.parse((_PACKAGE / "__main__.py").read_text())
    targets = [
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "uvicorn"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ]
    assert len(targets) == 1, "python -m treg must hand uvicorn exactly one app import string"
    return targets[0]


def test_package_init_holds_only_a_docstring_and_a_constant_version():
    tree = ast.parse((_PACKAGE / "__init__.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        is_version = (
            isinstance(node, ast.Assign)
            and [getattr(t, "id", None) for t in node.targets] == ["__version__"]
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
        assert is_version, f"src/treg/__init__.py must not run code at import: {ast.unparse(node)[:80]}"


def test_served_app_is_a_plain_fastapi_instance():
    target = _served_app_string()
    assert target == "treg.api:app"
    assert type(import_from_string(target)) is FastAPI
