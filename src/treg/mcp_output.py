"""Bound the team MCP client's copy of a relay before HTTPX collects the ASGI body."""

import codecs
import zlib
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import jsontext


BODY_BYTES = 20 * 1024
ENVELOPE_BYTES = 28 * 1024
TEXT_CAPTURE_BYTES = 64 * 1024
MEDIA_BYTES = 1024 * 1024
SUBMISSION_BYTES = 8 * 1024 * 1024  # /tasks verifies the relayed submission by digest
PREVIEW_BYTES = 1024
NATIVE_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


def _textual(mime: str) -> bool:
    return (not mime or mime.startswith("text/") or mime.endswith(("+json", "+xml"))
            or mime in ("application/json", "application/xml", "application/javascript",
                        "application/x-ndjson"))


def content_mime(content_type: str) -> str:
    return content_type.partition(";")[0].strip().lower()


def is_native_media(mime: str) -> bool:
    return mime in NATIVE_IMAGE_TYPES or mime.startswith("audio/")


def omitted_body(
    *, size: int, mime: str, status: int, path: str, textual: bool = True,
    encoding: str = "identity", location: str = "",
    head: bytes | bytearray = b"", tail: bytes | bytearray = b"",
) -> dict:
    receipt = {"omitted": True, "content_type": mime or None, "size_bytes": size}
    if 200 <= status < 300 and 0 < len(location) <= 2048:
        try:
            url = urlsplit(location)
            if url.scheme in ("https", "http") and url.hostname and url.username is None and url.password is None:
                receipt["content_location"] = location
        except ValueError:
            pass
    if encoding not in ("", "identity"):
        receipt["content_encoding"] = encoding
    elif textual:
        receipt["head"] = head[:PREVIEW_BYTES].decode("utf-8", "replace")
        if size > PREVIEW_BYTES:
            receipt["tail"] = tail[-PREVIEW_BYTES:].decode("utf-8", "replace")
    if status >= 400:
        note = "The error body exceeds the MCP budget and is not archived."
    elif not textual:
        download = ("Use content_location to fetch it." if "content_location" in receipt
                    else "Use the treg CLI or HTTP API to download it.")
        note = f"The binary response exceeds the MCP budget and is not archived. {download}"
    elif path.startswith("/call/"):
        note = "The body exceeds the MCP budget. call_result can check whether a copy was archived."
    else:
        note = "The stored result exceeds the MCP budget."
    receipt["note"] = note
    return receipt


def _decompress(raw: bytes, encoding: str, limit: int) -> bytes | None:
    if encoding not in ("gzip", "x-gzip", "deflate"):
        return None
    modes = (32 + zlib.MAX_WBITS,)
    if encoding == "deflate":
        modes += (-zlib.MAX_WBITS,)
    for mode in modes:
        decoder = zlib.decompressobj(mode)
        try:
            body = decoder.decompress(raw, limit + 1)
        except zlib.error:
            continue
        if len(body) <= limit and decoder.eof and not decoder.unconsumed_tail and not decoder.unused_data:
            return body
        return None
    return None


class BoundedCallResponse:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path.startswith("/call/") or
                (path.startswith("/calls/") and path.endswith("/result"))):
            await self.app(scope, receive, send)
            return

        start: Message = {}
        mime = encoding = ""
        limit = TEXT_CAPTURE_BYTES
        size = 0
        prefix = bytearray()
        tail = bytearray()

        async def collect(message: Message) -> None:
            nonlocal start, mime, encoding, limit, size
            if message["type"] == "http.response.start":
                start = message.copy()
                headers = Headers(raw=start["headers"])
                mime = content_mime(headers.get("content-type", ""))
                encoding = headers.get("content-encoding", "identity").strip().lower()
                if (path.startswith("/call/") and 200 <= start["status"] < 300
                        and headers.get("x-treg-async") and _textual(mime)):
                    limit = SUBMISSION_BYTES
                elif is_native_media(mime):
                    limit = MEDIA_BYTES
                return
            if message["type"] != "http.response.body":
                await send(message)
                return
            chunk = memoryview(message.get("body", b""))
            size += len(chunk)
            if len(prefix) < limit:
                prefix.extend(chunk[:limit - len(prefix)])
            if len(chunk) >= PREVIEW_BYTES:
                tail[:] = chunk[-PREVIEW_BYTES:]
            elif chunk:
                tail.extend(chunk)
                del tail[:-PREVIEW_BYTES]
            if message.get("more_body"):
                return

            if size == 0:
                await send(start)
                await send({"type": "http.response.body", "body": b""})
                return

            headers = Headers(raw=start["headers"])
            status = start["status"]
            body = bytes(prefix)
            complete = size <= limit
            if complete and encoding not in ("", "identity"):
                decoded = _decompress(body, encoding, limit)
                complete = decoded is not None
                if decoded is not None:
                    body = decoded
            textual = _textual(mime)
            if not mime:
                try:
                    codecs.getincrementaldecoder("utf-8")().decode(body, final=complete)
                except UnicodeDecodeError:
                    textual = False
            native = 200 <= status < 300 and is_native_media(mime)
            if not complete or not (textual or native):
                body = jsontext.dumps_bytes(omitted_body(
                    size=size, mime=mime, status=status, path=path, textual=textual,
                    encoding=encoding, location=headers.get("content-location", ""),
                    head=prefix, tail=tail,
                ), separators=(",", ":"))
                content_type = b"application/json"
            else:
                content_type = headers.get("content-type", "").encode("latin-1")
            start["headers"] = [(k, v) for k, v in start["headers"] if k.lower() not in
                                (b"content-type", b"content-encoding", b"content-length", b"transfer-encoding")]
            if content_type:
                start["headers"].append((b"content-type", content_type))
            start["headers"].append((b"content-length", str(len(body)).encode()))
            await send(start)
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, collect)
