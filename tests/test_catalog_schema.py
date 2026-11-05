"""The strict catalog models both spellings share with the loader and the validator."""

from __future__ import annotations

import json

import pytest
import yaml
from pydantic import TypeAdapter, ValidationError

from treg.domain import plugins
from treg.domain.catalog import store
from treg.domain.catalog.routing import paths as expressions
from treg.domain.catalog.schema import CatalogSchemaError, Manifest, V2Row, is_v2, violations
from treg.domain.catalog.schema.base import KINDS
from treg.domain.catalog.schema.common import Input
from treg.domain.catalog.schema.export import json_schemas
from treg.domain.catalog.schema.files import merge
from treg.domain.catalog.schema.paths import ResponsePredicate, parse_path


@pytest.mark.parametrize("text,many", [
    ("response", False), ("response.tasks.0.status_code", False), ("response.tasks.*.id", True),
    ("response.usageMetadata.candidatesTokensDetails[modality=IMAGE].tokenCount", True),
    ("body.params.size", False), ("query.limit", False), ("header.x-api-version", False),
])
def test_a_path_names_a_location_and_its_steps(text, many):
    assert parse_path(text).many is many


@pytest.mark.parametrize("text", ["tasks.0.id", "response..a", "response.", "response.a[b]",
                                  "response.a[=b]", "queryParams.limit", ""])
def test_a_malformed_path_is_refused(text):
    with pytest.raises(ValueError):
        parse_path(text)


@pytest.mark.parametrize("value,message", [
    ({"path": "body.a", "op": "absent"}, "a body path is not allowed here"),
    ({"path": "response.a", "op": "in", "value": 1}, "op in takes a list"),
    ({"path": "response.a", "op": "absent", "value": 1}, "op absent takes no value"),
    ({"path": "response.a", "op": "contains", "value": 3}, "op contains takes a string"),
    ({"path": "response.a", "op": "equals"}, "op equals needs a value"),
    ([], "at least 1 item"),
    ({"any": [{"path": "response.a", "op": "empty", "x": 1}]}, "Extra inputs"),
])
def test_a_predicate_parses_strictly_and_only_where_its_location_is_allowed(value, message):
    with pytest.raises(ValidationError, match=message):
        TypeAdapter(ResponsePredicate).validate_python(value)
    TypeAdapter(ResponsePredicate).validate_python(
        [{"path": "response.a", "op": "absent"}, {"any": [{"path": "response.b", "op": "in", "value": [1]}]}])


@pytest.mark.parametrize("row,message", [
    ('{queryParams: {q: {type: string, required: true, note: "One, two."}}, note: ok}', None),
    ('{body: {type: object, required: [q], properties: {q: {type: string, items: {type: string}}}}}', None),
    ('{body: {type: object, required: true, note: the whole body}}', None),
    ('{query: {q: {type: string, required: true}}}', r"(?s)\nquery\n.*Extra inputs"),
    ('{body: {data: {type: string, note: Email, or a domain.}}}', r"(?s)`or a domain.`\n.*Extra inputs"),
    ('{body: {type: object, properties: {a: {type: object, properties: {b: {tpye: string}}}}}}',
     r"(?s)properties.b.tpye\n.*Extra inputs"),
    ('{bodyType: xml}', "bodyType"),
])
def test_input_keys_and_parameter_spec_keys_are_the_known_vocabulary(row, message):
    if message is None:
        Input.model_validate(yaml.safe_load(row))
    else:
        with pytest.raises(ValidationError, match=message):
            Input.model_validate(yaml.safe_load(row))


def _v2(**keys) -> dict:
    return {"id": "demo.web.page", "method": "GET", "path": "/a", **keys}


PROVENANCE = {"source": "docs", "source_url": "https://example.com/pricing", "checked": "2026-10-01",
              "confidence": "documented"}


