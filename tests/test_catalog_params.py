"""The one parameter check (`catalog.params`): the function, the declarations it reads, and the
three roads that call it (a catalog row's query and body on treg's key and on the team's own key,
and a routed call's identity and filters), each refusing before anything is held or relayed."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from treg.application.call import service as call_service
from treg.config import get_settings
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


# -- the roads: every refusal is a 400 naming the parameter, before any hold or relay -------------------

REFUSED = "catalog_parameter_invalid"


def _upstream(monkeypatch, answer: dict) -> list:
    seen: list = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"*": [(200, answer)] * 10}, seen))
    return seen


async def _refused(r, parameter: str, seen: list, before: int, clients: AsyncClient, value=None) -> None:
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert (detail["error"], detail["parameter"]) == (REFUSED, parameter), detail
    assert detail["message"].endswith("Nothing was charged.")
    assert seen == [] and await _balance(clients) == before
    if isinstance(value, str) and len(value) > 3:
        assert value not in r.text


EXTRACT = {"urls": ["https://example.com"], "extract_depth": "basic"}
BODY_CASES = [
    ({"chunks_per_source": 5.5}, "body.chunks_per_source"),
    ({"chunks_per_source": "abc"}, "body.chunks_per_source"),
    ({"chunks_per_source": True}, "body.chunks_per_source"),
    ({"chunks_per_source": 0}, "body.chunks_per_source"),
    ({"chunks_per_source": -1}, "body.chunks_per_source"),
    ({"extract_depth": "deepest"}, "body.extract_depth"),
    ({"urls": [f"https://example.com/{i}" for i in range(21)]}, "body.urls"),
    ({"urls": ["https://example.com", 7]}, "body.urls"),
    ({"timeout": 61}, "body.timeout"),
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
async def test_a_body_parameter_outside_its_declaration_is_refused_on_every_tier(
        clients: AsyncClient, monkeypatch, tavily_on, own_key):
    if own_key:
        assert (await clients.post("/secrets", json={"name": "tavily", "value": "OWN-TAVILY"})).status_code == 200
    seen = _upstream(monkeypatch, {"results": [{"url": "https://example.com", "raw_content": "x"}],
                                   "failed_results": []})
    for patch, parameter in BODY_CASES:
        before = await _balance(clients)
        r = await clients.post("/call/tavily.web.extract", json={**EXTRACT, **patch})
        await _refused(r, parameter, seen, before, clients, next(iter(patch.values())))
    for patch in SERVED_BODIES:
        r = await clients.post("/call/tavily.web.extract", json={**EXTRACT, **patch})
        assert r.status_code == 200, (patch, r.text)
        assert seen.pop()[3] == {**EXTRACT, **patch}, "relayed as sent"


FETCH = {"url": "https://example.com", "respond_with": "markdown"}
QUERY_CASES = [
    ({"assert_status_code": "5.5"}, "queryParams.assert_status_code"),
    ({"assert_status_code": "abc"}, "queryParams.assert_status_code"),
    ({"assert_status_code": "true"}, "queryParams.assert_status_code"),
    ({"assert_status_code": "0"}, "queryParams.assert_status_code"),
    ({"assert_status_code": "-1"}, "queryParams.assert_status_code"),
    ({"engine": "fastest"}, "queryParams.engine"),
    ({"locale": "x" * 65}, "queryParams.locale"),
]


@pytest.mark.parametrize("own_key", [False, True], ids=["treg-key", "own-key"])
async def test_a_query_parameter_outside_its_declaration_is_refused_on_every_tier(
        clients: AsyncClient, monkeypatch, own_key):
    monkeypatch.setenv("TREG_PLATFORM_KEY_LITESCRAPE", "PLATFORM-LITESCRAPE")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "litescrape")
    get_settings.cache_clear()
    try:
        if own_key:
            assert (await clients.post("/secrets", json={"name": "litescrape", "value": "OWN"})).status_code == 200
        seen = _upstream(monkeypatch, {"markdown": "# Example"})
        for patch, parameter in QUERY_CASES:
            before = await _balance(clients)
            r = await clients.get("/call/litescrape.web.fetch", params={**FETCH, **patch})
            await _refused(r, parameter, seen, before, clients, next(iter(patch.values())))
        for patch in ({}, {"assert_status_code": "200"}, {"engine": "curl"}, {"locale": "en-US"},
                      {"assert_status_code": ""}):
            r = await clients.get("/call/litescrape.web.fetch", params={**FETCH, **patch})
            assert r.status_code == 200, (patch, r.text)
            assert seen.pop()[2] == {**FETCH, **patch}
    finally:
        get_settings.cache_clear()


ROUTED_CASES = [
    ({"limit": 5.5}, "body.limit"), ({"limit": "abc"}, "body.limit"), ({"limit": True}, "body.limit"),
    ({"limit": 0}, "body.limit"), ({"limit": -1}, "body.limit"), ({"country": "fr"}, "body.country"),
    ({"q": "x" * 81}, "body.q"), ({"keywords": ["a", "b", "c"]}, "body.keywords"), ({"keywords": "a"}, "body.keywords"),
]


async def test_a_routed_identity_or_filter_outside_its_declaration_is_refused_before_any_provider(
        clients: AsyncClient, monkeypatch, people_search_on):
    contract = catalog_store.load().contracts["people.search"]
    monkeypatch.setitem(contract.filters, "limit", {"type": "integer", "min": 1, "max": 50})
    monkeypatch.setitem(contract.filters, "country", {"type": "string", "enum": ["us", "gb"]})
    monkeypatch.setitem(contract.filters, "keywords", {"type": "array", "items": {"type": "string"}, "maxItems": 2})
    monkeypatch.setitem(contract.identity_specs, "q", {"type": "string", "maxLength": 80})
    seen = _upstream(monkeypatch, {"results": [{"name": "Ada Lovelace", "title": "Engineer"}]})
    for patch, parameter in ROUTED_CASES:
        before = await _balance(clients)
        r = await clients.post("/call/treg.people.search", json={"q": "engineers in london", **patch})
        await _refused(r, parameter, seen, before, clients, next(iter(patch.values())))
    r = await clients.post("/call/treg.people.search", json={
        "q": "engineers in london", "limit": "10", "country": "us", "keywords": ["python"]})
    assert r.status_code == 200, r.text
    assert seen, "a provider was asked"


async def test_a_routed_call_reads_the_shipped_types(clients: AsyncClient, monkeypatch, people_search_on):
    seen = _upstream(monkeypatch, {"results": []})
    for patch, parameter in (({"limit": 5.5}, "body.limit"), ({"limit": "ten"}, "body.limit"), ({"q": ["a"]}, "body.q"),
                             ({"company_domain": 7}, "body.company_domain")):
        before = await _balance(clients)
        r = await clients.post("/call/treg.people.search", json={"q": "engineers", **patch})
        await _refused(r, parameter, seen, before, clients)
