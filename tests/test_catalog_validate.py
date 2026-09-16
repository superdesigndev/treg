from pathlib import Path

import pytest

from scripts import catalog_validate as validator
from treg.domain.catalog import store as catalog_store


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


def _valid_async():
    return {
        "id_from": "task_id",
        "poll": {"endpoint": "demo.video-gen.status",
                 "param": {"in": "pathParams", "name": "task_id"}},
        "status": {"path": "task.status", "success": ["succeeded"],
                   "failure": ["failed", "cancelled"]},
        "result": {"path": "task.content.url", "ttl_note": "9h"},
        "interval": 10,
    }


def _async_errors(descriptor, cost=None, endpoint_index=None):
    errors: list[str] = []
    default_index = {
        "demo.video-gen.status": {
            "provider": "demo", "kind": "utility", "method": "GET", "path": "/tasks/{task_id}",
            "input": {"pathParams": {"task_id": {"type": "string", "required": True}}},
        },
        "demo.video-gen.content": {
            "provider": "demo", "kind": "utility", "method": "GET", "path": "/content/{video_id}",
            "input": {"pathParams": {"video_id": {"type": "string", "required": True}}},
        },
        "other.video-gen.status": {
            "provider": "other", "kind": "utility", "method": "GET", "path": "/tasks/{task_id}",
            "input": {"pathParams": {"task_id": {"type": "string", "required": True}}},
        },
    }
    validator.check_async_descriptor(
        descriptor, "demo.yaml:submit", "demo", endpoint_index or default_index,
        cost or {"type": "per_success"}, errors,
    )
    return errors


def test_async_descriptor_accepts_both_poll_and_result_modes():
    assert _async_errors(_valid_async()) == []
    dynamic = _valid_async()
    dynamic["poll"] = {"url_from": "urls.get", "url_hosts": ["api.example.com"]}
    dynamic["result"] = {
        "fetch": "demo.video-gen.content",
        "fetch_param": {"in": "pathParams", "name": "video_id", "value_from": "id"}}
    assert _async_errors(dynamic) == []


@pytest.mark.parametrize(("mutate", "message"), [
    (lambda d: d.update(id_from=""), "async.id_from must be a dotted JSON path"),
    (lambda d: d.update(poll=[]), "async.poll must be a mapping"),
    (lambda d: d.update(poll={}), "async.poll needs exactly one"),
    (lambda d: d.update(poll={"endpoint": "demo.video-gen.status", "url_from": "url",
                              "param": {"in": "pathParams", "name": "task_id"},
                              "url_hosts": ["api.example.com"]}),
     "async.poll needs exactly one"),
    (lambda d: d.update(poll={"endpoint": "other.video-gen.status",
                              "param": {"in": "pathParams", "name": "task_id"}}),
     "existing same-provider catalog id"),
    (lambda d: d.update(poll={"endpoint": "demo.video-gen.status"}),
     "requires exactly in, name"),
    (lambda d: d.update(poll={"endpoint": "demo.video-gen.status",
                              "param": {"in": "headers", "name": "task_id"}}),
     "must name an input field in pathParams or queryParams"),
    (lambda d: d.update(poll={"endpoint": "demo.video-gen.status",
                              "param": {"in": "pathParams", "name": "missing"}}),
     "target does not declare input field"),
    (lambda d: d.update(poll={"endpoint": "demo.video-gen.status",
                              "param": {"in": "pathParams", "name": "task_id"},
                              "url_hosts": ["api.example.com"]}),
     "endpoint mode allows only endpoint and param"),
    (lambda d: d.update(poll={"url_from": "url"}), "requires non-empty url_hosts"),
    (lambda d: d.update(poll={"url_from": "url", "url_hosts": [""]}),
     "requires non-empty url_hosts"),
    (lambda d: d.update(poll={"url_from": "url", "url_hosts": ["https://api.example.com"]}),
     "requires non-empty url_hosts"),
    (lambda d: d.update(poll={"url_from": "url", "url_hosts": ["api.example.com"],
                              "param": {"in": "queryParams", "name": "id"}}),
     "url_from mode allows only url_from and url_hosts"),
    (lambda d: d.update(status=[]), "async.status must be a mapping"),
    (lambda d: d["status"].update(path=""), "async.status.path must be a dotted JSON path"),
    (lambda d: d["status"].update(success=[]), "async.status.success must be a non-empty list"),
    (lambda d: d["status"].update(failure=[]), "async.status.failure must be a non-empty list"),
    (lambda d: d["status"].update(failure=["succeeded"]), "must not overlap"),
    (lambda d: d["status"].update(success=[{"done": True}]),
     "values must be non-empty strings or numbers"),
    (lambda d: d.update(result=[]), "async.result must be a mapping"),
    (lambda d: d.update(result={}), "async.result needs exactly one"),
    (lambda d: d.update(result={"path": "url", "fetch": "demo.video-gen.content"}),
     "async.result needs exactly one"),
    (lambda d: d.update(result={"fetch": "other.video-gen.status",
                                "fetch_param": {"in": "pathParams", "name": "task_id",
                                                "value_from": "id"}}),
     "existing same-provider catalog id"),
    (lambda d: d.update(result={"fetch": "demo.video-gen.content"}),
     "requires exactly in, name and value_from"),
    (lambda d: d.update(result={"path": "url", "fetch_param": {
        "in": "pathParams", "name": "video_id", "value_from": "id"}}),
     "path mode allows only path and ttl_note"),
    (lambda d: d.update(result={"path": "url", "ttl_note": ""}),
     "ttl_note must be non-empty"),
    (lambda d: d.update(interval=0), "async.interval must be a positive finite number"),
])
def test_async_descriptor_rejects_each_invalid_contract_shape(mutate, message):
    descriptor = _valid_async()
    mutate(descriptor)
    errors = _async_errors(descriptor)
    assert any(message in error for error in errors), errors


