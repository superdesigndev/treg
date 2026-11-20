from __future__ import annotations

import asyncio
import base64
import math
import sqlite3
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import aiosqlite
import httpx

SqlValue = None | bool | int | float | str | bytes
_COMMIT_WINDOW = 0.5


@dataclass(frozen=True)
class StoreResult:
    rows: list[dict[str, SqlValue]]
    rows_affected: int


class StoreError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _http_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        if (
            parts.scheme not in {"https", "libsql", "turso"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or parts.port not in {None, 443}
            or parts.path not in {"", "/"}
        ):
            raise ValueError
        host = parts.hostname.encode("idna").decode("ascii").lower()
        netloc = f"[{host}]" if ":" in host else host
        return urlunsplit(("https", netloc, "", "", ""))
    except ValueError:
        raise StoreError("invalid_configuration") from None


def _encode(value: SqlValue) -> dict:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, int) and -(2**63) <= value < 2**63:
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float) and math.isfinite(value):
        return {"type": "float", "value": value}
    if isinstance(value, str):
        return {"type": "text", "value": value}
    if isinstance(value, bytes):
        return {"type": "blob", "base64": base64.b64encode(value).decode("ascii")}
    raise StoreError("invalid_parameter")


def _decode(value: dict) -> SqlValue:
    match value["type"]:
        case "null":
            return None
        case "integer":
            return int(value["value"])
        case "float":
            return float(value["value"])
        case "text":
            return str(value["value"])
        case "blob":
            encoded = value["base64"]
            return base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=True)
        case _:
            raise ValueError


def _result(response: dict) -> StoreResult:
    result = response["result"]
    names = [col["name"] for col in result["cols"]]
    rows = [dict(zip(names, map(_decode, row), strict=True)) for row in result["rows"]]
    return StoreResult(rows, int(result["affected_row_count"]))


def _statement(sql: str, params: Sequence[SqlValue]) -> dict:
    return {"type": "execute", "stmt": {
        "sql": sql, "args": [_encode(value) for value in params], "want_rows": True,
    }}


def _statement_error(code: str) -> StoreError:
    if code.startswith("SQLITE_CONSTRAINT"):
        return StoreError("constraint_violation")
    if code in {"TRANSACTION_TIMEOUT", "TRANSACTION_CLOSED", "STREAM_EXPIRED"}:
        return StoreError("transaction_expired")
    if code in {"SQLITE_BUSY", "SQLITE_LOCKED", "SQLITE_CANTOPEN"}:
        return StoreError("unavailable")
    return StoreError("transaction_failed")


class TursoStore:
    def __init__(self, url: str, auth_token: str, *, read_auth_token: str,
                 transport: httpx.AsyncBaseTransport | None = None,
                 transaction_timeout: float = 4.0):
        self._url = _http_url(url)
        if (not auth_token or not read_auth_token or read_auth_token == auth_token
                or not _COMMIT_WINDOW < transaction_timeout < 5):
            raise StoreError("invalid_configuration")
        self._auth_token = auth_token
        self._read_auth_token = read_auth_token
        self._transport = transport
        self._transaction_timeout = transaction_timeout

    def _client(self, *, read_only: bool = False) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={"Authorization": f"Bearer {self._read_auth_token if read_only else self._auth_token}"},
            transport=self._transport, follow_redirects=False, trust_env=False,
        )

    async def read(self, sql: str, params: Sequence[SqlValue] = ()) -> StoreResult:
        async with self._client(read_only=True) as client:
            tx = _TursoTransaction(client, self._url, self._transaction_timeout)
            responses = await tx._request([
                _statement(sql, params), {"type": "close"},
            ], closing=True)
            return _result(responses[0])

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[_TursoTransaction]:
        async with self._client() as client:
            tx = _TursoTransaction(client, self._url, self._transaction_timeout)
            try:
                await tx.execute("BEGIN IMMEDIATE")
                yield tx
                if tx.failed:
                    raise StoreError("transaction_failed")
                await tx._request([_statement("COMMIT", ()), {"type": "close"}],
                                  closing=True, committing=True)
            except BaseException:
                await tx.rollback()
                raise
            finally:
                tx.closed = True


