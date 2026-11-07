"""Server-rendered views. A request with `X-Treg-View: 1` to a route that has a view gets
`{"data": <the route's own JSON body>, "text": <what the CLI prints>}` with the route's status and
headers plus `X-Treg-View: 1`; every other request gets the route's answer untouched."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..domain.identity.access import Caller, require_member
from ..infra.db import session_maker

VIEW_HEADER = "X-Treg-View"


@dataclass(frozen=True)
class Answer:
    """A route's whole answer, and the request body it read."""

    request: Request
    status: int
    headers: Headers
    body: bytes
    sent: bytes

    def json(self) -> Any:
        return json.loads(self.body)

    def object(self) -> dict:
        body = self.json()
        return body if isinstance(body, dict) else {}


@dataclass(frozen=True)
class Rendered:
    """A view that answers in the route's place: `catalog get <job>` 404s as an endpoint."""

    text: str
    data: bytes
    status: int


Renderer = Callable[[Answer], Awaitable["str | Rendered"]]
_RENDERERS: dict[Callable, Renderer] = {}


def renders(endpoint: Callable) -> Callable[[Renderer], Renderer]:
    def register(render: Renderer) -> Renderer:
        _RENDERERS[endpoint] = render
        return render
    return register


def wants_view(scope: Scope) -> bool:
    return scope["type"] == "http" and scope["method"] != "HEAD" and Headers(scope=scope).get(VIEW_HEADER) == "1"


def wrap(endpoint: Callable, app: ASGIApp) -> ASGIApp:
    """`app` (a route's ASGI app) answering with its view when one is asked for."""
    render = _RENDERERS.get(endpoint)
    if render is None:
        return app

    async def view_app(scope: Scope, receive: Receive, send: Send) -> None:
        if not wants_view(scope):
            await app(scope, receive, send)
            return
        sent = bytearray()

        async def read() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                sent.extend(message.get("body", b""))
            return message

        start: Message = {}
        chunks: list[bytes] = []

        async def capture(message: Message) -> None:
            if message["type"] == "http.response.start":
                start.update(message)
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        await app(scope, read, capture)
        raw = start.get("headers", [])
        answer = Answer(Request(scope), start["status"], Headers(raw=raw), b"".join(chunks), bytes(sent))
        if not _is_json(answer):
            await _send(send, answer.status, raw, answer.body)
            return
        try:
            rendered = await render(answer)
        except Exception:
            logging.getLogger("treg.view").exception("view failed to render; answering without it")
            await _send(send, answer.status, raw, answer.body)
            return
        if isinstance(rendered, Rendered):
            text, data, status = rendered.text, rendered.data, rendered.status
        else:
            text, data, status = rendered, answer.body, answer.status
        envelope = b'{"data":' + data + b',"text":' + json.dumps(text, ensure_ascii=False).encode() + b"}"
        kept = [(k, v) for k, v in raw if k.lower() not in (b"content-length", b"content-type")]
        await _send(send, status, [*kept, (b"content-type", b"application/json"),
                                   (VIEW_HEADER.lower().encode(), b"1")], envelope)

    return view_app


def _is_json(answer: Answer) -> bool:
    if "json" not in answer.headers.get("content-type", ""):
        return False
    try:
        answer.json()
    except ValueError:
        return False
    return True


async def _send(send: Send, status: int, headers: list[tuple[bytes, bytes]], body: bytes) -> None:
    headers = [(k, v) for k, v in headers if k.lower() != b"content-length"]
    await send({"type": "http.response.start", "status": status,
                "headers": [*headers, (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


async def get(request: Request, path: str, query: str = "") -> tuple[int, bytes]:
    """GET `path` from this app's router as the same caller, without a view: what the Python CLI
    asked for in a second request."""
    scope = {k: v for k, v in request.scope.items() if k not in ("endpoint", "route", "path_params")}
    raw_path = quote(path)
    scope |= {"method": "GET", "path": path, "raw_path": raw_path.encode(), "query_string": query.encode(),
              "headers": [(k, v) for k, v in request.scope["headers"]
                          if k.lower() not in (VIEW_HEADER.lower().encode(), b"content-length", b"content-type")]}

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    start: Message = {}
    chunks: list[bytes] = []

    async def capture(message: Message) -> None:
        if message["type"] == "http.response.start":
            start.update(message)
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await request.scope["router"](scope, receive, capture)
    return start["status"], b"".join(chunks)


async def with_caller(request: Request, read: Callable[[AsyncSession, Caller], Awaitable[Any]]) -> Any:
    """`read(db, caller)` in a short session for the caller the request names, None without one.
    It runs after the route's own answer is complete, so the request holds one session at a time."""
    async with session_maker() as db:
        caller = await optional_caller(request, db)
        return None if caller is None else await read(db, caller)


async def caller_of(request: Request) -> Caller | None:
    async def itself(db: AsyncSession, caller: Caller) -> Caller:
        return caller
    return await with_caller(request, itself)


async def optional_caller(request: Request, db: AsyncSession) -> Caller | None:
    """Who is asking, when the request says and the registry accepts it; views of open routes
    add what only this caller may see."""
    token = request.headers.get("x-treg-token", "")
    authorization = request.headers.get("authorization", "")
    if not token and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    cookie = request.cookies.get("treg_session", "")
    if not token and not cookie:
        return None
    try:
        return await require_member(request, x_treg_token=token, x_treg_org=request.headers.get("x-treg-org", ""),
                                    treg_session=cookie, db=db)
    except HTTPException:
        return None


def credentials_sent(request: Request) -> bool:
    return bool(request.headers.get("x-treg-token") or request.headers.get("authorization")
                or request.cookies.get("treg_session"))


async def quietly(read: Awaitable, default: Any = None) -> Any:
    """An enrichment of a view: its value, or `default` when it fails, so the page still renders."""
    try:
        return await read
    except Exception:
        logging.getLogger("treg.view").warning("view enrichment failed", exc_info=True)
        return default