def test_async_descriptor_must_be_a_mapping():
    assert any("async must be a mapping" in error for error in _async_errors([]))


def test_async_descriptor_requires_per_success_cost():
    errors = _async_errors(_valid_async(), {"type": "per_call"})
    assert any("cost.type per_success" in error for error in errors)


def test_async_descriptor_rejects_non_get_or_non_utility_targets():
    target = {
        "demo.video-gen.status": {
            "provider": "demo", "kind": "data", "method": "POST",
            "input": {"pathParams": {"task_id": {"type": "string", "required": True}}},
        },
    }
    errors = _async_errors(_valid_async(), endpoint_index=target)
    assert any("must have kind utility" in error for error in errors)
    assert any("must use GET" in error for error in errors)


def test_async_descriptor_rejects_unknown_keys_and_invalid_json_paths():
    descriptor = _valid_async()
    descriptor["webhook"] = "https://example.com"
    descriptor["result"] = {
        "fetch": "demo.video-gen.content",
        "fetch_param": {"in": "pathParams", "name": "video_id", "value_from": "bad..path"},
    }
    errors = _async_errors(descriptor)
    assert any("async has unknown keys" in error for error in errors)
    assert any("value_from must be a dotted" in error for error in errors)


def test_resource_ownership_contract_validates_ids_and_declared_parameters():
    errors: list[str] = []
    validator.check_resource_ownership(
        {"requires": {"kind": "job", "param": "job_id"},
         "produces": [{"kind": "result", "path": "data.result_id"},
                      {"kind": "task", "path": "tasks.*.id"}]},
        "demo.yaml:status", {"pathParams": {"job_id": {"type": "string"}}}, errors,
    )
    assert errors == []
    validator.check_resource_ownership(
        {"requires": {"kind": "", "param": "missing"},
         "produces": [{"kind": "result", "path": "bad..path"}]},
        "demo.yaml:status", {}, errors,
    )
    assert any("requires needs exactly" in error for error in errors)
    assert any("produces item needs exactly" in error for error in errors)


def test_platform_async_object_reads_cannot_silently_omit_ownership_metadata():
    """A new/edited shared-account task reader must fail CI instead of becoming fail-open."""
    catalog = catalog_store.load()
    missing = []
    for endpoint in catalog.endpoints:
        capability = str(endpoint.get("capability") or "")
        inputs = endpoint.get("input") or {}
        path_ids = [name for name, spec in (inputs.get("pathParams") or {}).items()
                    if isinstance(spec, dict) and spec.get("required")
                    and name.lower().endswith(("id", "_id"))]
        query_ids = [name for name, spec in (inputs.get("queryParams") or {}).items()
                     if isinstance(spec, dict) and spec.get("required")
                     and name.lower().endswith(("id", "_id"))]
        looks_like_object_read = (
            endpoint.get("method") == "GET"
            and capability.endswith((".status", ".results"))
            and (path_ids or (endpoint.get("kind") == "utility" and query_ids))
        )
        if (catalog.platform_eligible(endpoint) and looks_like_object_read
                and not (endpoint.get("resource_ownership") or {}).get("requires")):
            missing.append(endpoint["id"])
    assert missing == []