@pytest.mark.parametrize("keys,message", [
    ({"price": {"value": 0.5}, "provenance": PROVENANCE}, "money is a quoted decimal string"),
    ({"price": {"value": "1", "table": [{"when": [{"path": "body.a", "op": "equals", "value": 1}],
                                         "value": "1"}]}, "provenance": PROVENANCE}, "exactly one of value"),
    ({"price": {"table": [{"when": [{"path": "body.a", "op": "equals", "value": 1}], "value": "1"}]},
      "provenance": PROVENANCE}, "a table needs a fallback"),
    ({"price": {"table": [{"when": [{"path": "header.a", "op": "equals", "value": 1}], "value": "1"}],
                "fallback": {"value": "1", "note": "upper"}}, "provenance": PROVENANCE}, "header path"),
    ({"price": {"value": "1"}}, "a priced row needs provenance"),
    ({"price": {"value": None, "note": "no public price"},
      "provenance": PROVENANCE | {"confidence": "documented"}}, "confidence is unknown exactly"),
    ({"price": {"value": "1"}, "provenance": PROVENANCE | {"source": "docs", "source_url": None}},
     "source_url is required"),
    ({"reserve": {"rows": {"default": {"value": 20, "source": "docs"}, "max": 100}}, "price": {"value": "0"}},
     "frozen legacy hold"),
    ({"reserve": {"plugin": "demo.hold"}, "price": {"value": "0"}}, "needs max"),
    ({"reserve": {"entities": {}}, "price": {"value": "0"}}, "from"),
    ({"settle": {"reported": {"path": "response.cost", "header": "x-cost", "unit": "usd"}},
      "price": {"value": "0"}}, "exactly one of path, header"),
    ({"settle": {"count": {"path": "body.items"}}, "price": {"value": "0"}}, "body path"),
    ({"price": {"rates": {"a": "1"}}, "provenance": PROVENANCE, "settle": {"count": {"path": "response.a"}}},
     "price.rates stands only beside plugins"),
    ({"price": {"rates": {"a": "1"}}, "provenance": PROVENANCE, "settle": {"plugin": "demo.used"}},
     "price.rates stands only beside plugins"),
    ({"price": {"rates": {"a": "1"}}, "provenance": PROVENANCE, "reserve": {"plugin": "demo.hold", "max": "1"},
      "bill": {"miss": 2}}, "bill.miss in units needs a unit price"),
    ({"miss": {"status": [200]}}, "greater than or equal to 400"),
    ({"miss": {"means": "nothing"}}, "needs status, when or plugin"),
    ({"error": {"when": {"path": "response.e", "op": "present"}, "plugin": "demo.err"}}, "exactly one of when"),
    ({"not_found": {"status": [200]}, "price": {"value": "0"}}, "every success ends the call"),
    ({"bill": {"miss": 0}}, "bill.miss"),
    ({"reserve": {"plugin": "Demo-Hold", "max": "1"}, "price": {"value": "0"}}, "<module>.<function>"),
    ({"cost": {"type": "free"}, "price": {"value": "0"}}, "mixes today's spelling"),
])
def test_the_v2_row_refuses_each_invalid_shape(keys, message):
    from treg.domain.catalog.schema.files import parse_row
    with pytest.raises((ValidationError, CatalogSchemaError), match=message):
        parse_row(_v2(**keys), "demo.yaml")


def test_a_v2_row_is_served_as_plain_values_with_every_absent_meaning_and_its_cost_view():
    from treg.domain.catalog.schema.v2 import served
    row = V2Row.model_validate(_v2(
        price={"value": "1", "currency": "credit", "per": 10, "round": "up", "unit": "result"},
        provenance=PROVENANCE, reserve={"rows": {"param": "body.limit", "default": {"value": 100, "source": "docs"},
                                                 "max": 10000}},
        settle={"reported": {"path": "response.credits_used", "unit": "credit"}}))
    out = served(row, {})
    assert out["price"] == {"value": "1", "currency": "credit", "per": 10, "round": "up", "unit": "result"}
    assert out["expect"] is None and out["miss"] is None and out["bill"] == {"miss": "free", "error": "free"}
    assert out["cost"] == {"type": "per_result", "value": 1, "currency": "credit", "per": 10, "unit": "result",
                           "reserve": {"rows": {"param": "body.limit", "default": {"value": 100, "source": "docs"},
                                                "max": 10000}},
                           "reported_charge": {"path": "credits_used", "unit": "credit"}, **PROVENANCE}
    assert json.loads(json.dumps(out)) == out


