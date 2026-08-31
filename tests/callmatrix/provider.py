"""Programmable in-process provider used by the call state matrix."""

from __future__ import annotations

import asyncio
import gzip
import json
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import Response


@dataclass(frozen=True)
class Hit:
    """One request that reached the fake provider."""

    method: str
    host: str
    path: str
    raw_path: bytes
    query: tuple[tuple[str, str], ...]
    headers: dict[str, str]
    body: bytes


class FakeProvider:
    """ASGI provider whose response is selected with ``X-Fake-*`` headers."""

    def __init__(self) -> None:
        self.hits: list[Hit] = []
        self._arrived = asyncio.Event()
        self.app = FastAPI()
        self.app.add_api_route(
            "/{path:path}", self._respond,
            methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        )

    async def wait_for_hits(self, count: int, timeout: float = 30.0) -> None:
        """Block until ``count`` requests have arrived, or fail the wait.

        Callers racing an in-flight call must wait on this rather than yielding
        the event loop a fixed number of times: an idle loop returns from
        ``sleep(0)`` immediately, so a yield budget is spent at a rate set by
        the machine, not by the call's progress.
        """
        try:
            async with asyncio.timeout(timeout):
                while True:
                    self._arrived.clear()
                    if len(self.hits) >= count:
                        return
                    await self._arrived.wait()
        except TimeoutError:
            raise AssertionError(
                f"only {len(self.hits)} of {count} calls reached the fake provider "
                f"in {timeout}s",
            ) from None

    async def _respond(self, request: Request) -> Response:
        body = await request.body()
        headers = {key.lower(): value for key, value in request.headers.items()}
        self.hits.append(Hit(
            method=request.method,
            host=request.url.hostname or "",
            path=request.url.path,
            raw_path=request.scope.get("raw_path", b""),
            query=tuple(request.query_params.multi_items()),
            headers=headers,
            body=body,
        ))
        self._arrived.set()

        if delay := headers.get("x-fake-sleep"):
            await asyncio.sleep(float(delay))

        status = int(headers.get("x-fake-status", "200"))
        response_body = self._response_body(request, headers)
        response_headers: dict[str, str] = {}
        if cookie := headers.get("x-fake-set-cookie"):
            response_headers["Set-Cookie"] = cookie
        if headers.get("x-fake-gzip", "").lower() in {"1", "true", "yes"}:
            response_body = gzip.compress(response_body)
            response_headers["Content-Encoding"] = "gzip"
        return Response(
            content=response_body,
            status_code=status,
            media_type="application/json",
            headers=response_headers,
        )

    @staticmethod
    def _response_body(request: Request, headers: dict[str, str]) -> bytes:
        if "x-fake-body" in headers:
            return headers["x-fake-body"].encode()
        if "x-fake-cost" not in headers:
            return b"{}"

        cost = json.loads(headers["x-fake-cost"])
        host = request.url.hostname or ""
        if "dataforseo" in host:
            payload = {"cost": cost, "tasks": []}
        elif "scrapecreators" in host:
            payload = {"success": True, "credits_charged": cost}
        elif "akta" in host or "leadmagic" in host:
            payload = {"credits_consumed": cost}
        elif "lusha" in host:
            payload = {"billing": {"creditsCharged": cost}}
        else:
            payload = {"cost": cost}
        return json.dumps(payload, separators=(",", ":")).encode()
