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