@pytest.mark.parametrize("keys,cost", [
    ({"reserve": {"rows": {"param": "query.limit", "max": 50}}},
     {"reserve": {"rows": {"param": "queryParams.limit", "max": 50}}}),
    ({"reserve": {"rows": {"param": "query.n", "default": {"value": 5, "source": "docs"}, "max": 50}}},
     {"reserve": {"rows": {"param": "pathParams.n", "default": {"value": 5, "source": "docs"}, "max": 50}}}),
    ({"reserve": {"entities": {"from": "body.domains"}}, "settle": {"count": {"path": "response.data.items"}}},
     {"reserve": {"entities": {}}, "settle": {"count": {"path": "data.items"}}}),
    ({"settle": {"reported": {"header": "x-cost", "unit": "usd"}}}, {}),
])
def test_the_served_cost_rebuilds_todays_hold_and_settle_forms(keys, cost):
    from treg.domain.catalog.schema.v2 import served
    row = V2Row.model_validate(_v2(price={"value": "1"}, provenance=PROVENANCE, **keys))
    out = served(row, {"pathParams": {"n": {"type": "integer"}}, "queryParams": {"limit": {"type": "integer"}}})
    assert out["cost"] == {"type": "per_result" if "reserve" in keys or "count" in keys.get("settle", {})
                           else "per_success", "value": 1, **cost, **PROVENANCE}


def test_the_merge_rule_replaces_whole_inherits_the_absent_and_opts_out_on_null():
    defaults = {"expect": {"path": "response.code", "op": "equals", "value": 0},
                "bill": {"miss": "charged", "error": "charged"}}
    row = {"id": "x", "expect": {"path": "response.ok", "op": "equals", "value": True}, "bill": None}
    merged = merge(row, defaults)
    assert merged["expect"] == {"path": "response.ok", "op": "equals", "value": True}
    assert merged["bill"] is None
    assert merge({"id": "x"}, defaults) == {"id": "x", **defaults}


def _catalog(tmp_path, files: dict[str, str]):
    (tmp_path / "capabilities.yaml").write_text("platforms: {web: Web}\ncapabilities: {}\n")
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    return store.load(directory=tmp_path)


ROW = "  - id: demo.web.{name}\n    platform: web\n    method: GET\n    path: /{name}\n"
V2_DEFAULTS = ("defaults:\n  bill: {miss: charged, error: charged}\n"
               "  provenance: {source: docs, source_url: 'https://example.com', checked: '2026-10-01', "
               "confidence: documented}\n")


