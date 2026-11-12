"""The one parameter check (`catalog.params`): the function, the declarations it reads, and the
three roads that call it (a catalog row's query and body on treg's key and on the team's own key,
and a routed call's identity and filters), each refusing before anything is held or relayed."""

from __future__ import annotations

import json
import logging

import pytest
from httpx import AsyncClient

from treg.application.call import resolve as call_resolve
from treg.application.call import service as call_service
from treg.config import Settings, get_settings
from treg.domain.catalog import params
from treg.domain.catalog import store as catalog_store
from test_marketplace_call import _balance, platform_on  # noqa: F401
from test_routing import _relay_by_provider, people_search_on  # noqa: F401

COUNT = {"type": "integer", "min": 1, "max": 20}


@pytest.mark.parametrize("value,reason", [
    (5, None), ("5", None), (None, None), ("", None),
    (5.5, "must be a whole number"), (5.0, "must be a whole number"), ("abc", "must be a whole number"),
    (True, "must be a whole number"), ("5.5", "must be a whole number"), (" 5", "must be a whole number"),
    (0, "must be at least 1"), (-1, "must be at least 1"), (21, "must be at most 20"),
    ("0" * 5000 + "21", "must be at most 20"), ("9" * 5000, "must be at most 20"),
])
def test_an_integer_is_a_json_integer_or_ascii_digits_within_its_range(value, reason):
    assert params.refusal(COUNT, value) == reason


@pytest.mark.parametrize("value,reason", [
    ("5", None), ("-1", "must be at least 1"), ("5.5", "must be a whole number"), ("true", "must be a whole number"),
    ("٣", "must be a whole number"), (["5", "6"], "must be sent once"),
])
def test_a_query_string_is_read_by_the_declared_type(value, reason):
    assert params.refusal(COUNT, value, text=True) == reason


def test_every_type_word_reads_its_own_values():
    assert params.refusal({"type": "number", "min": 0}, 0.5) is None
    assert params.refusal({"type": "number"}, "0.5") == "must be a number"
    assert params.refusal({"type": "number"}, "0.5", text=True) is None
    assert params.refusal({"type": "number"}, float("nan")) == "must be a number"
    assert params.refusal({"type": "number", "max": 2}, json.loads("1" + "0" * 400)) == "must be at most 2"
    assert params.refusal({"type": "boolean"}, True) is None
    assert params.refusal({"type": "boolean"}, "true") == "must be true or false"
    assert params.refusal({"type": "boolean"}, "TRUE", text=True) is None
    assert params.refusal({"type": "boolean"}, 1) == "must be true or false"
    assert params.refusal({"type": "object"}, {"a": 1}) is None
    assert params.refusal({"type": "object"}, '{"a": 1}', text=True) is None
    assert params.refusal({"type": "object"}, "a=1", text=True) == "must be a JSON object"
    assert params.refusal({"type": "string"}, 5) == "must be a string"
    assert params.refusal({"type": ["boolean", "object"]}, True) is None
    assert params.refusal({"type": ["boolean", "object"]}, {"maxCharacters": 5}) is None
    assert params.refusal({"type": ["boolean", "object"]}, "yes") == "must be true or false or a JSON object"


def test_a_required_parameter_is_checked_even_when_null_and_an_unknown_type_is_not_checked():
    assert params.refusal({"type": "integer", "required": True}, None) == "must be a whole number"
    assert params.refusal({"type": "string", "required": True}, "") is None
    assert params.refusal({"type": "array[string]"}, 5) is None
    assert params.refusal({"note": "no type"}, 5) is None


