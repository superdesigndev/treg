"""Responses are gzip-encoded for a client that accepts it, and every other response is untouched."""

from __future__ import annotations

import asyncio
import gzip
import zlib

import pytest

from treg.bootstrap_http import _GzipResponseMiddleware

_TEXT = b'{"items": [' + b", ".join(b'{"id": %d, "name": "row"}' % i for i in range(400)) + b"]}"


def _scope(accept: str | None) -> dict:
    headers = [(b"host", b"localhost")]
    if accept is not None:
        headers.append((b"accept-encoding", accept.encode()))
    return {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
            "scheme": "http", "path": "/", "raw_path": b"/", "query_string": b"", "root_path": "",
            "headers": headers, "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8000)}


def _responder(chunks: list[bytes], headers: dict[str, str], status: int = 200):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": status,
                    "headers": [(k.encode(), v.encode()) for k, v in headers.items()]})
        for i, chunk in enumerate(chunks):
            await send({"type": "http.response.body", "body": chunk,
                        "more_body": i < len(chunks) - 1})
    return app


async def _run(app, accept: str | None) -> list[dict]:
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await _GzipResponseMiddleware(app)(_scope(accept), receive, send)
    return sent


def _headers(start: dict) -> dict[str, str]:
    return {k.decode().lower(): v.decode() for k, v in start["headers"]}


def _body(sent: list[dict]) -> bytes:
    return b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


async def test_the_app_gzips_a_text_page_for_a_client_that_accepts_it(clients) -> None:
    plain = await clients.get("/llms.txt", headers={"Accept-Encoding": "identity"})
    zipped = await clients.get("/llms.txt", headers={"Accept-Encoding": "gzip"})

    assert plain.status_code == zipped.status_code == 200
    assert "content-encoding" not in plain.headers
    assert zipped.headers["content-encoding"] == "gzip"
    assert "accept-encoding" in zipped.headers["vary"].lower()
    assert zipped.content == plain.content


async def test_a_buffered_json_body_is_gzipped_with_its_compressed_length() -> None:
    sent = await _run(_responder([_TEXT], {"content-type": "application/json",
                                           "content-length": str(len(_TEXT))}), "gzip, br")

    headers = _headers(sent[0])
    assert headers["content-encoding"] == "gzip"
    assert headers["vary"] == "Accept-Encoding"
    assert int(headers["content-length"]) == len(_body(sent)) < len(_TEXT)
    assert gzip.decompress(_body(sent)) == _TEXT


@pytest.mark.parametrize("accept", [None, "identity", "br", "gzip;q=0", "gzip; q=0.000"])
async def test_a_client_that_does_not_accept_gzip_gets_the_body_as_sent(accept) -> None:
    sent = await _run(_responder([_TEXT], {"content-type": "application/json"}), accept)

    assert "content-encoding" not in _headers(sent[0])
    assert _body(sent) == _TEXT


@pytest.mark.parametrize("headers,status", [
    ({"content-type": "image/png"}, 200),
    ({"content-type": "application/octet-stream"}, 200),
    ({"content-type": "text/event-stream"}, 200),
    ({"content-type": "application/json", "content-encoding": "br"}, 200),
    ({"content-type": "application/json", "cache-control": "no-transform"}, 200),
    ({"content-type": "application/json", "content-range": "bytes 0-9/100"}, 206),
    ({}, 200),
])
async def test_responses_that_must_not_be_reencoded_pass_through(headers, status) -> None:
    sent = await _run(_responder([_TEXT], headers, status), "gzip")

    assert _headers(sent[0]) == headers
    assert _body(sent) == _TEXT


async def test_a_small_body_is_not_worth_encoding() -> None:
    sent = await _run(_responder([b'{"ok": true}'], {"content-type": "application/json"}), "gzip")

    assert "content-encoding" not in _headers(sent[0])
    assert _body(sent) == b'{"ok": true}'


async def test_a_small_stream_keeps_its_declared_length() -> None:
    chunks = [b'{"ok": ', b"true}"]
    sent = await _run(_responder(chunks, {"content-type": "application/json",
                                          "content-length": "12"}), "gzip")

    assert _headers(sent[0]) == {"content-type": "application/json", "content-length": "12"}
    assert _body(sent) == b'{"ok": true}'


async def test_a_stream_is_decodable_chunk_by_chunk_before_it_ends() -> None:
    first_read = asyncio.Event()
    received: list[dict] = []

    async def stream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/x-ndjson"),
                                (b"content-length", b"4096")]})
        await send({"type": "http.response.body", "body": b'{"line": 1}\n', "more_body": True})
        await first_read.wait()
        await send({"type": "http.response.body", "body": b'{"line": 2}\n', "more_body": False})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        received.append(message)
        if message["type"] == "http.response.body" and message.get("more_body"):
            first_read.set()

    await _GzipResponseMiddleware(stream)(_scope("gzip"), receive, send)

    headers = _headers(received[0])
    assert headers["content-encoding"] == "gzip"
    assert "content-length" not in headers
    decoder = zlib.decompressobj(31)
    assert decoder.decompress(received[1]["body"]) == b'{"line": 1}\n'
    assert decoder.decompress(received[2]["body"]) == b'{"line": 2}\n'
    assert decoder.eof


async def test_a_large_chunk_compresses_to_the_same_bytes() -> None:
    big = _TEXT * 200
    sent = await _run(_responder([big, big], {"content-type": "text/plain"}), "gzip")

    assert gzip.decompress(_body(sent)) == big + big
