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
         "produces": [{"kind": "result", "path": "data.result_id"}]},
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