def test_enum_length_and_items():
    mode = {"type": "string", "enum": ["basic", "advanced"]}
    assert params.refusal(mode, "basic") is None
    assert params.refusal(mode, "BASIC") == "must be one of basic, advanced"
    assert params.refusal({"type": "integer", "enum": [10, 25]}, "25", text=True) is None
    assert params.refusal({"type": "string", "enum": [f"v{i}" for i in range(11)]}, "x") == \
        "must be one of the declared values"
    text = {"type": "string", "minLength": 2, "maxLength": 4}
    assert params.refusal(text, "a") == "must be at least 2 characters"
    assert params.refusal(text, "abcde") == "must be at most 4 characters"
    assert params.refusal(text, "日本語で") is None, "characters, not bytes"
    urls = {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}, "minItems": 1, "maxItems": 2}
    assert params.refusal(urls, ["a"]) is None
    assert params.refusal(urls, []) == "must have at least 1 items"
    assert params.refusal(urls, ["a", "b", "a"]) == "must have at most 2 items"
    assert params.refusal(urls, ["a", 5]) == "item 1 must be a string"
    assert params.refusal(urls, ["a", "c"]) == "item 1 must be one of a, b"
    assert params.refusal(urls, ["a", None]) is None, "a null item is not sent"
    assert params.refusal(urls, "a,b", text=True) is None, "a comma list is relayed unread"
    assert params.refusal(urls, '["a","c"]', text=True) == "item 1 must be one of a, b"
    assert params.refusal(urls, ["a", "c"], text=True) == "item 1 must be one of a, b"
    whole = {"type": "array", "items": {"type": "string"}, "enum": [["work_email"]]}
    assert params.refusal(whole, ["work_email"]) is None
    assert params.refusal(whole, ["work_email", "phone"]) == 'must be one of ["work_email"]'


def test_a_refusal_never_quotes_the_value():
    secret = "sk-live-0123456789abcdef"
    for spec in (COUNT, {"type": "string", "enum": ["a"]}, {"type": "string", "maxLength": 3},
                 {"type": "array", "items": {"type": "integer"}}):
        for value in (secret, [secret]):
            assert secret not in (params.refusal(spec, value) or "")
            assert secret not in (params.refusal(spec, value, text=True) or "")


@pytest.mark.parametrize("spec,error", [
    ({"type": "string"}, None),
    ({"type": ["boolean", "object"]}, None),
    ({"type": "array", "items": {"type": "string"}, "minItems": 1}, None),
    ({"type": "str"}, "type str is none of"),
    ({"type": "url"}, "url and email are a format of a string"),
    ({"type": "array[string]"}, "type array[string] is none of"),
    ({}, "type is one of"),
    ({"type": ["string"]}, "a type list names two or more distinct type words"),
    ({"type": "array"}, "an array declares its items"),
    ({"type": "string", "items": {"type": "string"}}, "items belongs to an array"),
    ({"type": "integer", "minimum": 1}, "minimum is written min"),
    ({"type": "string", "min": 1}, "min bounds integer or number"),
    ({"type": "integer", "maxLength": 3}, "maxLength bounds string"),
    ({"type": "string", "maxItems": 3}, "maxItems bounds array"),
    ({"type": "integer", "min": 5, "max": 1}, "min is above max"),
    ({"type": "string", "format": "uri"}, "format is one of url, email"),
    ({"type": "string", "format": "binary"}, "format is one of url, email"),
    ({"type": "integer", "format": "url"}, "format is one of url, email"),
    ({"type": "string", "enum": [False]}, "enum entries false are not a string"),
    ({"type": "array", "items": {"type": "string"}, "enum": ["a"]}, "an enum of each item goes in items"),
    ({"type": "array", "items": {"type": "integer", "enum": ["1"]}}, "x[]: enum entries 1 are not a whole number"),
])
def test_a_declaration_is_held_to_the_vocabulary(spec, error):
    errors = params.declaration_errors(spec, "x")
    assert (error is None and errors == []) or any(error in e for e in errors), errors


def test_every_shipped_declaration_and_contract_field_is_in_the_vocabulary():
    cat = catalog_store.load()
    for contract in cat.contracts.values():
        for spec in [*contract.identity_specs.values(), *contract.filters.values()]:
            assert params.types_of(spec) and all(t in params.TYPES for t in params.types_of(spec)), spec


# -- the roads: in refuse mode a 400 naming the parameter, before any hold or relay; in log mode -------
# -- the request is relayed as sent and one line names the parameter and the rule, never the value ----

REFUSED = "catalog_parameter_invalid"


@pytest.fixture(params=["refuse", "log"])
def mode(request, monkeypatch):
    monkeypatch.setenv("TREG_PARAM_CHECK", request.param)
    get_settings.cache_clear()
    yield request.param
    get_settings.cache_clear()