def test_untracked_extended_async_consumers_are_explicitly_byok_only():
    catalog = catalog_store.load()
    ids = {
        "akta.x.request-status",
        "tikhub.x.youtube-web-v2-get-video-captions-result",
        "dataforseo.x.serp-ai-summary",
        "dataforseo.x.serp-screenshot",
        "dataforseo.x.on-page-content-parsing",
        "dataforseo.x.on-page-duplicate-content",
        "dataforseo.x.on-page-duplicate-tags",
        "dataforseo.x.on-page-keyword-density",
        "dataforseo.x.on-page-links",
        "dataforseo.x.on-page-non-indexable",
        "dataforseo.x.on-page-pages",
        "dataforseo.x.on-page-pages-by-resource",
        "dataforseo.x.on-page-raw-html",
        "dataforseo.x.on-page-redirect-chains",
        "dataforseo.x.on-page-resources",
        "dataforseo.x.on-page-uncrawlable-resources",
        "dataforseo.x.on-page-waterfall",
        "dataforseo.x.on-page-summary-id",
    }
    for endpoint_id in ids:
        endpoint = catalog.by_id[endpoint_id]
        assert endpoint["platform_blocked"]
        assert not catalog.platform_eligible(endpoint)


def _valid_table():
    return {
        "type": "per_success",
        "table": [
            {"when": {"body.model": "Hailuo", "body.duration": 6}, "value": 0.3},
            {"when": {"body.model": "H3"}, "value": 0.13, "times": "body.duration"},
        ],
        "fallback": {"value": 2.0, "note": "most expensive supported combination"},
        "currency": "USD",
        "settle": "table",
        "source": "docs",
        "source_url": "https://example.com/pricing",
        "checked": "2026-09-01",
        "confidence": "documented",
    }


def _valid_input():
    return {"body": {
        "model": {"type": "string", "required": True},
        "duration": {"type": "integer", "required": False, "default": 6, "max": 10},
    }}


def _table_errors(cost, input_schema=None):
    errors: list[str] = []
    validator.check_cost(cost, "demo.yaml:submit", errors, [], input_schema or _valid_input())
    return errors


def test_cost_table_accepts_subset_rows_times_bounds_and_usage_settlement():
    assert _table_errors(_valid_table()) == []
    usage = _valid_table() | {
        "settle": "usage", "usage": {"path": "usage.cost", "unit": "usd"}}
    assert _table_errors(usage) == []


@pytest.mark.parametrize(("mutate", "message"), [
    (lambda c: c.update(table=[]), "cost.table must be a non-empty list"),
    (lambda c: c.update(table=["row"]), "table row must be a mapping"),
    (lambda c: c["table"][0].update(when={}), "when must be a non-empty mapping"),
    (lambda c: c["table"][0].update(when={"body.unknown": "x"}), "is not declared in input"),
    (lambda c: c["table"][0].update(value=-1), "value must be a finite non-negative number"),
    (lambda c: c["table"][1].update(times="body.frames"), "times field 'body.frames' is not declared"),
    (lambda c: c["table"][1].update(times=""), "times must name an input field"),
    (lambda c: c.pop("fallback"), "requires a fallback mapping"),
    (lambda c: c["fallback"].update(value=-1), "fallback.value must be a finite non-negative number"),
    (lambda c: c["fallback"].update(note=""), "fallback.note must explain"),
    (lambda c: c["fallback"].update(value=1.0), "must be at least every table row"),
    (lambda c: c.update(settle="later"), "settle must be 'table' or 'usage'"),
    (lambda c: c.update(settle="usage"), "requires usage.path and usage.unit"),
    (lambda c: c.update(usage={"path": "usage.cost", "unit": "usd"}),
     "usage is only valid with settle: usage"),
    (lambda c: c.update(currency="points"), "cost.table currency must be one of"),
])
def test_cost_table_rejects_each_invalid_contract_shape(mutate, message):
    cost = _valid_table()
    mutate(cost)
    errors = _table_errors(cost)
    assert any(message in error for error in errors), errors


def test_cost_table_when_fields_need_required_or_default_and_times_needs_max():
    optional = _valid_input()
    optional["body"]["duration"].pop("default")
    errors = _table_errors(_valid_table(), optional)
    assert any("must be required or declare a default" in error for error in errors)

    no_max = _valid_input()
    no_max["body"]["duration"].pop("max")
    errors = _table_errors(_valid_table(), no_max)
    assert any("must declare a positive input max" in error for error in errors)