class _TursoTransaction:
    def __init__(self, client: httpx.AsyncClient, url: str, timeout: float):
        self.client = client
        self.url = url
        self.baton: str | None = None
        self.deadline = time.monotonic() + timeout
        self.closed = False
        self.failed = False
        self.outcome_unknown = False
        self.lock = asyncio.Lock()

    async def execute(self, sql: str, params: Sequence[SqlValue] = ()) -> StoreResult:
        async with self.lock:
            try:
                responses = await self._request([_statement(sql, params)])
                return _result(responses[0])
            except StoreError:
                self.failed = True
                raise
            except (KeyError, TypeError, ValueError):
                self.failed = True
                raise StoreError("invalid_response") from None

    async def _request(self, requests: list[dict], *, closing: bool = False,
                       committing: bool = False, cleanup: bool = False) -> list[dict]:
        if not cleanup and (self.closed or self.failed):
            raise StoreError("transaction_failed")
        remaining = self.deadline - time.monotonic()
        if not cleanup and (remaining <= 0 or committing and remaining < _COMMIT_WINDOW):
            self.failed = True
            raise StoreError("transaction_expired")
        statement_error = None
        try:
            async with asyncio.timeout(1.0 if cleanup else remaining):
                response = await self.client.post(
                    self.url + "/v2/pipeline",
                    json={"baton": self.baton, "requests": requests},
                    timeout=1.0 if cleanup else remaining,
                )
            if not response.is_success:
                raise StoreError("unavailable")
            payload = response.json()
            next_url = payload.get("base_url")
            if next_url is not None and _http_url(next_url) != self.url:
                raise StoreError("invalid_response")
            self.baton = payload["baton"]
            if not closing and (not isinstance(self.baton, str) or not self.baton):
                raise StoreError("transaction_expired")
            results = payload["results"]
            expected = requests[:1] if committing else requests
            if committing:
                # COMMIT's result is authoritative; close is best-effort cleanup.
                results = results[:1]
            if len(results) != len(expected):
                raise StoreError("invalid_response")
            responses = []
            for result, request in zip(results, expected, strict=True):
                if result["type"] == "error":
                    statement_error = _statement_error(str(result["error"].get("code", "")))
                    break
                item = result["response"]
                if result["type"] != "ok" or item["type"] != request["type"]:
                    raise StoreError("invalid_response")
                if item["type"] == "execute":
                    _result(item)
                responses.append(item)
            self.closed = closing and self.baton is None
        except (httpx.HTTPError, TimeoutError, StoreError, KeyError, TypeError,
                ValueError, AttributeError, OverflowError) as exc:
            self.failed = True
            if committing:
                self.baton = None
                self.outcome_unknown = True
                raise StoreError("outcome_unknown") from None
            if isinstance(exc, StoreError):
                raise
            code = "unavailable" if isinstance(exc, (httpx.HTTPError, TimeoutError)) else "invalid_response"
            raise StoreError(code) from None
        except asyncio.CancelledError:
            self.failed = True
            if committing:
                self.baton = None
                self.outcome_unknown = True
            raise
        if statement_error is not None:
            self.failed = True
            raise statement_error
        return responses

    async def rollback(self) -> None:
        if self.baton is not None and not self.closed:
            with suppress(StoreError):
                await self._request([_statement("ROLLBACK", ()), {"type": "close"}],
                                    closing=True, cleanup=True)


class SQLiteStore:
    def __init__(self, path: str | Path, *, transaction_timeout: float = 4.0):
        if not _COMMIT_WINDOW < transaction_timeout < 5:
            raise StoreError("invalid_configuration")
        self._path = str(path)
        self._transaction_timeout = transaction_timeout

    @asynccontextmanager
    async def _connection(self, *, read_only: bool = False) -> AsyncIterator[aiosqlite.Connection]:
        path = Path(self._path).absolute().as_uri() + "?mode=ro" if read_only else self._path
        try:
            async with aiosqlite.connect(path, uri=read_only, isolation_level=None,
                                        timeout=self._transaction_timeout) as connection:
                yield connection
        except sqlite3.Error as exc:
            raise _statement_error(getattr(exc, "sqlite_errorname", "")) from None

    async def read(self, sql: str, params: Sequence[SqlValue] = ()) -> StoreResult:
        async with self._connection(read_only=True) as connection:
            await connection.execute("PRAGMA query_only = ON")
            tx = _SQLiteTransaction(connection, self._transaction_timeout)
            return await tx.execute(sql, params)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[_SQLiteTransaction]:
        async with self._connection() as connection:
            tx = _SQLiteTransaction(connection, self._transaction_timeout)
            try:
                await tx.execute("BEGIN IMMEDIATE")
                yield tx
                if tx.failed:
                    raise StoreError("transaction_failed")
                await tx.execute("COMMIT", _committing=True)
            except BaseException:
                with suppress(sqlite3.Error):
                    await connection.rollback()
                raise
            finally:
                tx.closed = True


class _SQLiteTransaction:
    def __init__(self, connection: aiosqlite.Connection, timeout: float):
        self.connection = connection
        self.deadline = time.monotonic() + timeout
        self.closed = False
        self.failed = False
        self.outcome_unknown = False

    async def execute(self, sql: str, params: Sequence[SqlValue] = (), *,
                      _committing: bool = False) -> StoreResult:
        if self.closed or self.failed:
            raise StoreError("transaction_failed")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or _committing and remaining < _COMMIT_WINDOW:
            self.failed = True
            raise StoreError("transaction_expired")
        try:
            for value in params:
                _encode(value)
            async with asyncio.timeout(remaining):
                self.connection.row_factory = sqlite3.Row
                async with self.connection.execute(sql, params) as cursor:
                    rows = [dict(row) for row in await cursor.fetchall()]
                    return StoreResult(rows, max(cursor.rowcount, 0))
        except StoreError:
            self.failed = True
            raise
        except sqlite3.Error as exc:
            self.failed = True
            raise _statement_error(getattr(exc, "sqlite_errorname", "")) from None
        except (TimeoutError, asyncio.CancelledError) as exc:
            self.failed = True
            if _committing:
                self.outcome_unknown = True
            if isinstance(exc, TimeoutError):
                raise StoreError("outcome_unknown" if _committing else "transaction_expired") from None
            raise
