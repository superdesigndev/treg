"""Transport-level failures for the in-process fake provider."""

from __future__ import annotations

import httpx


class _InterruptedStream(httpx.AsyncByteStream):
    def __init__(self, inner: httpx.AsyncByteStream, request: httpx.Request) -> None:
        self._inner = inner
        self._request = request

    async def __aiter__(self):
        async for chunk in self._inner:
            if chunk:
                yield chunk
                break
        raise httpx.ReadError("fake response stream interrupted", request=self._request)

    async def aclose(self) -> None:
        await self._inner.aclose()


class FaultTransport(httpx.AsyncBaseTransport):
    """Wrap an ASGI transport and inject failures selected by ``X-Fake-Net``."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        fault = request.headers.get("X-Fake-Net", "").lower()
        if fault in {"timeout", "read-timeout"}:
            raise httpx.ReadTimeout("fake upstream timeout", request=request)
        if fault in {"connect", "connect-error"}:
            raise httpx.ConnectError("fake upstream connection failure", request=request)

        response = await self._inner.handle_async_request(request)
        if fault in {"stream", "read-error"}:
            response.stream = _InterruptedStream(response.stream, request)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()