def test_cost_table_rejects_shadowed_rows_and_ambiguous_or_non_finite_values():
    cost = _valid_table()
    cost["table"] = [
        {"when": {"body.model": "Hailuo"}, "value": 0.3},
        {"when": {"body.model": "Hailuo", "body.duration": 6}, "value": 0.4},
    ]
    cost["fallback"]["value"] = float("inf")
    cost["value"] = 1
    cost["table"][0]["unexpected"] = True
    errors = _table_errors(cost)
    assert any("unknown table row keys" in error for error in errors)
    assert any("shadowed by an earlier subset row" in error for error in errors)
    assert any("finite non-negative" in error for error in errors)
    assert any("cost.value and cost.table are mutually exclusive" in error for error in errors)


def test_cost_table_checks_enum_bounds_numeric_times_and_usage_shape():
    input_schema = _valid_input()
    input_schema["body"]["model"]["enum"] = ["Hailuo", "H3"]
    input_schema["body"]["duration"]["min"] = 2
    input_schema["body"]["label"] = {
        "type": "string", "required": False, "default": "short", "max": 10,
    }
    cost = _valid_table()
    cost["table"][0]["when"]["body.model"] = "Unknown"
    cost["table"][0]["when"]["body.duration"] = 20
    cost["table"][1]["times"] = "body.label"
    errors = _table_errors(cost, input_schema)
    assert any("not in input enum" in error for error in errors)
    assert any("above input max" in error for error in errors)
    assert any("must be numeric" in error for error in errors)

    usage = _valid_table() | {
        "settle": "usage", "usage": {"path": "usage..cost", "unit": "credits", "extra": True},
    }
    assert any("requires usage.path and usage.unit" in error for error in _table_errors(usage))


def test_validator_checks_the_endpoint_descriptor_that_replaces_the_provider_default(tmp_path, monkeypatch, capsys):
    (tmp_path / "capabilities.yaml").write_text(
        "platforms: {video-gen: Video}\n"
        "capabilities: {video-gen.from_text: Generate}\n")
    (tmp_path / "fx.yaml").write_text("credit_rates_usd: {}\n")
    (tmp_path / "tikhub.yaml").write_text(
        "provider: tikhub\n"
        "source: {docs: https://example.com/docs}\n"
        "async:\n"
        "  id_from: task_id\n"
        "  poll: {url_from: urls.get, url_hosts: [api.example.com]}\n"
        "  status: {path: status, success: [done], failure: [failed]}\n"
        "  result: {path: output.url}\n"
        "  interval: 10\n"
        "endpoints:\n"
        "  - id: tikhub.video-gen.from-text\n"
        "    capability: video-gen.from_text\n"
        "    platform: video-gen\n"
        "    method: POST\n"
        "    path: /generate\n"
        "    summary: Generate a video\n"
        "    input:\n"
        "      body:\n"
        "        model: {type: string, required: true}\n"
        "        duration: {type: integer, required: false, default: 6, max: 10}\n"
        "    async:\n"
        "      id_from: task_id\n"
        "      poll: {url_from: urls.get, url_hosts: [api.example.com]}\n"
        "      status: {path: status, success: [succeeded], failure: [failed]}\n"
        "      result: {path: output.url}\n"
        "      interval: 10\n"
        "    cost:\n"
        "      type: per_success\n"
        "      table: [{when: {body.model: H3}, value: 0.13, times: body.duration}]\n"
        "      fallback: {value: 1.3, note: Maximum duration}\n"
        "      currency: USD\n"
        "      source: docs\n"
        "      source_url: https://example.com/pricing\n"
        "      checked: 2026-09-01\n"
        "      confidence: documented\n")
    monkeypatch.setattr(validator, "CATALOG", tmp_path)

    assert validator.main(["tikhub"]) == 0
    assert "0 error(s)" in capsys.readouterr().out


