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


def test_cost_minimum_units_is_an_integer_floor_on_a_per_result_price():
    base = {
        "type": "per_result", "value": 2, "currency": "credit", "per": 1,
        "unit": "record", "source": "docs", "source_url": "https://example.com/pricing",
        "checked": "2026-09-09", "confidence": "verified",
    }
    for floor in (0, 1, 5):
        errors: list[str] = []
        validator.check_cost(base | {"minimum_units": floor}, "catalog:test", errors, [])
        assert errors == [], floor

    for bad in (-1, True, 1.5, "1"):
        broken: list[str] = []
        validator.check_cost(base | {"minimum_units": bad}, "catalog:test", broken, [])
        assert any("cost.minimum_units must be a non-negative integer" in error for error in broken), bad

    wrong_type: list[str] = []
    validator.check_cost(base | {"type": "per_call", "unit": "call", "minimum_units": 1},
                         "catalog:test", wrong_type, [])
    assert any("only valid with type: per_result" in error for error in wrong_type)


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


def test_dataforseo_task_posts_without_a_servable_consumer_are_platform_blocked():
    """A task_post only ENQUEUES work; the answer comes back through task_get or an id-keyed reader
    of the same family. Ingest drops task_get (scripts/catalog_ingest.py) and the id-keyed readers
    are BYOK-only, so a shared-key task_post charged the caller for a result treg could never fetch
    (213 platform calls across 21 orgs before 2026-09-09). The rule, not the list: a task_post may
    be offered on treg's key only while at least one consumer of its family is."""
    catalog = catalog_store.load()
    dataforseo = [ep for ep in catalog.endpoints if ep["provider"] == "dataforseo"]
    task_posts = [ep for ep in dataforseo if ep["path"].endswith("/task_post")]
    assert len(task_posts) >= 20, "the whole legacy async surface, not a sample"

    def consumes_a_task(ep: dict) -> bool:
        inputs = ep.get("input") or {}
        return (any(seg in ep["path"] for seg in ("/task_get", "/tasks_ready", "{id}"))
                or "id" in (inputs.get("body") or {}))

    for post in task_posts:
        family = post["path"][: -len("/task_post")] + "/"
        consumers = [ep for ep in dataforseo
                     if ep is not post and ep["path"].startswith(family) and consumes_a_task(ep)]
        if any(catalog.platform_eligible(ep) for ep in consumers):
            continue
        reason = post["platform_blocked"]
        assert reason and not catalog.platform_eligible(post), post["id"]
        # the reason must say what happens and what to do instead, not just "no"
        assert "task_get" in reason and "own DataForSEO key" in reason, post["id"]
        for alternative in ("dataforseo.web.page.audit", "brightdata.x.trustpilot-reviews"):
            if alternative in reason:
                assert catalog.platform_eligible(catalog.by_id[alternative]), alternative
    # the two families with a one-shot sibling on treg's key point at it by id
    assert "dataforseo.web.page.audit" in catalog.by_id["dataforseo.x.on-page-task-post"]["platform_blocked"]
    assert "brightdata.x.trustpilot-reviews" in \
        catalog.by_id["dataforseo.x.business-data-trustpilot-reviews-task-post"]["platform_blocked"]


def test_on_page_consumer_notes_point_at_a_real_one_shot_route():
    """The task consumers' notes named `dataforseo.x.on-page-instant-pages`, an id that never
    existed; an agent following it got a 404 instead of the one-shot audit."""
    catalog = catalog_store.load()
    summary = catalog.by_id["dataforseo.x.on-page-summary-id"]
    assert summary["input"]["pathParams"]["id"]["required"] is True, "the {id} in the path is declared"
    for ep in catalog.endpoints:
        if ep["provider"] != "dataforseo":
            continue
        for text in (str(ep.get("untestable") or ""), str(ep.get("platform_blocked") or "")):
            for word in text.replace("(", " ").replace(")", " ").replace(",", " ").split():
                if word.startswith("dataforseo.") or word.startswith("brightdata."):
                    assert word in catalog.by_id, f"{ep['id']} names unknown id {word!r}"


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
        "linkedin": 8, "people": 10, "companies": 2}
    for e in entries:
        assert e["capability"].split(".")[0] == e["platform"]
    assert cat.by_id["contactout.people.contact.work"]["platform"] == "linkedin"
    results, _ = catalog_store.search("contactout linkedin work email", cat, limit=100)
    assert any(e["id"] == "contactout.people.contact.work" for e, _ in results)


def test_contactout_person_routes_cannot_recapture_pii():
    from pathlib import Path
    import yaml
    path = Path("src/treg/catalog/contactout.yaml")
    endpoints = yaml.safe_load(path.read_text())["endpoints"]
    safe = {"contactout.people.count", "contactout.people.email.verify",
            "contactout.companies.search", "contactout.companies.enrich"}
    for ep in endpoints:
        if ep["id"] in safe:
            continue
        assert ep["untestable"]
        assert not any(key in ep for key in ("test_request", "verified", "example_response"))
        assert not (path.parent / "examples" / (ep["id"] + ".json")).exists()
    work = next(ep for ep in endpoints if ep["id"] == "contactout.people.enrich.work_email")
    assert work["cost"]["value"] == 0.17


@pytest.mark.parametrize('page,cost_type,ok', [
    (100, 'per_result', True), (10, 'quota_rows', True),
    (0, 'per_result', False), (-5, 'per_result', False), (True, 'per_result', False),
    ('100', 'per_result', False), (1.5, 'per_result', False),
    (100, 'per_call', False), (100, 'per_success', False),
])
def test_page_default_is_a_positive_row_count_on_a_row_priced_entry(page, cost_type, ok):
    """`cost.page_default` is what the reserve assumes when the caller names no limit (SE Ranking's
    keyword ideas answer 100 rows by default, not treg's 20); it has no meaning on a flat price."""
    cost = {
        'type': cost_type, 'value': 10, 'currency': 'credit', 'per': 1, 'unit': 'row',
        'source': 'docs', 'source_url': 'https://example.com/pricing',
        'checked': '2026-09-09', 'confidence': 'documented', 'page_default': page,
    }
    errors = []
    validator.check_cost(cost, 'test', errors, [])
    assert (not errors) is ok, errors