def _upstream(monkeypatch, answer: dict) -> list:
    seen: list = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"*": [(200, answer)] * 40}, seen))
    return seen


async def _refused(r, parameter: str, seen: list, before: int, clients: AsyncClient, value=None) -> None:
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert (detail["error"], detail["parameter"]) == (REFUSED, parameter), detail
    assert detail["message"].endswith("Nothing was charged.")
    assert seen == [] and await _balance(clients) == before
    if isinstance(value, str) and len(value) > 3:
        assert value not in r.text


def _logged(caplog) -> list[dict]:
    lines = [r.getMessage() for r in caplog.records if r.name == "treg.param_check"]
    caplog.clear()
    assert all(line.startswith("catalog_parameter_would_refuse {") for line in lines), lines
    return [json.loads(line.split(" ", 1)[1]) for line in lines]


async def _checked(r, mode: str, caplog, seen: list, before: int, clients: AsyncClient, *, endpoint_id: str,
                   parameter: str, rule: str, tier: str, road: str, sent=None, value=None,
                   main_status: int = 200) -> None:
    """Refuse mode: the 400. Log mode: what main does, the request relayed as sent and answered (or
    `main_status`, what main answers it without asking a provider), and one line naming the
    endpoint, parameter, rule, tier and road."""
    if mode == "refuse":
        await _refused(r, parameter, seen, before, clients, value)
        assert _logged(caplog) == []
        return
    if main_status != 200:
        assert r.status_code == main_status, r.text
        assert seen == [] and await _balance(clients) == before
    else:
        assert r.status_code == 200, r.text
        assert seen, "relayed"
        if sent is not None:
            assert seen.pop() == sent, "relayed as sent"
        seen.clear()
    assert [line for line in _logged(caplog) if line["road"] == road] == [
        {"endpoint_id": endpoint_id, "parameter": parameter, "rule": rule, "tier": tier, "road": road}]


EXTRACT = {"urls": ["https://example.com"], "extract_depth": "basic"}
BODY_CASES = [
    ({"chunks_per_source": 5.5}, "body.chunks_per_source", "type"),
    ({"chunks_per_source": "abc"}, "body.chunks_per_source", "type"),
    ({"chunks_per_source": True}, "body.chunks_per_source", "type"),
    ({"chunks_per_source": 0}, "body.chunks_per_source", "range"),
    ({"chunks_per_source": -1}, "body.chunks_per_source", "range"),
    ({"extract_depth": "deepest"}, "body.extract_depth", "enum"),
    ({"urls": [f"https://example.com/{i}" for i in range(21)]}, "body.urls", "array_length"),
    ({"urls": ["https://example.com", 7]}, "body.urls", "items:type"),
    ({"timeout": 61}, "body.timeout", "range"),
]
SERVED_BODIES = [{}, {"chunks_per_source": 3}, {"chunks_per_source": "3"}, {"chunks_per_source": None},
                 {"extract_depth": "advanced"}, {"urls": [f"https://example.com/{i}" for i in range(20)]},
                 {"unlisted": "relayed as sent"}]