def test_async_param_location_must_agree_with_the_target_path(tmp_path, monkeypatch, capsys):
    """The worker substitutes by declared location: a pathParams id needs exactly one placeholder."""
    (tmp_path / "capabilities.yaml").write_text(
        "platforms: {video-gen: Video}\ncapabilities: {video-gen.from_text: Generate, video-gen.task.status: Poll}\n")
    (tmp_path / "fx.yaml").write_text("credit_rates_usd: {}\n")
    (tmp_path / "tikhub.yaml").write_text(
        "provider: tikhub\n"
        "source: {docs: https://example.com/docs}\n"
        "endpoints:\n"
        "  - id: tikhub.video-gen.from-text\n"
        "    capability: video-gen.from_text\n    platform: video-gen\n"
        "    method: POST\n    path: /generate\n    summary: Generate a video\n"
        "    input: {body: {prompt: {type: string, required: true}}}\n"
        "    async:\n"
        "      id_from: id\n"
        "      poll: {endpoint: tikhub.video-gen.task.status, param: {in: pathParams, name: id}}\n"
        "      status: {path: status, success: [done], failure: [failed]}\n"
        "      result: {path: url}\n"
        "      interval: 10\n"
        "    cost: {type: per_success, table: [{when: {body.prompt: a}, value: 0.1}],\n"
        "           fallback: {value: 0.1, note: flat}, currency: USD, settle: usage,\n"
        "           usage: {path: usage.cost, unit: usd}, source: docs,\n"
        "           source_url: https://example.com/pricing, checked: 2026-09-01, confidence: documented}\n"
        "  - id: tikhub.video-gen.task.status\n"
        "    kind: utility\n    capability: video-gen.task.status\n    platform: video-gen\n"
        "    method: GET\n    path: /tasks\n    summary: Poll\n"
        "    input: {pathParams: {id: {type: string, required: true}}}\n"
        "    cost: {type: free, value: 0, currency: USD, unit: call}\n")
    monkeypatch.setattr(validator, "CATALOG", tmp_path)
    assert validator.main(["tikhub"]) != 0
    out = capsys.readouterr().out
    assert "needs exactly one {id} in the target path" in out


def test_usage_settlement_requires_an_async_descriptor_and_finite_interval():
    cost = _valid_table()
    cost.update(settle="usage", usage={"path": "usage.cost", "unit": "usd"})
    errors: list[str] = []
    validator.check_cost_table(cost, _valid_input(), "x", errors)
    assert errors == []  # the block itself is fine; the pairing is checked at the endpoint level
    descriptor = _valid_async()
    descriptor["interval"] = float("nan")
    errors = []
    validator.check_async_descriptor(descriptor, "x", "demo", {}, {"type": "per_success"}, errors)
    assert any("finite" in e for e in errors)


def test_async_descriptor_rejects_a_retired_or_broken_poll_target():
    errors: list[str] = []
    index = {"demo.video-gen.status": {
        "provider": "demo", "kind": "utility", "method": "GET", "path": "/tasks/{task_id}",
        "status": "retired",
        "input": {"pathParams": {"task_id": {"type": "string", "required": True}}}}}
    validator.check_async_descriptor(_valid_async(), "demo.yaml:submit", "demo", index,
                                     {"type": "per_success"}, errors)
    assert any("marked 'retired'" in e for e in errors)


@pytest.mark.parametrize('rule', [
    {'path': 'billing.charge', 'unit': 'usd'},
    {'path': 'billing.charge', 'unit': 'credits'},
    {'path': '', 'unit': 'usd'},
    {'path': 'billing.charge', 'unit': 'usd', 'scale': 2},
])
def test_reported_charge_requires_supported_units_and_path(rule):
    cost = dict(catalog_store.load().by_id['trykitt.people.email.find']['cost'])
    cost['reported_charge'] = rule
    errors = []
    validator.check_cost(cost, 'test', errors, [])
    assert bool(errors) is (rule != {'path': 'billing.charge', 'unit': 'usd'})


@pytest.mark.parametrize('rule,valid', [
    ({'body.realtime': True}, True),
    ({'body.realtime': 1}, False),
    ({'body.realtime': False}, False),
    ({'body.missing': True}, False),
    ({'queryParams.realtime': True}, False),
    ({}, False),
])
def test_platform_request_requires_declared_fixed_body_value(rule, valid):
    errors = []
    validator.check_platform_request(rule, {'body': {
        'realtime': {'type': 'boolean', 'enum': [True]},
    }}, 'test', errors)
    assert (not errors) is valid


# ---- ContactOut ----

def _contactout_cost(eid):
    return catalog_store.load().cost_view(
        catalog_store.load().by_id["contactout." + eid]["cost"], "contactout"
    )


def test_contactout_catalog_prices_validate_and_surface_is_bounded():
    from scripts.catalog_validate import check_cost

    cat = catalog_store.load()
    entries = [e for e in cat.endpoints if e.get("provider") == "contactout"]
    assert len(entries) == 20
    assert not any("batch" in e["path"] for e in entries)
    errors = []
    for e in entries:
        check_cost(e["cost"], e["id"], errors, [], e["input"])
    assert errors == []
    broken = _contactout_cost("people.contact.work") | {
        "contactout": {"job": "contact", "rates_micro": {"phone": -1}}
    }
    check_cost(broken, "test", errors, [])
    assert errors


def test_contactout_free_checkers_are_not_advertised_as_contact_finders():
    cat = catalog_store.load()
    for eid in ("people.work_email.available", "people.personal_email.available", "people.phone.available"):
        assert cat.by_id["contactout." + eid]["capability"].endswith(".availability")
    assert cat.by_id["contactout.people.count"]["capability"] == "people.count"


def test_contactout_catalog_distribution_preserves_ids_and_global_discovery():
    from collections import Counter
    cat = catalog_store.load()
    entries = [e for e in cat.endpoints if e.get("provider") == "contactout"]
    assert Counter(e["platform"] for e in entries) == {
        "linkedin": 2, "people": 16, "companies": 2}
    for e in entries:
        assert e["capability"].split(".")[0] == e["platform"]
    assert cat.by_id["contactout.people.contact.work"]["platform"] == "people"
    assert cat.by_id["contactout.people.contact.personal"]["capability"] == "people.email.personal.find"
    results, _ = catalog_store.search("contactout people work email", cat, limit=100)
    assert any(e["id"] == "contactout.people.contact.work" for e, _ in results)


def test_contactout_person_routes_cannot_recapture_pii():
    from pathlib import Path
    import yaml
    path = Path("src/treg/catalog/contactout.yaml")
    endpoints = yaml.safe_load(path.read_text())["endpoints"]
    safe = {"contactout.people.count", "contactout.people.email.verify",
            "contactout.companies.search", "contactout.companies.enrich"}
    structural = {"contactout.people.contact.work", "contactout.people.contact.phone"}
    for ep in endpoints:
        if ep["id"] in safe:
            continue
        assert ep["untestable"]
        assert not any(key in ep for key in ("test_request", "verified"))
        example = path.parent / "examples" / (ep["id"] + ".json")
        if ep["id"] in structural:
            assert ep["example_response"] == "examples/" + example.name
            payload = example.read_text()
            assert "example.invalid" in payload or "+10000000000" in payload
        else:
            assert "example_response" not in ep
            assert not example.exists()
    work = next(ep for ep in endpoints if ep["id"] == "contactout.people.enrich.work_email")
    assert work["cost"]["value"] == 0.17


@pytest.mark.parametrize('display,valid', [
    ({'unit':'records','grouped':True,'round_up':True}, True),
    ({'unit':'item','variable':True}, True),
    ({'unit':'records','round_up':True}, False),
    ({'unit':'item','variable':'yes'}, False),
    ({'unit':''}, False),
    ({'unit':'item','provider':'sumble'}, False),
])
def test_generic_price_display_metadata(display, valid):
    cost = {'type':'per_result','value':1,'currency':'USD','per':25,'unit':'record',
            'source':'docs','source_url':'https://example.com','checked':'2026-09-09',
            'confidence':'documented','display':display}
    errors = []
    validator.check_cost(cost, 'test', errors, [])
    assert (not errors) == valid


@pytest.mark.parametrize('patch,valid', [
    ({}, True), ({'strict_query': 'yes'}, False), ({'method': 'POST'}, False),
    ({'path': '/{id}'}, False),
    ({'input': {'queryParams': {'mode': {'enum': [True]}}}}, False),
])
def test_strict_query_contract_validation(patch, valid):
    ep = {'strict_query': True, 'method': 'GET', 'path': '/lookup',
          'input': {'queryParams': {'mode': {'type': 'string', 'enum': ['true']}}}}
    errors = []
    validator.check_strict_query(ep | patch, 'example', errors)
    assert bool(errors) is not valid


@pytest.mark.parametrize('patch,valid', [
    ({}, True),
    ({'strict_body': 'yes'}, False),
    ({'method': 'GET'}, False),
    ({'input': {'body': {'items': {'type': 'array[object]', 'min': 2, 'max': 1}}}}, False),
])
def test_strict_body_contract_validation(patch, valid):
    ep = {'strict_body': True, 'method': 'POST', 'path': '/lookup',
          'input': {'body': {'items': {'type': 'array[object]', 'min': 1, 'max': 1}}}}
    errors = []
    validator.check_strict_body(ep | patch, 'example', errors)
    assert bool(errors) is not valid


