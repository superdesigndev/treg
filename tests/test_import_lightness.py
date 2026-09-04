"""Runtime guard for the base CLI package's import-time dependency surface."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap


_LIGHTWEIGHT_MODULES = (
    "treg.cli",
    "treg.convert",
    "treg.skills",
    "treg.providers",
    "treg.localrun",
    "treg.shell",
    "treg.agents",
    "treg.egress",
    "treg.fsjail",
    # The CLI's `--await` reads the async descriptor through the server's own domain module; it
    # stays stdlib-only so the light install never learns what a database is.
    "treg.domain.asynctasks",
)
_SERVER_DEPENDENCY_ROOTS = (
    "aiosqlite",
    "alembic",
    "asyncpg",
    "cryptography",
    "fastapi",
    "mcp",
    "pydantic",
    "pydantic_core",
    "pydantic_settings",
    "sqlalchemy",
    "sqlmodel",
    "starlette",
    "stripe",
    "uvicorn",
    "yaml",
)


def test_lightweight_modules_do_not_load_server_dependencies():
    script = textwrap.dedent(
        f"""
        import importlib
        import json
        import sys

        for module in {repr(_LIGHTWEIGHT_MODULES)}:
            importlib.import_module(module)

        loaded = sorted(
            dependency
            for dependency in {repr(_SERVER_DEPENDENCY_ROOTS)}
            if dependency in sys.modules
        )
        print(json.dumps(loaded))
        """
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == []
