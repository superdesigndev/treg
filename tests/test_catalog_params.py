"""The one parameter check (`catalog.params`): the function and the declarations it reads."""

from __future__ import annotations

import pytest

from treg.domain.catalog import params

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
    ({"type": "string", "format": "uri"}, "format is one of url, email, binary"),
    ({"type": "integer", "format": "url"}, "format is one of url, email, binary"),
    ({"type": "string", "enum": [False]}, "enum entries false are not a string"),
    ({"type": "array", "items": {"type": "string"}, "enum": ["a"]}, "an enum of each item goes in items"),
    ({"type": "array", "items": {"type": "integer", "enum": ["1"]}}, "x[]: enum entries 1 are not a whole number"),
])
def test_a_declaration_is_held_to_the_vocabulary(spec, error):
    errors = params.declaration_errors(spec, "x")
    assert (error is None and errors == []) or any(error in e for e in errors), errors
