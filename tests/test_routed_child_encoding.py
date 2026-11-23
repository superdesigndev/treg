"""A routed child's answer is read as identity bytes even when the provider compressed it.

The routed call drops the caller's `Accept-Encoding`, so the relay asks the provider for identity,
but some providers compress regardless (Keepa gzips every answer). Read raw, such an answer did not
parse as JSON and every hit became a miss."""

from __future__ import annotations

import gzip
import json
import zlib

import pytest

from treg.application.call import route
from treg.application.call.types import UpstreamResponse


def _response(body: bytes, encoding: str | None) -> UpstreamResponse:
    headers = ((b"content-type", b"application/json"),)
    if encoding is not None:
        headers += ((b"Content-Encoding", encoding.encode()),)

    async def stream():
        for i in range(0, len(body), 1000):
            yield body[i:i + 1000]

    async def close() -> None:
        return None

    return UpstreamResponse(status=200, raw_headers=headers, body_stream=stream(), close=close)


ANSWER = json.dumps({"products": [{"asin": "B002DYIZH6", "title": "Whey"}]}).encode()


@pytest.mark.parametrize("encoding, body", [
    ("gzip", gzip.compress(ANSWER)),
    ("x-gzip", gzip.compress(ANSWER)),
    ("deflate", zlib.compress(ANSWER)),
    ("deflate", zlib.compress(ANSWER)[2:-4]),  # raw deflate, the other "deflate" in the wild
    ("identity", ANSWER),
    (None, ANSWER),
])
async def test_a_child_answer_is_read_as_identity_bytes(encoding, body):
    assert await route._read(_response(body, encoding)) == ANSWER


async def test_a_body_that_does_not_decode_stays_as_it_came():
    assert await route._read(_response(b"not gzip at all", "gzip")) == b"not gzip at all"


async def test_an_encoding_it_does_not_undo_stays_as_it_came():
    assert await route._read(_response(b"\x28\xb5\x2f\xfd", "zstd")) == b"\x28\xb5\x2f\xfd"


async def test_a_compressed_answer_cannot_inflate_past_the_routed_cap():
    bomb = gzip.compress(b"0" * (route.ROUTED_BODY_MAX + 1))
    assert len(bomb) < route.ROUTED_BODY_MAX
    with pytest.raises(route._ChildTooLarge):
        await route._read(_response(bomb, "gzip"))
