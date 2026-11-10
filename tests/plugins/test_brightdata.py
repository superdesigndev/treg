import gzip

import pytest

from treg.domain.plugins import Answer, Request, brightdata


def _records(raw: bytes):
    return brightdata.records(Request(method="POST", query={}, body=None),
                              Answer(status=200, headers={}, raw=raw, body=None))


@pytest.mark.parametrize(("raw", "count"), [
    (b'[{"url": "a"}, {"url": "b"}]', 2),
    (b"[]", 0),
    (b"", 0),
    (b"  \n ", 0),
    (b'{"snapshot_id": "s_1"}', 0),
    (b'{"status": "running"}', 0),
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