@pytest.fixture
def tavily_on(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_TAVILY", "PLATFORM-TAVILY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tavily")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("own_key", [False, True], ids=["treg-key", "own-key"])
async def test_a_body_parameter_outside_its_declaration_on_every_tier(
        clients: AsyncClient, monkeypatch, caplog, mode, tavily_on, own_key):
    caplog.set_level(logging.INFO, logger="treg.param_check")
    if own_key:
        assert (await clients.post("/secrets", json={"name": "tavily", "value": "OWN-TAVILY"})).status_code == 200
    tier = "credential" if own_key else "platform"
    seen = _upstream(monkeypatch, {"results": [{"url": "https://example.com", "raw_content": "x"}],
                                   "failed_results": []})
    for patch, parameter, rule in BODY_CASES:
        before = await _balance(clients)
        r = await clients.post("/call/tavily.web.extract", json={**EXTRACT, **patch})
        capped = not own_key and rule == "array_length"   # main's own cap on treg's key
        await _checked(r, mode, caplog, seen, before, clients, endpoint_id="tavily.web.extract", parameter=parameter,
                       rule=rule, tier="none" if capped else tier, road="catalog",
                       sent=("*", "POST", {}, {**EXTRACT, **patch}), value=next(iter(patch.values())),
                       main_status=400 if capped else 200)
    for patch in SERVED_BODIES:
        r = await clients.post("/call/tavily.web.extract", json={**EXTRACT, **patch})
        assert r.status_code == 200, (patch, r.text)
        assert seen.pop()[3] == {**EXTRACT, **patch}, "relayed as sent"
    assert _logged(caplog) == []
    repeated = b'{"urls": ["https://example.com"], "timeout": 1, "timeout": 61}'
    before = await _balance(clients)
    r = await clients.post("/call/tavily.web.extract", content=repeated,
                           headers={"content-type": "application/json"})
    await _checked(r, mode, caplog, seen, before, clients, endpoint_id="tavily.web.extract",
                   parameter="body.timeout", rule="range", tier=tier, road="catalog")
    r = await clients.post("/call/tavily.web.extract", content=repeated.replace(b"61", b"2"),
                           headers={"content-type": "application/json"})
    assert r.status_code == 200, r.text
    seen.pop()


async def test_log_mode_answers_as_if_there_were_no_check_and_logs_no_value(
        clients: AsyncClient, monkeypatch, caplog, tavily_on):
    """Log mode is main's relay: for a request the check would refuse, the provider receives the
    bytes the caller sent and the caller the same answer and charge as with no check at all; the
    line carries no part of the value."""
    monkeypatch.setenv("TREG_PARAM_CHECK", "log")
    get_settings.cache_clear()
    caplog.set_level(logging.INFO, logger="treg.param_check")
    answer = {"results": [{"url": "https://example.com", "raw_content": "x"}], "failed_results": []}
    sent: list[bytes] = []

    async def relay(request, upstream_url, tool, secrets, client, drop_params=None, force_identity=False):
        body = b""
        async for chunk in request.body_stream():
            body += chunk
        sent.append(body)
        return await _relay_by_provider({"*": [(200, answer)]}, [])(
            _Replayed(request, body), upstream_url, tool, secrets, client, drop_params, force_identity)

    monkeypatch.setattr(call_service, "relay", relay)
    raw = b'{"urls":["https://example.com"],"extract_depth":"secret-depth-value", "timeout" : 7}'

    async def call():
        before = await _balance(clients)
        r = await clients.post("/call/tavily.web.extract", content=raw, headers={"content-type": "application/json"})
        return r.status_code, r.content, sent.pop(), before - await _balance(clients)

    logged = await call()
    lines = [r.getMessage() for r in caplog.records if r.name == "treg.param_check"]
    monkeypatch.setattr(params, "check", lambda *args, **kwargs: None)
    assert logged == await call() and logged[0] == 200 and logged[2] == raw
    assert lines == ['catalog_parameter_would_refuse {"endpoint_id":"tavily.web.extract",'
                     '"parameter":"body.extract_depth","rule":"enum","tier":"platform","road":"catalog"}']
    assert "secret" not in caplog.text


class _Replayed:
    """The relayed request with its already-read body, for the scripted upstream."""

    def __init__(self, request, body: bytes):
        self._request, self._body = request, body

    def __getattr__(self, name):
        return getattr(self._request, name)

    def body_stream(self):
        async def stream():
            yield self._body
        return stream()


FETCH = {"url": "https://example.com", "respond_with": "markdown"}
QUERY_CASES = [
    ({"assert_status_code": "5.5"}, "queryParams.assert_status_code", "type"),
    ({"assert_status_code": "abc"}, "queryParams.assert_status_code", "type"),
    ({"assert_status_code": "true"}, "queryParams.assert_status_code", "type"),
    ({"assert_status_code": "0"}, "queryParams.assert_status_code", "range"),
    ({"assert_status_code": "-1"}, "queryParams.assert_status_code", "range"),
    ({"locale": "x" * 65}, "queryParams.locale", "length"),
]


@pytest.mark.parametrize("own_key", [False, True], ids=["treg-key", "own-key"])
async def test_a_query_parameter_outside_its_declaration_on_every_tier(
        clients: AsyncClient, monkeypatch, caplog, mode, own_key):
    caplog.set_level(logging.INFO, logger="treg.param_check")
    monkeypatch.setenv("TREG_PLATFORM_KEY_LITESCRAPE", "PLATFORM-LITESCRAPE")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "litescrape")
    get_settings.cache_clear()
    try:
        if own_key:
            assert (await clients.post("/secrets", json={"name": "litescrape", "value": "OWN"})).status_code == 200
        seen = _upstream(monkeypatch, {"markdown": "# Example"})
        for patch, parameter, rule in QUERY_CASES:
            before = await _balance(clients)
            r = await clients.get("/call/litescrape.web.fetch", params={**FETCH, **patch})
            await _checked(r, mode, caplog, seen, before, clients, endpoint_id="litescrape.web.fetch",
                           parameter=parameter, rule=rule, tier="credential" if own_key else "platform",
                           road="catalog", sent=("*", "GET", {**FETCH, **patch}, None),
                           value=next(iter(patch.values())))
        for patch in ({}, {"assert_status_code": "200"}, {"engine": "curl"}, {"locale": "en-US"},
                      {"assert_status_code": ""}):
            r = await clients.get("/call/litescrape.web.fetch", params={**FETCH, **patch})
            assert r.status_code == 200, (patch, r.text)
            assert seen.pop()[2] == {**FETCH, **patch}
        assert _logged(caplog) == []
        r = await clients.get("/call/litescrape.web.fetch", params={**FETCH, "engine": "fastest"})
        assert r.status_code == 400 and "parameter" not in r.json()["detail"], "strict_query refuses in either mode"
    finally:
        get_settings.cache_clear()


