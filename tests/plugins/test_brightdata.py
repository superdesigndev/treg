import gzip

import pytest

from treg.domain.plugins import Answer, Request, brightdata


def _records(raw: bytes):
    return brightdata.records(Request(method="POST", query={}, body=None),
                              Answer(status=200, headers={}, raw=raw, body=None))


@pytest.mark.parametrize(("raw", "count"), [
    (b'[{"url": "a"}, {"url": "b"}]', 2),
    (b"[]", 0),
    (b'[{"error": "Page not found", "error_code": "dead_page", "input": {"url": "a"}}]', 0),
    (b'[{"url": "a"}, {"error": "x", "error_code": "dead_page", "input": {"url": "b"}}]', 1),
    (b'{"url": "a"}\n{"error": "x", "error_code": "dead_page"}\n', 1),
    (b"", 0),
    (b"  \n ", 0),
    (b'{"snapshot_id": "s_1"}', 0),
    (b'{"snapshot_id": "s_1", "message": "m"}', 0),
    (b'{"status": "running"}', 0),
    (b'{"status": "running", "message": "not ready"}', 0),
    (b'{"error": "x"}', 0),
    (b'{"account": "a", "followers": 1}', 1),
    (b'{"account": "a"}\n', 1),
    (b'[{"timestamp": "t", "input": {"url": "a"}}]', 0),
    (b'[{"timestamp": "t", "input": {"url": "a"}}, {"url": "b", "post_id": "p"}]', 1),
    (b'{"timestamp": "t", "input": {"url": "a"}}', 0),
    (b'{"error": "x", "error_code": "dead_page", "input": {"url": "a"}}', 0),
    (b'{"a": 1}\n{"a": 2}\n\n{"a": 3}\n', 3),
    (b"url,name\nhttps://a,x\nhttps://b,y\n", 2),
    (b"url,name\n", None),
    (b'[{"url": "a"},', None),
    (b'{"a": 1}\n{"a": ', None),
    (gzip.compress(b'[{"url": "a"}]', mtime=0), None),
    (b"42", None),
])
def test_records_counts_each_delivery_format(raw, count):
    assert _records(raw) == count


@pytest.mark.parametrize(("raw", "miss"), [
    (b"[]", True),
    (b'{"snapshot_id": "s_1"}', True),
    (b'[{"error": "x", "error_code": "dead_page"}]', True),
    (b'[{"timestamp": "t", "input": {"url": "a"}}]', True),
    (b'[{"followers": 1, "biography": "b"}]', False),
    (b'[{"error_code": "dead_page"}, {"account": "a"}]', False),
    (b'{"account": "a"}\n', False),
    (b"url,name\nhttps://a,x\n", False),
    (gzip.compress(b'[{"url": "a"}]', mtime=0), False),
])
def test_no_records_is_a_miss_only_when_nothing_billable_was_delivered(raw, miss):
    assert brightdata.no_records(Request(method="POST", query={}, body=None),
                                 Answer(status=200, headers={}, raw=raw, body=None)) is miss