def test_the_loader_reads_both_spellings_into_one_resolved_row(tmp_path):
    cat = _catalog(tmp_path, {
        "demo.yaml": "provider: demo\nendpoints:\n" + ROW.format(name="old")
        + "    cost: {type: per_call, value: 0.01, currency: USD, unit: call, source: docs, "
          "source_url: 'https://example.com', checked: '2026-10-01', confidence: documented}\n"
        + ROW.format(name="new") + "    price: {value: '0'}\n"
          "    miss: {when: {path: response.items, op: empty}}\n"
        + ROW.format(name="unpriced") + "    expect: {path: response.code, op: equals, value: 0}\n",
        "next.yaml": "provider: next\n" + V2_DEFAULTS + "endpoints:\n"
        + ROW.format(name="kept").replace("demo.", "next.") + "    price: {value: '0.01', unit: call}\n",
        "next.extended.yaml": "provider: next\nendpoints:\n"
        + ROW.format(name="ext").replace("demo.", "next.") + "    price: {value: '0.02'}\n    bill: null\n",
    })
    old, new, unpriced = (cat.by_id[f"demo.web.{n}"] for n in ("old", "new", "unpriced"))
    kept, ext = cat.by_id["next.web.kept"], cat.by_id["next.web.ext"]
    assert old["cost"]["value"] == 0.01 and not is_v2(old) and "price" not in old
    assert is_v2(new) and new["cost"] == {"type": "free", "value": 0} and new["tier"] == "core"
    assert new["price"] == {"value": "0"} and new["miss"] == {"when": {"path": "response.items", "op": "empty"}}
    assert is_v2(unpriced) and unpriced["price"] is None and unpriced["cost"] is None
    assert unpriced["expect"] == {"path": "response.code", "op": "equals", "value": 0}
    assert kept["bill"] == {"miss": "charged", "error": "charged"} and kept["provenance"]["source"] == "docs"
    assert kept["price"] == {"value": "0.01", "unit": "call"}
    assert kept["cost"] == {"type": "per_call", "value": 0.01, "unit": "call", "source": "docs",
                            "source_url": "https://example.com", "checked": "2026-10-01", "confidence": "documented"}
    assert ext["tier"] == "extended" and ext["bill"] == {"miss": "free", "error": "free"}
    assert cat.platform_eligible(kept) and json.dumps(cat.by_id["next.web.kept"])


def test_a_legacy_row_keeps_its_cost_ceiling_param_and_its_own_response_retention(tmp_path):
    cat = _catalog(tmp_path, {"demo.yaml": "provider: demo\nendpoints:\n" + ROW.format(name="old")
                              + "    response_retention: forbidden\n"
                              "    cost: {type: per_call, value: 0, currency: USD, unit: call, "
                              "ceiling_param: {in: query, name: max_usd, unit: usd}}\n"})
    old = cat.by_id["demo.web.old"]
    assert not is_v2(old) and old["response_retention"] == "forbidden"
    assert old["cost"]["ceiling_param"] == {"in": "query", "name": "max_usd", "unit": "usd"}


def test_provider_defaults_reach_both_files_and_refuse_a_legacy_row(tmp_path):
    with pytest.raises(CatalogSchemaError, match="demo.web.old: mixes today's spelling"):
        _catalog(tmp_path, {
            "demo.yaml": "provider: demo\nendpoints:\n" + ROW.format(name="old")
            + "    cost: {type: free, value: 0, currency: USD, unit: call}\n",
            "demo.extended.yaml": "provider: demo\n" + V2_DEFAULTS + "endpoints:\n"
            + ROW.format(name="ext") + "    price: {value: '0'}\n"})


@pytest.mark.parametrize("files,message", [
    ({"demo.yaml": "provider: demo\n" + V2_DEFAULTS + "endpoints:\n" + ROW.format(name="a"),
      "demo.extended.yaml": "provider: demo\n" + V2_DEFAULTS + "endpoints:\n" + ROW.format(name="b")},
     "defaults is also declared in demo.yaml"),
    ({"demo.yaml": "provider: demo\nexpect: {json_path: code, equals: 0}\n"
                   "defaults:\n  expect: {path: response.code, op: equals, value: 0}\nendpoints:\n"
                   + ROW.format(name="a")}, "expect is declared both in the header and in defaults"),
    ({"demo.yaml": "provider: demo\ncache: {mode: public}\nendpoints:\n" + ROW.format(name="a"),
      "demo.extended.yaml": "provider: demo\ndefaults:\n  cache: {mode: forbidden}\nendpoints:\n"
                            + ROW.format(name="b")},
     r"demo.yaml: cache is declared both in the header and in defaults \(demo.extended.yaml\)"),
    ({"demo.yaml": "provider: demo\ndefaults:\n  host: api.example.com\nendpoints:\n" + ROW.format(name="a")},
     "defaults may not hold host"),
    ({"demo.yaml": "provider: demo\ndefaults:\n  bill: null\nendpoints:\n" + ROW.format(name="a")},
     "defaults may not hold null"),
    ({"demo.yaml": "provider: demo\nexpect: {json_path: code, equals: 0}\nendpoints:\n"
                   + ROW.format(name="a") + "    price: {value: '0'}\n"},
     "cannot inherit the header's expect"),
    ({"other.yaml": "provider: demo\nendpoints:\n" + ROW.format(name="a")}, "!= filename stem"),
    ({"demo.yaml": "provider: demo\nendpoints:\n" + ROW.format(name="a") + "    cosst: {}\n"},
     "cosst: Extra inputs"),
])
def test_the_loader_fails_on_what_the_models_refuse(tmp_path, files, message):
    with pytest.raises(CatalogSchemaError, match=message):
        _catalog(tmp_path, files)