@pytest.mark.parametrize('patch,valid', [
    ({}, True),
    ({'platform_auth': 'provider'}, False),
    ({'method': 'POST'}, False),
    ({'cost': {'type': 'per_success', 'value': 0.02}}, False),
    ({'verified': ''}, False),
    ({'scope': 'own_account'}, False),
    ({'authorization_method': 'oauth'}, False),
    ({'async': {'poll': {}}}, False),
])
def test_anonymous_platform_auth_is_a_verified_free_read_only_contract(patch, valid):
    ep = {
        'id': 'example.public.values',
        'platform_auth': 'anonymous',
        'method': 'GET',
        'scope': 'any_account',
        'cost': {'type': 'free', 'value': 0, 'currency': 'USD', 'unit': 'call'},
        'verified': '2026-09-15',
    }
    errors = []
    validator.check_platform_auth(ep | patch, 'example', errors)
    assert (not errors) is valid


def test_missing_platform_auth_normalizes_as_absent():
    normalized = catalog_store._normalize({
        'id': 'example.public.values',
        'method': 'GET',
        'path': '/values',
    }, 'example', Path('.'))
    assert normalized['platform_auth'] is None


def test_dropleads_catalog_surface_is_bounded_and_excludes_internal_routes():
    catalog = catalog_store.load()
    rows = [ep for ep in catalog.endpoints if ep["provider"] == "dropleads"]
    assert len(rows) == 10
    assert all(catalog.platform_eligible(ep) for ep in rows)
    assert not any(
        "credits/balance" in ep["path"] or "export/cost" in ep["path"]
        for ep in rows
    )
    assert {ep.get("host") for ep in rows if ep.get("host")} == {"api.dropleads.io"}
    assert catalog.by_id["dropleads.companies.search.count"]["capability"] == \
        "companies.search.count"
    assert catalog.by_id["dropleads.people.enrich"]["test_request"]["body"] == {
        "name": "Jane Doe",
        "organization_name": "Example",
        "domain": "example.com",
    }
    assert catalog.by_id["dropleads.people.enrich.verified"]["test_request"]["body"] == {
        "name": "Jane Doe",
        "organization_name": "Example",
        "domain": "example.com",
        "email_verification_type": "valid_and_catchall",
    }


def test_prospeo_catalog_surface_excludes_account_info_and_prices_mobile_at_the_documented_maximum():
    catalog = catalog_store.load()
    rows = [ep for ep in catalog.endpoints if ep["provider"] == "prospeo"]
    assert len(rows) == 7
    assert not any(ep["path"] == "/account-information" for ep in rows)
    assert {ep["path"] for ep in rows} == {
        "/enrich-person", "/enrich-company", "/search-person", "/search-company",
        "/search-suggestions",
    }
    phone = catalog.by_id["prospeo.people.phone.find"]
    assert not phone.get("platform_blocked")
    assert phone["cost"]["value"] == 10
    assert all(catalog.platform_eligible(ep) for ep in rows)


def test_aiark_catalog_covers_the_selected_documented_surface():
    catalog = catalog_store.load()
    endpoints = {eid: ep for eid, ep in catalog.by_id.items() if eid.startswith("aiark.")}
    assert set(endpoints) == {
        "aiark.people.search", "aiark.people.preview", "aiark.companies.search",
        "aiark.people.email.find", "aiark.people.phone.find", "aiark.people.enrich",
        "aiark.people.personality.analyze", "aiark.lists.upsert",
    }
    assert not any(ep["path"] in {
        "/v1/payments/credits", "/v1/people/export/single",
        "/v1/people/mobile-phone-finder",
    } for ep in endpoints.values())
    assert all(
        ep.get("platform_blocked") for eid, ep in endpoints.items()
        if eid == "aiark.lists.upsert"
    )
    assert endpoints["aiark.people.search"]["input"]["body"]["size"]["enum"] == [1]
    assert endpoints["aiark.people.search"]["platform_request"] == {"body.size": 1}
    assert catalog.cost_view(
        endpoints["aiark.people.email.find"]["cost"], "aiark"
    )["usd"] == 0.005267
    assert catalog.cost_view(
        endpoints["aiark.people.phone.find"]["cost"], "aiark"
    )["usd"] == 0.026335


