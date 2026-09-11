"""Validate DataForSEO catalog entries match upstream API constraints.

DataForSEO has provider-specific rules that generic catalog validation can't catch:

1. Live endpoints (/live) accept exactly 1 task per POST body array — multi-task
   arrays are only supported by async task_post endpoints.

2. Google Trends item_types google_trends_topics_list and google_trends_queries_list
   require exactly 1 keyword — multiple keywords with these item_types cause error 40501.

These tests ensure catalog test_requests and documentation stay aligned with live behavior.
"""

import pytest
import yaml
from pathlib import Path


CATALOG = Path("src/treg/catalog")


def load_dataforseo_endpoints():
    """Load all DataForSEO endpoints from core and extended catalogs."""
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
            for ep in data:
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


def test_google_trends_topics_queries_require_single_keyword():
    """Google Trends topics_list/queries_list item_types require exactly 1 keyword.

    The upstream API documentation states: "to obtain google_trends_topics_list
    and google_trends_queries_list items, specify no more than 1 keyword".
    Sending multiple keywords with these item_types causes error 40501 (Invalid Field).

    Ref: https://docs.dataforseo.com/v3/keywords_data/google_trends/explore/live/
    """
    endpoints = load_dataforseo_endpoints()
    restricted_item_types = {"google_trends_topics_list", "google_trends_queries_list"}
    violations = []

    for ep in endpoints:
        path = ep.get("path", "")

        if "google_trends" not in path:
            continue

        test_req = ep.get("test_request", {})
        body = test_req.get("body")

        if not isinstance(body, list) or not body:
            continue

        for i, task in enumerate(body):
            if not isinstance(task, dict):
                continue

            item_types = task.get("item_types", [])
            if not isinstance(item_types, list):
                item_types = [item_types] if item_types else []

            uses_restricted = bool(set(item_types) & restricted_item_types)

            if not uses_restricted:
                continue

            keywords = task.get("keywords", [])
            if not isinstance(keywords, list):
                keywords = [keywords] if keywords else []

            if len(keywords) > 1:
                violations.append(
                    f"{ep['id']}: task[{i}] uses {set(item_types) & restricted_item_types} "
                    f"with {len(keywords)} keywords, but these item_types require exactly 1 keyword"
                )

    assert not violations, "Google Trends constraint violated:\n" + "\n".join(violations)


def test_limits_doc_mentions_single_task_for_live():
    """The provider limits string must clarify Live endpoints accept only 1 task."""
    core_path = CATALOG / "dataforseo.yaml"
    data = yaml.safe_load(core_path.read_text())
    limits = data.get("limits", "")

    assert "Live" in limits, "limits should mention Live endpoint behavior"
    assert "1 task" in limits or "exactly 1" in limits, (
        "limits should state that Live endpoints accept exactly 1 task per POST"
    )


@pytest.mark.parametrize("endpoint_id", [
    "dataforseo.web.backlinks.summary",
    "dataforseo.web.backlinks.list",
    "dataforseo.web.linking_domains.list",
    "dataforseo.web.anchors.list",
    "dataforseo.web.url.metrics",
    "dataforseo.web.backlinks.competitors",
    "dataforseo.google.serp.organic",
    "dataforseo.google.keywords.volume",
    "dataforseo.google.keywords.ideas",
    "dataforseo.google.domain.ranked_keywords",
    "dataforseo.web.page.audit",
])
def test_core_live_endpoints_document_single_task_constraint(endpoint_id):
    """Each core Live endpoint's input.note must mention the single-task constraint."""
    core_path = CATALOG / "dataforseo.yaml"
    data = yaml.safe_load(core_path.read_text())

    endpoint = None
    for ep in data.get("endpoints", []):
        if ep.get("id") == endpoint_id:
            endpoint = ep
            break

    assert endpoint is not None, f"Endpoint {endpoint_id} not found"

    input_spec = endpoint.get("input", {})
    note = input_spec.get("note", "")

    assert "exactly 1" in note.lower() or "do not support multi-task" in note.lower(), (
        f"{endpoint_id}: input.note should clarify Live endpoints accept exactly 1 task"
    )