def test_a_listed_rejection_loads_and_an_unlisted_one_fails(tmp_path, monkeypatch):
    listed = violations.parse({"rejected": [{"at": "demo.web.a", "path": "input.body", "reason": "r"}]})
    monkeypatch.setattr(violations, "load", lambda: listed)
    files = {"demo.yaml": "provider: demo\nendpoints:\n" + ROW.format(name="a")
             + "    input: {body: {domain: stripe.com}}\n"}
    assert _catalog(tmp_path, files).by_id["demo.web.a"]["input"] == {"body": {"domain": "stripe.com"}}
    files["demo.yaml"] += ROW.format(name="b") + "    input: {body: {domain: stripe.com}}\n"
    with pytest.raises(CatalogSchemaError, match="demo.web.b: input.body"):
        _catalog(tmp_path, files)


def test_the_violation_audit_refuses_growth_and_stale_entries():
    listed = violations.parse({
        "endpoint_keys": {"team_label": {"provider": "crawl4ai"}},
        "unread_input_words": {"demo.web.a": ["maxLength"]},
        "rejected": [{"at": "demo.web.z", "path": "input", "reason": "r"}],
    })
    uses = [violations.Use("crawl4ai.x", "crawl4ai", {"team_label": ["body.x"]}),
            violations.Use("demo.web.b", "demo", {"team_label": ["body.x"],
                                                  "input": {"queryParams": {"q": {"format": "uri"}}}})]
    errors = violations.audit(uses, [], set(), listed)
    assert any("also used by demo" in e for e in errors)
    assert any("demo.web.b: input uses format" in e for e in errors)
    assert any("unread_input_words.demo.web.a no longer uses maxLength" in e for e in errors)
    assert any("rejected demo.web.z input parses now" in e for e in errors)
    assert violations.audit([uses[0]], [], set(), violations.parse(
        {"endpoint_keys": {"team_label": {"provider": "crawl4ai"}}})) == []


def test_the_shipped_violation_list_is_exactly_what_the_catalog_still_violates():
    from scripts import catalog_validate
    assert catalog_validate.main([]) == 0
    assert violations.load().cost_keys and violations.load().endpoint_keys


def test_the_shrink_only_check_refuses_a_new_entry():
    from scripts.catalog_shrink_only import grown
    base = "rejected: [{at: a.b, path: input, reason: r}]\n"
    assert grown(base, base) == []
    assert grown(base, "rejected: [{at: a.b, path: input, reason: r}, {at: a.c, path: input, reason: r}]\n") \
        == [("rejected", "a.c", "input")]
    assert grown(base, "rejected: []\n") == []


