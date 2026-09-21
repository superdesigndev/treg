"""DataForSEO rules that hold across a whole class of catalog rows.

Live endpoints (/live) accept exactly 1 task per POST body array; multi-task arrays are only
supported by async task_post endpoints. Generic catalog validation cannot know that, so the rule
is checked here over every Live row.

Per-endpoint upstream quirks (a rejected field, a narrower enum, a location/language pair) live in
that row's `note` in the catalog YAML and are deliberately not restated as tests.
"""

import yaml
from pathlib import Path


CATALOG = Path("src/treg/catalog")


def load_dataforseo_endpoints():
    """Load all DataForSEO endpoints from core and extended catalogs.

    Core is a provider document (`endpoints:` list). Extended is the same
    shape after ingest; a bare list is still accepted.
    """
    endpoints = []

    core_path = CATALOG / "dataforseo.yaml"
    if core_path.exists():
        data = yaml.safe_load(core_path.read_text())
        for ep in data.get("endpoints", []):
            ep["_source"] = "dataforseo.yaml"
            endpoints.append(ep)

    extended_path = CATALOG / "dataforseo.extended.yaml"
    if extended_path.exists():
        data = yaml.safe_load(extended_path.read_text())
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("endpoints", [])
        else:
            items = []
        for ep in items:
            ep["_source"] = "dataforseo.extended.yaml"
            endpoints.append(ep)

    return endpoints


def test_live_endpoints_have_single_task_test_requests():
    """DataForSEO Live endpoints accept exactly 1 task per POST array.

    The upstream API documentation states: "each Live API call can contain only
    one task". Async task_post endpoints support up to 100 tasks, but /live
    endpoints reject multi-task arrays.

    Ref: https://docs.dataforseo.com/v3/backlinks/summary/live/
    """
    endpoints = load_dataforseo_endpoints()
    violations = []

    for ep in endpoints:
        path = ep.get("path", "")
        method = ep.get("method", "")

        if "/live" not in path or method != "POST":
            continue

        test_req = ep.get("test_request", {})
        body = test_req.get("body")

        if body is None:
            continue

        if not isinstance(body, list):
            violations.append(f"{ep['id']}: test_request.body is not a list")
            continue

        if len(body) != 1:
            violations.append(
                f"{ep['id']}: Live endpoint test_request.body has {len(body)} tasks, "
                f"expected exactly 1 (Live endpoints do not support multi-task arrays)"
            )

    assert not violations, "DataForSEO Live endpoints must have exactly 1 task:\n" + "\n".join(violations)


