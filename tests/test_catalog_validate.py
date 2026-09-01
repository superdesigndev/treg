import json

from scripts import catalog_validate as validator


def test_cost_modifiers_accept_only_supported_declarative_credit_rules():
    base = {
        "type": "per_success", "value": 5, "currency": "credit", "per": 1,
        "unit": "call", "source": "docs", "source_url": "https://example.com/pricing",
        "checked": "2026-08-25", "confidence": "documented",
    }
    errors: list[str] = []
    validator.check_cost(base | {"settle": "modifiers", "modifiers": {
        "preview": {"location": "query", "when": "truthy", "set_credits": 0},
        "email": {"location": "lookups", "when": "present", "add_credits": 3,
                  "reserve_only": True},
        "enrich": {"location": "query", "when": "truthy", "add_credits_per_result": 1},
    }}, "catalog:test", errors, [])
    assert errors == []

    broken: list[str] = []
    validator.check_cost(base | {"modifiers": {
        "preview": {"location": "headers", "set_credits": 1},
        "email": {"add_credits": -1, "add_credits_per_result": 2},
        "rescrape": {"add_credits": 2, "reserve_only": "yes"},
        "enrich": {"add_credits_per_result": 1, "reserve_only": True},
    }}, "catalog:test", broken, [])
    assert any("location must be query, body, or lookups" in error for error in broken)
    assert any("set_credits currently supports only the free value 0" in error for error in broken)
    assert any("needs exactly one credit effect" in error for error in broken)
    assert any("add_credits must be a non-negative number" in error for error in broken)
    assert any("reserve_only must be a boolean" in error for error in broken)
    assert any("reserve_only currently supports only add_credits" in error for error in broken)

    bad_settle: list[str] = []
    validator.check_cost(base | {"settle": "estimate"}, "catalog:test", bad_settle, [])
    assert any("cost.settle currently supports only 'base' or 'modifiers'" in error for error in bad_settle)


def test_status_marker_references_must_exist_and_end_at_a_live_endpoint():
    statuses = {"provider.old": "retired", "provider.live": "", "provider.dead": "broken"}

    errors: list[str] = []
    validator.check_status_marker(
        {"id": "provider.old", "status": "retired", "status_note": "moved",
         "superseded_by": "provider.live"},
        "catalog:provider.old", statuses, errors,
    )
    assert errors == []

    broken: list[str] = []
    validator.check_status_marker(
        {"id": "provider.old", "status": "retired", "status_note": "",
         "superseded_by": "provider.missing"},
        "catalog:provider.old", statuses, broken,
    )
    validator.check_status_marker(
        {"id": "provider.old", "status": "retired", "status_note": "moved",
         "superseded_by": "provider.dead"},
        "catalog:provider.old", statuses, broken,
    )
    validator.check_status_marker(
        {"id": "provider.old", "status": "Retired", "status_note": "wrong spelling"},
        "catalog:provider.old", statuses, broken,
    )
    assert any("requires a non-empty status_note" in error for error in broken)
    assert any("is not a catalog endpoint id" in error for error in broken)
    assert any("is itself broken" in error for error in broken)
    assert any("status 'Retired' not one of" in error for error in broken)


def test_verified_example_must_satisfy_its_declared_expectation(tmp_path):
    example = tmp_path / "examples" / "task.json"
    example.parent.mkdir()
    example.write_text(json.dumps({"tasks": [{"status_code": 20100}]}))
    endpoint = {
        "verified": "2026-08-31",
        "example_response": "examples/task.json",
        "expect": {"json_path": "tasks.0.status_code", "equals": 20100},
    }
    original = validator.CATALOG
    try:
        validator.CATALOG = tmp_path
        errors: list[str] = []
        validator.check_verified_example(endpoint, "catalog:task", errors)
        assert errors == []

        endpoint["expect"]["equals"] = 20000
        validator.check_verified_example(endpoint, "catalog:task", errors)
    finally:
        validator.CATALOG = original
    assert errors == [
        "catalog:task: verified example fails expect: tasks.0.status_code=20100, wanted 20000"
    ]


def test_verified_example_must_be_readable_json(tmp_path):
    example = tmp_path / "broken.json"
    example.write_text("not json")
    endpoint = {
        "verified": "2026-08-31",
        "example_response": "broken.json",
        "expect": {"json_path": "ok", "equals": True},
    }
    original = validator.CATALOG
    try:
        validator.CATALOG = tmp_path
        errors: list[str] = []
        validator.check_verified_example(endpoint, "catalog:broken", errors)
    finally:
        validator.CATALOG = original
    assert len(errors) == 1
    assert "is not readable JSON" in errors[0]