def test_limadata_catalog_covers_basic_v2_and_keeps_unsafe_calls_byok_only():
    catalog = catalog_store.load()
    rows = [ep for ep in catalog.endpoints if ep["provider"] == "limadata"]
    assert {(ep["method"], ep["path"]) for ep in rows} == {
        ("POST", "/api/v1/enrich/person"),
        ("POST", "/api/v1/enrich/company"),
        ("POST", "/api/v1/database/autocomplete"),
        ("POST", "/api/v1/database/count_companies"),
        ("POST", "/api/v1/database/count_people"),
        ("POST", "/api/v1/database/search_companies"),
        ("POST", "/api/v1/database/search_people"),
        ("POST", "/api/v1/database/search_people_employees"),
        ("POST", "/api/v1/find/ad_audience"),
        ("POST", "/api/v1/find/audience_identifiers"),
        ("POST", "/api/v1/find/email_personal"),
        ("POST", "/api/v1/find/email_verify"),
        ("POST", "/api/v1/find/email_work"),
        ("POST", "/api/v1/find/email_work_linkedin"),
        ("POST", "/api/v1/find/pages_company"),
        ("POST", "/api/v1/find/phone"),
        ("POST", "/api/v1/find/profiles_person"),
        ("POST", "/api/v1/find/reverse_email_lookup"),
        ("POST", "/api/v1/research/extract"),
        ("POST", "/api/v1/research/search"),
        ("POST", "/api/v1/search/web"),
    }
    platform = {ep["id"] for ep in rows if catalog.platform_eligible(ep)}
    assert len(rows) == 21 and len(platform) == 14
    assert {
        "limadata.people.enrich",
        "limadata.people.count",
        "limadata.companies.search",
        "limadata.people.search",
        "limadata.people.employees.search",
        "limadata.people.identity.resolve",
        "limadata.web.extract",
    }.isdisjoint(platform)
    company_page = catalog.by_id["limadata.companies.linkedin.find"]
    assert company_page["platform"] == "linkedin"
    assert company_page["capability"] == "linkedin.company.from_domain"


def test_zerobounce_catalog_exposes_verified_single_record_tools_only():
    catalog = catalog_store.load()
    rows = [ep for ep in catalog.endpoints if ep["provider"] == "zerobounce"]
    assert [ep["id"] for ep in rows] == [
        "zerobounce.people.email.verify",
        "zerobounce.people.email.find",
        "zerobounce.companies.email_pattern",
    ]
    validation = catalog.by_id["zerobounce.people.email.verify"]
    assert validation["method"] == "GET"
    assert validation["path"] == "/v2/validate"
    assert catalog.cost_view(validation["cost"], "zerobounce")["usd"] == 0.0138
    finder = catalog.by_id["zerobounce.people.email.find"]
    pattern = catalog.by_id["zerobounce.companies.email_pattern"]
    assert finder["path"] == pattern["path"] == "/v2/guessformat"
    assert catalog.cost_view(finder["cost"], "zerobounce")["usd"] == 0.276
    assert catalog.cost_view(pattern["cost"], "zerobounce")["usd"] == 0.276
    assert all(catalog.platform_eligible(ep) for ep in rows)


def test_bounceban_catalog_has_one_platform_tool_and_no_bulk_lifecycle():
    catalog = catalog_store.load()
    rows = [ep for ep in catalog.endpoints if ep["provider"] == "bounceban"]
    assert len(rows) == 4
    assert {ep["path"] for ep in rows} == {
        "/v1/verify/single",
        "/v1/verify/single/status",
        "/v1/account",
    }
    assert not any(ep["path"] in ("/v1/verify/bulk/file", "/v1/verify/bulk/destroy", "/v1/check")
                   for ep in rows)
    eligible = [ep["id"] for ep in rows if catalog.platform_eligible(ep)]
    assert eligible == ["bounceban.people.email.verify"]
    direct = catalog.by_id["bounceban.people.email.verify"]
    assert direct["cost"]["value"] == 1
    assert catalog.cost_view(direct["cost"], "bounceban")["usd"] == 0.004
    assert "disable_catchall_verify" not in direct["input"]["queryParams"]
    waterfall = catalog.by_id["bounceban.people.email.verify.waterfall"]
    assert waterfall["host"] == "api-waterfall.bounceban.com"
    assert waterfall["platform_blocked"]


def test_getleadsio_scalar_routes_are_available_to_byok_and_platform_callers():
    catalog = catalog_store.load()
    rows = [ep for ep in catalog.endpoints if ep["provider"] == "getleadsio"]
    assert len(rows) == 11
    assert not any(ep["path"] in {
        "/api/v1/usage/fair-use", "/api/v1/contacts/health"
    } for ep in rows)
    assert all(catalog.platform_eligible(ep) for ep in rows)
    assert not any(ep["id"].endswith(".trial") for ep in rows)
    assert not any(ep.get("platform_request") or ep.get("platform_blocked") for ep in rows)
    scalar = [ep for ep in rows if ep["id"].startswith("getleadsio.people.enrich.from_")]
    assert len(scalar) == 3
    assert all(ep["strict_body"] and ep["input"]["body"]["items"]["max"] == 1
               for ep in scalar)
