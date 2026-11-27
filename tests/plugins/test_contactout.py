import json

import pytest

from treg.domain.plugins import Answer, Request, contactout

REQUEST = Request(method="POST", query={}, body={"profiles": []})


def _answer(doc: object) -> Answer:
    return Answer(status=200, headers={}, raw=json.dumps(doc).encode(), body=doc)


def test_profiles_matched_counts_profiles_not_hashes():
    doc = {"status_code": 200, "matches": {"emails": ["h"] * 11}, "matches_found": 4,
           "unmatched": ["u"], "unmatched_count": 1}
    assert contactout.profiles_matched(REQUEST, _answer(doc)) == 4


@pytest.mark.parametrize("doc", [{"matches_found": None}, {"matches_found": "4"}, {"matches_found": 4.5},
                                 {"matches_found": -1}, {"matches_found": True}, {}, [], None])
def test_profiles_matched_reads_no_count_without_a_whole_number(doc):
    assert contactout.profiles_matched(REQUEST, _answer(doc)) is None


def _req(body: object) -> Request:
    return Request(method="POST", query={}, body=body)


def _job(result: object) -> Answer:
    return _answer({"data": {"uuid": "u", "status": "DONE", "result": result}})


def test_verdicts_billed_counts_only_the_verdicts_contactout_bills():
    result = {"a@x.com": "valid", "b@x.com": "invalid", "c@x.com": "accept_all",
              "d@x.com": "unknown", "e@x.com": "disposable", "f@x.com": None}
    assert contactout.verdicts_billed(_req({}), _job(result)) == 3
    assert contactout.verdicts_billed(_req({}), _job({})) == 0


@pytest.mark.parametrize("doc", [{"status": "QUEUED", "job_id": "j"}, {"data": {"result": []}}, [], None])
def test_verdicts_billed_reads_nothing_without_a_result_object(doc):
    assert contactout.verdicts_billed(_req({}), _answer(doc)) is None


@pytest.mark.parametrize(("body", "held"), [
    ({"profiles": ["a", "b"]}, {"work_email": 2, "personal_email": 2}),
    ({"profiles": ["a", "b"], "include_phone": True}, {"work_email": 2, "personal_email": 2, "phone": 2}),
    ({"profiles": ["a"], "email_type": "work"}, {"work_email": 1}),
    ({"profiles": ["a"], "email_type": "personal", "include_phone": True}, {"personal_email": 1, "phone": 1}),
    ({"profiles": ["a"], "email_type": "none"}, {}),
    ({"profiles": ["a"], "include_phone": "true"}, {"work_email": 1, "personal_email": 1, "phone": 1}),
    ({"profiles": ["a"], "include_phone": "false"}, {"work_email": 1, "personal_email": 1}),
    ({"profiles": ["a"], "email_type": "WORK"}, {"work_email": 1}),
    ({"profiles": ["a"], "email_type": "other"}, {"work_email": 1, "personal_email": 1}),
    ({"profiles": ["a"], "email_type": ["work"]}, {"work_email": 1, "personal_email": 1}),
    (None, {"work_email": 0, "personal_email": 0}),
])
def test_contacts_held_holds_every_profile_at_each_type_it_asks_for(body, held):
    assert contactout.contacts_held(_req(body)) == held


def test_contacts_found_counts_profiles_per_asked_type():
    result = {
        "p1": {"emails": ["w", "p"], "personal_emails": ["p"], "work_emails": ["w"], "phones": ["1", "2"]},
        "p2": {"emails": ["p"], "personal_emails": ["p"], "work_emails": [], "phones": []},
        "p3": {"emails": [], "personal_emails": [], "work_emails": [], "phones": []},
    }
    asked_all = _req({"profiles": ["p1", "p2", "p3"], "include_phone": True})
    assert contactout.contacts_found(asked_all, _job(result)) == {"work_email": 1, "personal_email": 2, "phone": 1}
    work_only = _req({"profiles": ["p1", "p2", "p3"], "email_type": "work"})
    assert contactout.contacts_found(work_only, _job(result)) == {"work_email": 1}


def test_contacts_found_reads_nothing_without_a_result_object():
    assert contactout.contacts_found(_req({"profiles": ["p"]}), _answer({"status": "QUEUED", "job_id": "j"})) is None
