"""The migration history is one line: one head, and every revision's parent exists.

Two branches that each add the next revision number would otherwise merge into two heads, and
`python -m treg upgrade` would refuse to choose between them at deploy time.
"""

from __future__ import annotations

import ast
from pathlib import Path

from alembic.script import ScriptDirectory

from treg.maintenance import _alembic_config

VERSIONS = Path(__file__).parents[1] / "src" / "treg" / "alembic" / "versions"


def test_alembic_has_a_single_head():
    script = ScriptDirectory.from_config(_alembic_config())
    heads = script.get_heads()
    assert len(heads) == 1, f"multiple alembic heads: {heads}; renumber one revision onto the other"
    revisions = list(script.walk_revisions())  # raises if any down_revision is missing
    assert revisions[-1].down_revision is None


def test_the_request_admission_revision_only_creates_new_tables():
    tree = ast.parse((VERSIONS / "0072_request_admission.py").read_text())
    upgrade = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade")
    calls = [n for n in ast.walk(upgrade) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and isinstance(n.func.value, ast.Name) and n.func.value.id == "op"]
    created = {c.args[0].value for c in calls if c.func.attr == "create_table"}
    assert created == {"ratelimitconfigversion", "blocklistentry", "blocklistaudit", "blocklistversion", "surgesignal"}
    assert {c.func.attr for c in calls} <= {"create_table", "create_index", "bulk_insert"}
    assert all(c.args[1].value in created for c in calls if c.func.attr == "create_index")
