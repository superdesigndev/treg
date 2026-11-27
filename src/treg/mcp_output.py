"""Bound the team MCP client's copy of a relay before HTTPX collects the ASGI body."""

import codecs
import zlib
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import jsontext


@dataclass(frozen=True)
class OutputBudget:
    text_bytes: int
    envelope_bytes: int
    media_bytes: int
    capture_bytes: int
    compact_media_content: bool = False


# The archive and task poll bounds anchor the text limit. Allow room for the receipt while
# keeping both JSON channels within the MCP frame budget and limiting synchronous gzip work.
MAX_TEXT_BYTES = 2 * 1024 * 1024
STANDARD_ENVELOPE_BYTES = MAX_TEXT_BYTES + 256 * 1024
MAX_MEDIA_BYTES = 3_932_160  # exactly 5 MiB in base64 for vision API inputs
# Hosted chat connectors cap the combined tool result, including both JSON channels.
CHAT_TEXT_BYTES = 192 * 1024
CHAT_ENVELOPE_BYTES = 224 * 1024
CHAT_MEDIA_BYTES = 720 * 1024  # 960 KiB in base64, with room for metadata below 1 MiB
SUBMISSION_BYTES = 8 * 1024 * 1024  # /tasks verifies the relayed submission by digest
PREVIEW_BYTES = 1024
OMITTED_HEADER = "x-treg-mcp-omitted"
NATIVE_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})

STANDARD_BUDGET = OutputBudget(
    text_bytes=MAX_TEXT_BYTES, envelope_bytes=STANDARD_ENVELOPE_BYTES,
    media_bytes=MAX_MEDIA_BYTES, capture_bytes=MAX_TEXT_BYTES,
)
CHAT_SANDBOX_BUDGET = OutputBudget(
    text_bytes=CHAT_TEXT_BYTES, envelope_bytes=CHAT_ENVELOPE_BYTES,
    media_bytes=CHAT_MEDIA_BYTES, capture_bytes=CHAT_TEXT_BYTES * 2,
    compact_media_content=True,
)


def is_chat_sandbox(headers: Any, client_name: str = "") -> bool:
    headers = Headers(headers or {})
    ua = headers.get("user-agent", "").lower()
    origin = headers.get("origin", "").lower()
    return (ua.startswith("openai-mcp/") or "claude-user/" in ua
            or origin == "https://claude.ai" or client_name.strip().lower() == "claude-ai")


def budget_for(headers: Any, client_name: str = "") -> OutputBudget:
    return CHAT_SANDBOX_BUDGET if is_chat_sandbox(headers, client_name) else STANDARD_BUDGET


def _textual(mime: str) -> bool:
    return (not mime or mime.startswith("text/") or mime.endswith(("+json", "+xml"))
            or mime in ("application/json", "application/xml", "application/javascript",
                        "application/x-ndjson"))


def content_mime(content_type: str) -> str:
    return content_type.partition(";")[0].strip().lower()


def is_native_media(mime: str) -> bool:
    return mime in NATIVE_IMAGE_TYPES or mime.startswith("audio/")


def omitted_note(*, status: int, path: str, textual: bool, has_location: bool,
                 may_be_archived: bool | None = None, spooled: bool = False) -> str:
    if status >= 400:
        return "The error body exceeds the MCP budget and is not archived."
    fetch = "Use content_location to fetch it."
    if not textual:
        return ("The binary response exceeds the MCP budget and is not archived. "
                + (fetch if has_location else "Use the treg CLI or HTTP API to download it."))
    if spooled:
        return ("This inline-media response exceeds the MCP budget and is not archived. Narrow the "
                "response with the endpoint's fields parameter if supported, or fetch it with the treg CLI or HTTP /call/.")
    if path.startswith("/call/"):
        if may_be_archived is True:
            return ("The body exceeds the MCP budget. Call call_result(call_id, offset=0) to read "
                    "stored pages when archive retention is active for this team.")
        if may_be_archived is False:
            return ("The body exceeds the MCP budget and is not archived. "
                    + (fetch if has_location else "Use the treg CLI or HTTP API to fetch the full response."))
        return "The body exceeds the MCP budget. call_result can check whether a copy was archived."
    return "The stored result exceeds the MCP budget."


def _tail_preview(tail: bytes | bytearray) -> str:
    start = max(0, len(tail) - PREVIEW_BYTES)
    # With up to three bytes of context, skip only a valid code point that straddles the cut.
    for lead in range(start - 1, max(-1, start - 4), -1):
        if tail[lead] & 0xC0 != 0x80:
            for end in range(start + 1, min(lead + 4, len(tail)) + 1):
                try:
                    tail[lead:end].decode("utf-8")
                except UnicodeDecodeError:
                    continue
                if all(byte & 0xC0 == 0x80 for byte in tail[start:end]):
                    start = end
                break
            break
    return tail[start:].decode("utf-8", "replace")


def omitted_body(
    *, size: int, mime: str, status: int, path: str, textual: bool = True,
    encoding: str = "identity", location: str = "",
    head: bytes | bytearray = b"", tail: bytes | bytearray = b"",
    may_be_archived: bool | None = None, spooled: bool = False,
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
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        head = head[:PREVIEW_BYTES]
        receipt["head"] = decoder.decode(head, final=size <= len(head))
        if size > PREVIEW_BYTES:
            receipt["tail"] = _tail_preview(tail)
    receipt["note"] = omitted_note(
        status=status, path=path, textual=textual, has_location="content_location" in receipt,
        may_be_archived=may_be_archived, spooled=spooled,
    )
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
    def __init__(self, app: ASGIApp, *, budget: OutputBudget = STANDARD_BUDGET) -> None:
        self.app = app
        self.budget = budget

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith("/call/"):
            await self.app(scope, receive, send)
            return

        start: Message = {}
        mime = encoding = ""
        limit = self.budget.capture_bytes
        size = 0
        prefix = bytearray()
        tail = bytearray()
        textual = True
        native = False
        omitted_header = OMITTED_HEADER.encode("latin-1")
        tail_bytes = PREVIEW_BYTES + 3

        async def collect(message: Message) -> None:
            nonlocal start, mime, encoding, limit, size, textual, native
            if message["type"] == "http.response.start":
                start = message.copy()
                # Only this middleware can label a response as an internal omission receipt.
                start["headers"] = [(k, v) for k, v in message["headers"] if k.lower() != omitted_header]
                headers = Headers(raw=start["headers"])
                mime = content_mime(headers.get("content-type", ""))
                encoding = headers.get("content-encoding", "identity").strip().lower()
                success = 200 <= start["status"] < 300
                textual = _textual(mime)
                native = success and is_native_media(mime)
                if success and headers.get("x-treg-async") and textual:
                    limit = SUBMISSION_BYTES
                elif native:
                    limit = self.budget.media_bytes
                elif not textual:
                    limit = 0
                return
            if message["type"] != "http.response.body":
                await send(message)
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if limit and chunk:
                view = memoryview(chunk)
                if len(prefix) < limit:
                    prefix.extend(view[:limit - len(prefix)])
                if textual and encoding in ("", "identity"):
                    if len(view) >= tail_bytes:
                        tail[:] = view[-tail_bytes:]
                    else:
                        tail.extend(view)
                        del tail[:-tail_bytes]
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
            if not mime and (complete or encoding in ("", "identity")):
                try:
                    codecs.getincrementaldecoder("utf-8")().decode(body, final=complete)
                except UnicodeDecodeError:
                    textual = False
            if not complete or not (textual or native):
                body = jsontext.dumps_bytes(omitted_body(
                    size=size, mime=mime, status=status, path=path, textual=textual,
                    encoding=encoding, location=headers.get("content-location", ""),
                    head=prefix, tail=tail,
                ), separators=(",", ":"))
                content_type = b"application/json"
                start["headers"].append((omitted_header, b"1"))
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