def test_every_exported_field_carries_a_kind_and_credential_placement_is_gated():
    schemas = json_schemas()
    for schema in schemas.values():
        for name, model in {**schema.get("$defs", {}), "": schema}.items():
            for prop, spec in model.get("properties", {}).items():
                assert spec.get("x-kind") in KINDS, f"{name}.{prop}"
    manifest = schemas["manifest.schema.json"]["$defs"]
    gated = {f"{m}.{p}" for m, spec in manifest.items() for p, s in spec.get("properties", {}).items()
             if s.get("x-gated")}
    assert {"Auth.kind", "Auth.base_url", "Auth.legacy_base_urls", "Auth.hosts", "Auth.required_headers",
            "Credential.in", "Credential.name", "Credential.format", "Credential.encode", "Pair.part",
            "Host.base_url", "Host.credential"} <= gated
    assert not any(f"{m}.label" in gated or f"{m}.placeholder" in gated for m in manifest)
    json.dumps(schemas)


MANIFEST = {
    "provider": "demo",
    "display": {"name": "Demo", "category": "SEO", "summary": "s", "docs_url": "https://docs.example.com"},
    "connect": {"setup_url": "https://example.com", "action": "Get a key", "steps": ["Copy it."]},
    "auth": {"kind": "key", "base_url": "https://api.example.com",
             "credential": {"in": "header", "name": "Authorization", "format": "Bearer {secret}"},
             "hosts": [{"base_url": "https://llm.example.com"}]},
    "probe": {"request": {"path": "/usage", "host": "llm.example.com"}},
}


@pytest.mark.parametrize("change,message", [
    ({"probe": {"request": {"path": "/usage", "host": "evil.example.com"}}}, "not an approved host"),
    ({"auth": MANIFEST["auth"] | {"base_url": "https://user@api.example.com"}}, "plain https"),
    ({"auth": MANIFEST["auth"] | {"credential": {"in": "header", "name": "X", "format": "Bearer"}}}, "{secret}"),
    ({"pricing": {"credit": {"usd": "0.01", "kind": "treg_trial", "basis": "b", "source": "s",
                             "checked": "2026-10-01"}}}, "trial_calls_per_team_day"),
    ({"capacity": {"seed": {"meter": "credits", "funding": "manual", "observed_by": "api"},
                   "balance": {"none": "no api", "read": {"path": "response.b", "unit": "USD"}}}}, "exactly one"),
    ({"defaults": {"credential": {"in": "query"}}}, "placement is provider-only"),
])
def test_the_manifest_refuses_each_invalid_shape(change, message):
    Manifest.model_validate(MANIFEST)
    with pytest.raises(ValidationError, match=message):
        Manifest.model_validate(MANIFEST | change)


def test_a_plugin_resolves_by_import_from_its_own_providers_module_only():
    with pytest.raises(ValueError, match="own provider's module, fiber_ai"):
        plugins.resolve("other.hold", "fiber-ai")
    with pytest.raises(ValueError, match="does not import"):
        plugins.resolve("fiber_ai.hold", "fiber-ai")


@pytest.mark.parametrize("expr", ["coalesce(data, []) == []", "a.b[0].c", ".", "x == 'safe'",
                                  "join(first, last)", "count / 100"])
def test_every_adapter_expression_form_parses(expr):
    expressions.parse(expr)


@pytest.mark.parametrize("expr", ["", "nope(x)", "join(a, b", "a..b", "a b"])
def test_a_malformed_adapter_expression_is_refused(expr):
    with pytest.raises(ValueError):
        expressions.parse(expr)


def test_a_malformed_adapter_fails_the_load(tmp_path):
    (tmp_path / "adapters.yaml").write_text("adapters:\n  demo.web.a: {out: {email: 'nope(x)'}}\n")
    with pytest.raises(ValueError, match="unknown transform 'nope'"):
        _catalog(tmp_path, {"demo.yaml": "provider: demo\nendpoints:\n" + ROW.format(name="a")})
    (tmp_path / "adapters.yaml").write_text("adapters:\n  demo.web.a: {outt: {}}\n")
    with pytest.raises(ValueError, match="unknown key outt"):
        _catalog(tmp_path, {"demo.yaml": "provider: demo\nendpoints:\n" + ROW.format(name="a")})
