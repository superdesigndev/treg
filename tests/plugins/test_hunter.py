import json

import pytest

from treg.domain.plugins import Answer, Request, hunter

REQUEST = Request(method="GET", query={}, body=None)


def _answer(doc: object, *, raw: bytes | None = None) -> Answer:
    body = raw if raw is not None else json.dumps(doc).encode()
    return Answer(status=200, headers={}, raw=body, body=doc)


def _emails(rows: object) -> Answer:
    return _answer({"data": {"domain": "x.com", "emails": rows}})


@pytest.mark.parametrize("count,billed", [(0, 0), (1, 10), (9, 10), (10, 10), (11, 20), (100, 100), (101, 110)])
def test_emails_returned_rounds_the_returned_emails_up_to_a_whole_ten(count, billed):
    assert hunter.emails_returned(REQUEST, _emails([{"value": f"p{i}@x.com"} for i in range(count)])) == billed


def test_emails_returned_counts_every_returned_row():
    assert hunter.emails_returned(REQUEST, _emails([None] * 11)) == 20


@pytest.mark.parametrize("doc", [{"data": {"emails": None}}, {"data": {"emails": ""}}, {"data": {}}, {"data": None},
                                 {"errors": [{"id": "wrong_params"}]}, [], None])
def test_emails_returned_reads_no_count_without_an_email_list(doc):
    assert hunter.emails_returned(REQUEST, _answer(doc)) is None


@pytest.mark.parametrize("doc,unanswered", [
    ({"data": {"email": "person@example.com"}}, False),
    ({"data": {"email": None}}, False),
    ({"data": {"email": "garbage"}}, True),
    ({"data": {"email": " person@example.com"}}, True),
    ({"data": {"email": ""}}, True),
    ({"data": {"email": 0}}, True),
    ({"data": {}}, True),
    ({"data": None}, True),
    ({}, True),
    ([], True),
    ({"errors": [{"id": "wrong_params"}], "data": {"email": "person@example.com"}}, True),
    ({"error": None, "data": {"email": "person@example.com"}}, False),
])
def test_finder_unanswered_is_any_json_answer_but_an_address_or_a_null(doc, unanswered):
    assert hunter.finder_unanswered(REQUEST, _answer(doc)) is unanswered


def test_finder_unanswered_leaves_a_body_that_is_not_json_alone():
    assert hunter.finder_unanswered(REQUEST, _answer(None, raw=b"<html>")) is False


@pytest.mark.parametrize("doc,unanswered", [
    ({"data": {"emails": [{"value": "person@example.com"}]}}, False),
    ({"data": {"emails": []}}, False),
    ({"data": {"emails": [{"value": ""}]}}, True),
    ({"data": {"emails": [{"value": "person@example.com"}, None]}}, True),
    ({"data": {"emails": {}}}, True),
    ({"data": {}}, True),
    ({}, True),
    ("text", True),
    ({"errors": [{"id": "rate_limit"}], "data": {"emails": []}}, True),
])
def test_domain_search_unanswered_is_any_json_answer_but_rows_with_an_address_or_none(doc, unanswered):
    assert hunter.domain_search_unanswered(REQUEST, _answer(doc)) is unanswered


def test_domain_search_unanswered_leaves_a_body_that_is_not_json_alone():
    assert hunter.domain_search_unanswered(REQUEST, _answer(None, raw=b"")) is False