ROUTED_CASES = [
    ({"country": "fr"}, "body.country", "enum", 200), ({"q": "x" * 81}, "body.q", "length", 200),
    ({"keywords": ["a", "b", "c"]}, "body.keywords", "array_length", 200),
    ({"keywords": "a"}, "body.keywords", "type", 200),
    ({"company_domain": 7}, "body.company_domain", "type", 503),   # main: no candidate takes it alone
]


async def test_a_routed_identity_or_filter_outside_its_declaration_before_any_provider(
        clients: AsyncClient, monkeypatch, caplog, mode, people_search_on):
    caplog.set_level(logging.INFO, logger="treg.param_check")
    contract = catalog_store.load().contracts["people.search"]
    monkeypatch.setitem(contract.filters, "country", {"type": "string", "enum": ["us", "gb"]})
    monkeypatch.setitem(contract.filters, "keywords", {"type": "array", "items": {"type": "string"}, "maxItems": 2})
    monkeypatch.setitem(contract.identity_specs, "q", {"type": "string", "maxLength": 80})
    seen = _upstream(monkeypatch, {"results": [{"name": "Ada Lovelace", "title": "Engineer"}]})
    for patch, parameter, rule, main_status in ROUTED_CASES:
        before = await _balance(clients)
        r = await clients.post("/call/treg.people.search", json={"q": "engineers in london", **patch})
        await _checked(r, mode, caplog, seen, before, clients, endpoint_id="treg.people.search", parameter=parameter,
                       rule=rule, tier="routed", road="routed", value=next(iter(patch.values())), main_status=main_status)
    r = await clients.post("/call/treg.people.search", json={
        "q": "engineers in london", "limit": "10", "country": "us", "keywords": ["python"]})
    assert r.status_code == 200, r.text
    assert seen, "a provider was asked"
    assert _logged(caplog) == []


def test_every_routed_call_template_meets_its_own_declarations():
    cat = catalog_store.load()
    refused = {row["id"]: call_resolve._body_refusal(row, json.dumps(row["test_request"]["body"]).encode())
               for row in cat.by_id.values() if row.get("kind") == "routed" and row.get("test_request")}
    assert refused and not {k: v for k, v in refused.items() if v}, refused


@pytest.mark.parametrize("value,expected", [
    ("refuse", "refuse"), (" Refuse ", "refuse"), ("log", "log"), ("", "log"), ("block", "log"), ("off", "log")])
def test_the_switch_reads_refuse_and_any_other_value_as_log(monkeypatch, value, expected):
    monkeypatch.setenv("TREG_PARAM_CHECK", value)
    assert Settings().param_check == expected
