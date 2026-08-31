"""Explicit manifests for the phase-1 application roles."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from starlette.routing import Mount

from treg import api
from treg.bootstrap import ROLE_STARTUP_CHECKS, create_app


_SNAPSHOT = Path(__file__).parent / "snapshots" / "routes.json"
_CALL_ROUTE = "DELETE,GET,HEAD,OPTIONS,PATCH,POST,PUT /call/{rest:path}"
_CATALOG_CALL_ROUTE = "DELETE,GET,HEAD,OPTIONS,PATCH,POST,PUT /catalog/call/{rest:path}"


def _all_routes() -> list[str]:
    rows = json.loads(_SNAPSHOT.read_text())
    result = []
    for row in rows:
        if row["kind"] == "Mount":
            result.append(f"MOUNT {row['path']}")
        else:
            result.append(f"{','.join(row['methods'])} {row['path']}")
    return result


_ALL_ROUTES = _all_routes()
# MCP is calling traffic: the dataplane owns its mount and its RFC 9728 resource metadata.
_DATAPLANE_ONLY = (
    _CALL_ROUTE,
    _CATALOG_CALL_ROUTE,
    "GET,HEAD /.well-known/oauth-protected-resource/mcp",
    "GET,HEAD /.well-known/oauth-protected-resource/mcp/v2",
    "MOUNT /mcp/v2",
    "MOUNT /mcp",
)
_DATAPLANE_ROUTES = [route for route in _ALL_ROUTES if route in _DATAPLANE_ONLY]
_EXPECTED_ROUTES = {
    "all": _ALL_ROUTES,
    "dataplane": _DATAPLANE_ROUTES,
    "control": [route for route in _ALL_ROUTES if route not in _DATAPLANE_ONLY],
}
_EXPECTED_BACKGROUND_TASKS = {
    "all": ["treg.adsconv.worker"],
    "dataplane": [],
    "control": ["treg.adsconv.worker"],
}
_READ_ONLY_STARTUP_CHECKS = {
    "all": (
        "treg.infra.db.verify_db",
        "app.state.http",
        "treg.mcp.mcp_lifespan",
    ),
    "dataplane": (
        "treg.infra.db.verify_db",
        "app.state.http",
        "treg.mcp.mcp_lifespan",
    ),
    "control": (
        "treg.infra.db.verify_db",
        "app.state.http",
    ),
}


def _actual_routes(app) -> list[str]:
    result = []
    for route in app.routes:
        if isinstance(route, Mount):
            result.append(f"MOUNT {route.path}")
        else:
            methods = ",".join(sorted(getattr(route, "methods", ()) or ()))
            result.append(f"{methods} {route.path}")
    return result


@pytest.mark.parametrize("role", ["all", "dataplane", "control"])
def test_role_manifests_are_explicit_and_match_the_created_app(role):
    app = create_app(role)
    expected = {
        "routes": _EXPECTED_ROUTES[role],
        "background_tasks": _EXPECTED_BACKGROUND_TASKS[role],
        "startup_checks": list(_READ_ONLY_STARTUP_CHECKS[role]),
    }

    assert app.state.role == role
    assert app.state.endpoint_observation_reader is not None
    assert app.state.role_manifest == expected
    assert _actual_routes(app) == expected["routes"]


def test_every_role_startup_manifest_is_pinned_to_read_only_checks():
    """Refactor plan §1.6: startup performs no writes; the PR2 carve-out is retired."""
    assert ROLE_STARTUP_CHECKS == _READ_ONLY_STARTUP_CHECKS


def test_api_app_remains_the_all_role_compatibility_entrypoint():
    assert api.app.state.role == "all"
    assert _actual_routes(api.app) == _EXPECTED_ROUTES["all"]


def test_factory_routes_bind_dependency_overrides_to_the_created_app():
    app = create_app("dataplane")
    route = next(route for route in app.routes if isinstance(route, APIRoute))
    assert route.dependency_overrides_provider is app


def test_unknown_role_is_refused():
    with pytest.raises(ValueError, match="unknown app role"):
        create_app("worker")
