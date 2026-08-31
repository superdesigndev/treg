#!/usr/bin/env python3
"""Write deterministic snapshots of treg's public application surface."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


def _configure_test_environment() -> None:
    """Match tests/conftest.py before importing treg, whose settings load at import time."""
    worker = os.environ.get("PYTEST_XDIST_WORKER", "")
    db_dir = os.path.join(tempfile.gettempdir(), "treg-tests")
    os.makedirs(db_dir, exist_ok=True)
    default_db = f"sqlite+aiosqlite:///{db_dir}/treg-test{'-' + worker if worker else ''}.db"
    os.environ["TREG_DATABASE_URL"] = os.environ.get("TREG_TEST_DB_URL", default_db)
    os.environ["TREG_EMAIL_DEV_MODE"] = "true"
    os.environ["TREG_RESEND_API_KEY"] = ""
    os.environ["TREG_RUN_ALLOWED_BINS"] = (
        "sh,echo,true,false,cat,sleep,treg-nonexistent-bin-xyz"
    )
    os.environ["TREG_PROXY_SSRF_CHECK"] = "false"
    os.environ["TREG_CLAUDE_CONNECTOR_ENABLED"] = "true"

    for key in (
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GOOGLE_ADS_DEVELOPER_TOKEN",
        "LINKEDIN_CLIENT_ID",
        "LINKEDIN_CLIENT_SECRET",
        "X_CLIENT_ID",
        "X_CLIENT_SECRET",
        "SLACK_CLIENT_ID",
        "SLACK_CLIENT_SECRET",
        "TIKTOK_CLIENT_KEY",
        "TIKTOK_CLIENT_SECRET",
        "META_CLIENT_ID",
        "META_CLIENT_SECRET",
        "PLATFORM_PROVIDERS",
        "PLATFORM_KEY_TIKHUB",
        "PLATFORM_KEY_DATAFORSEO",
        "PLATFORM_KEY_SCRAPECREATORS",
    ):
        os.environ[f"TREG_{key}"] = ""


_configure_test_environment()

from fastapi.routing import APIRoute  # noqa: E402
from treg.api import app  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_DIR = ROOT / "tests" / "snapshots"
SNAPSHOT_FILES = {
    "routes.json": SNAPSHOT_DIR / "routes.json",
    "composition.json": SNAPSHOT_DIR / "composition.json",
    "openapi.json": SNAPSHOT_DIR / "openapi.json",
    "lifespan.json": SNAPSHOT_DIR / "lifespan.json",
}


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def _routes() -> list[dict[str, Any]]:
    """Keep registration order while stabilizing each route's unordered method set."""
    routes = []
    for route in app.routes:
        methods = getattr(route, "methods", None)
        routes.append(
            {
                "kind": type(route).__name__,
                "methods": sorted(methods) if methods is not None else [],
                "name": route.name,
                "path": route.path,
            }
        )
    return routes


def _qualified_name(value: Any) -> str:
    return f"{value.__module__}.{value.__qualname__}"


def _composition_value(value: Any) -> Any:
    if callable(value):
        return _qualified_name(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return repr(value)


def _composition() -> dict[str, Any]:
    """Capture ordered middleware and exception-handler registration."""
    middleware = []
    for item in app.user_middleware:
        middleware.append(
            {
                "args": [_composition_value(value) for value in item.args],
                "class": _qualified_name(item.cls),
                "kwargs": {
                    key: _composition_value(value) for key, value in item.kwargs.items()
                },
            }
        )
    return {
        "exception_handlers": [
            {
                "exception": (
                    _qualified_name(exception)
                    if isinstance(exception, type)
                    else str(exception)
                ),
                "handler": _qualified_name(handler),
            }
            for exception, handler in app.exception_handlers.items()
        ],
        "middleware": middleware,
    }


def _lifespan() -> dict[str, Any]:
    """List lifespan work explicitly because task creation is not framework-inspectable."""
    return {
        "background_tasks": [
            {
                "condition": "treg.adsconv.enabled()",
                "task": "treg.adsconv.worker",
            }
        ],
        "mounted_lifespans": [
            {
                "condition": "treg.bootstrap._mcp is not None",
                "task": "treg.mcp.mcp_lifespan",
            }
        ],
        "shutdown": [
            {
                "action": "cancel",
                "condition": "treg.adsconv.worker was started",
                "task": "treg.adsconv.worker",
            },
            {"action": "await", "task": "treg.audit.drain"},
            {"action": "await", "task": "treg.analytics.drain"},
            {"action": "await close", "task": "app.state.http"},
        ],
        "startup": [
            {"action": "await", "task": "treg.infra.db.verify_db"},
            {"action": "create", "task": "app.state.http (httpx.AsyncClient)"},
        ],
    }


def _openapi() -> dict[str, Any]:
    """Generate OpenAPI with FastAPI's default operation IDs made deterministic."""
    for route in app.routes:
        if not isinstance(route, APIRoute) or route.operation_id is not None:
            continue
        operation_id = re.sub(r"\W", "_", f"{route.name}{route.path_format}")
        route.unique_id = f"{operation_id}_{sorted(route.methods)[0].lower()}"

    # Another test may have populated the cache before this snapshot is collected.
    app.openapi_schema = None
    return app.openapi()


def render_snapshots() -> dict[str, bytes]:
    """Render every committed snapshot without touching the filesystem."""
    return {
        "routes.json": _json_bytes(_routes()),
        "composition.json": _json_bytes(_composition()),
        "openapi.json": _json_bytes(_openapi()),
        "lifespan.json": _json_bytes(_lifespan()),
    }


def main() -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    snapshots = render_snapshots()
    for name, path in SNAPSHOT_FILES.items():
        path.write_bytes(snapshots[name])
        print(path.relative_to(ROOT))


if __name__ == "__main__":
    main()
