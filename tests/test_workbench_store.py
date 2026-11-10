import asyncio
import base64
import json
import sqlite3

import httpx
import pytest

from treg.infra.workbench_store import SQLiteStore, StoreError, TursoStore


class PipelineServer:
    def __init__(self, path):
        self.path = path
        self.streams = {}
        self.calls = []
        self.authorizations = []
        self.sequence = 0
        self.commit_fault = None
        self.base_url = None

    def handle(self, request):
        body = json.loads(request.content)
        self.calls.append(body)
        authorization = request.headers["Authorization"]
        self.authorizations.append(authorization)
        assert request.url == "https://team.example/v2/pipeline"
        if authorization not in {"Bearer private-token", "Bearer readonly-token"}:
            return httpx.Response(401)
        baton = body["baton"]
        if baton is None:
            read_only = authorization == "Bearer readonly-token"
            path = self.path.as_uri() + "?mode=ro" if read_only else self.path
            connection = sqlite3.connect(path, uri=read_only, isolation_level=None)
        else:
            connection = self.streams.pop(baton)
        results = []
        committed = False
        closed = False
        for operation in body["requests"]:
            if operation["type"] == "close":
                connection.close()
                closed = True
                results.append({"type": "ok", "response": {"type": "close"}})
                continue
            statement = operation["stmt"]
            if statement["sql"].strip().upper().startswith("PRAGMA QUERY_ONLY") and "=" in statement["sql"]:
                results.append({"type": "error", "error": {"code": "SQL_PARSE_ERROR"}})
                continue
            params = []
            for value in statement["args"]:
                match value["type"]:
                    case "null":
                        params.append(None)
                    case "integer":
                        params.append(int(value["value"]))
                    case "float" | "text":
                        params.append(value["value"])
                    case "blob":
                        params.append(base64.b64decode(value["base64"]))
            try:
                cursor = connection.execute(statement["sql"], params)
                committed |= statement["sql"] == "COMMIT"
                rows = []
                for row in cursor.fetchall():
                    values = []
                    for value in row:
                        if value is None:
                            values.append({"type": "null"})
                        elif isinstance(value, bytes):
                            values.append({"type": "blob", "base64": base64.b64encode(value).decode().rstrip("=")})
                        elif isinstance(value, int):
                            values.append({"type": "integer", "value": str(value)})
                        elif isinstance(value, float):
                            values.append({"type": "float", "value": value})
                        else:
                            values.append({"type": "text", "value": value})
                    rows.append(values)
                result = {"cols": [{"name": col[0]} for col in cursor.description or ()],
                          "rows": rows, "affected_row_count": max(cursor.rowcount, 0)}
                results.append({"type": "ok", "response": {"type": "execute", "result": result}})
            except sqlite3.Error as exc:
                results.append({"type": "error", "error": {
                    "code": exc.sqlite_errorname, "message": "private SQL value private-token",
                }})
        if closed:
            next_baton = None
        else:
            self.sequence += 1
            next_baton = f"baton-{self.sequence}"
            self.streams[next_baton] = connection
        if committed and self.commit_fault == "timeout":
            raise httpx.ReadTimeout("private SQL value private-token", request=request)
        if committed and self.commit_fault == "http_error":
            return httpx.Response(502, text="private SQL value private-token")
        if committed and self.commit_fault == "malformed":
            return httpx.Response(200, json={"baton": None, "results": []})
        if committed and self.commit_fault == "malformed_error":
            results[0] = {"type": "error", "error": None}
        return httpx.Response(200, json={
            "baton": next_baton, "base_url": self.base_url, "results": results,
        })


@pytest.fixture(params=["sqlite", "http"])
def store(request, tmp_path):
    path = tmp_path / "team.db"
    if request.param == "sqlite":
        return SQLiteStore(path)
    server = PipelineServer(path)
    return TursoStore("libsql://team.example", "private-token", read_auth_token="readonly-token",
                      transport=httpx.MockTransport(server.handle))


async def test_parameterized_write_roundtrip_and_conditional_update(store):
    async with store.transaction() as tx:
        await tx.execute("CREATE TABLE leads (id INTEGER PRIMARY KEY, email TEXT, revision INTEGER, extra BLOB)")
        inserted = await tx.execute("INSERT INTO leads VALUES (?, ?, ?, ?)",
                                    (2**60, "a'); DROP TABLE leads; --", 1, b"\x00\xff"))
        assert inserted.rows_affected == 1
        stale = await tx.execute("UPDATE leads SET revision = revision + 1 WHERE id = ? AND revision = ?",
                                 (2**60, 0))
        assert stale.rows_affected == 0
        updated = await tx.execute("UPDATE leads SET revision = revision + 1 WHERE id = ? AND revision = ?",
                                   (2**60, 1))
        assert updated.rows_affected == 1
    rows = await store.read("SELECT * FROM leads WHERE id = ?", (2**60,))
    assert rows.rows == [{"id": 2**60, "email": "a'); DROP TABLE leads; --", "revision": 2, "extra": b"\x00\xff"}]


async def test_batch_failure_rolls_back_prior_rows(store):
    async with store.transaction() as tx:
        await tx.execute("CREATE TABLE leads (id INTEGER PRIMARY KEY, email TEXT)")
    with pytest.raises(StoreError, match="constraint_violation"):
        async with store.transaction() as tx:
            await tx.execute("INSERT INTO leads VALUES (?, ?)", (1, "first@example.com"))
            await tx.execute("INSERT INTO leads VALUES (?, ?)", (1, "duplicate@example.com"))
    assert (await store.read("SELECT * FROM leads")).rows == []


@pytest.mark.parametrize("value, error", [
    (1, "constraint_violation"),
    (2**63, "invalid_parameter"),
    (float("nan"), "invalid_parameter"),
])
async def test_suppressed_statement_error_cannot_commit_partial_batch(store, value, error):
    async with store.transaction() as tx:
        await tx.execute("CREATE TABLE leads (id INTEGER PRIMARY KEY)")
    with pytest.raises(StoreError, match="transaction_failed"):
        async with store.transaction() as tx:
            await tx.execute("INSERT INTO leads VALUES (?)", (1,))
            with pytest.raises(StoreError, match=error):
                await tx.execute("INSERT INTO leads VALUES (?)", (value,))
    assert (await store.read("SELECT * FROM leads")).rows == []


async def test_application_error_rolls_back_transaction(store):
    with pytest.raises(RuntimeError, match="abort batch"):
        async with store.transaction() as tx:
            await tx.execute("CREATE TABLE leads (id INTEGER PRIMARY KEY)")
            raise RuntimeError("abort batch")
    assert (await store.read("SELECT name FROM sqlite_master WHERE name = ?", ("leads",))).rows == []


async def test_cancellation_before_commit_rolls_back_transaction(store):
    with pytest.raises(asyncio.CancelledError):
        async with store.transaction() as tx:
            await tx.execute("CREATE TABLE leads (id INTEGER PRIMARY KEY)")
            raise asyncio.CancelledError
    assert tx.outcome_unknown is False
    assert (await store.read("SELECT name FROM sqlite_master WHERE name = ?", ("leads",))).rows == []


async def test_parameter_types_roundtrip(store):
    async with store.transaction() as tx:
        await tx.execute("CREATE TABLE parameters (value)")
    result = await store.read("SELECT ? AS a, ? AS b, ? AS c, ? AS d, ? AS e",
                              (None, True, False, -(2**63), 1.5))
    assert result.rows == [{"a": None, "b": 1, "c": 0, "d": -(2**63), "e": 1.5}]


async def test_read_cannot_mutate_database(store):
    async with store.transaction() as tx:
        await tx.execute("CREATE TABLE leads (id INTEGER PRIMARY KEY)")
    with pytest.raises(StoreError):
        await store.read("INSERT INTO leads VALUES (?)", (1,))
    assert (await store.read("SELECT * FROM leads")).rows == []


async def test_expired_transaction_rolls_back_without_committing(store):
    with pytest.raises(StoreError, match="transaction_expired"):
        async with store.transaction() as tx:
            await tx.execute("CREATE TABLE leads (id INTEGER PRIMARY KEY)")
            tx.deadline = 0
    assert (await store.read("SELECT name FROM sqlite_master WHERE name = ?", ("leads",))).rows == []


@pytest.mark.parametrize("fault", ["timeout", "http_error", "malformed", "malformed_error"])
async def test_commit_response_loss_is_unknown_and_never_retried(tmp_path, fault):
    path = tmp_path / "team.db"
    server = PipelineServer(path)
    store = TursoStore("https://team.example", "private-token", read_auth_token="readonly-token",
                       transport=httpx.MockTransport(server.handle))
    async with store.transaction() as tx:
        await tx.execute("CREATE TABLE leads (id INTEGER PRIMARY KEY, revision INTEGER)")
        await tx.execute("INSERT INTO leads VALUES (?, ?)", (1, 1))
    server.calls.clear()
    server.commit_fault = fault
    with pytest.raises(StoreError) as caught:
        async with store.transaction() as tx:
            await tx.execute("UPDATE leads SET revision = revision + 1 WHERE id = ?", (1,))
    assert caught.value.code == "outcome_unknown"
    assert tx.outcome_unknown is True
    assert str(caught.value) == "outcome_unknown"
    assert [call["requests"][0]["stmt"]["sql"] for call in server.calls] == [
        "BEGIN IMMEDIATE", "UPDATE leads SET revision = revision + 1 WHERE id = ?", "COMMIT",
    ]
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT revision FROM leads").fetchone() == (2,)


async def test_suppressed_malformed_response_cannot_commit_partial_batch(tmp_path):
    server = PipelineServer(tmp_path / "team.db")
    corrupt = False

    def respond(request):
        response = server.handle(request)
        if corrupt:
            payload = response.json()
            payload["results"][0] = {"type": "error", "error": None}
            return httpx.Response(200, json=payload)
        return response

    store = TursoStore("https://team.example", "private-token", read_auth_token="readonly-token",
                       transport=httpx.MockTransport(respond))
    async with store.transaction() as tx:
        await tx.execute("CREATE TABLE leads (id INTEGER PRIMARY KEY)")
    with pytest.raises(StoreError, match="transaction_failed"):
        async with store.transaction() as tx:
            await tx.execute("INSERT INTO leads VALUES (?)", (1,))
            corrupt = True
            with pytest.raises(Exception) as caught:
                await tx.execute("INSERT INTO leads VALUES (?)", (2,))
            corrupt = False
    assert isinstance(caught.value, StoreError)
    assert caught.value.code == "invalid_response"
    assert (await store.read("SELECT * FROM leads")).rows == []


@pytest.mark.parametrize("url", [
    "http://team.example", "https://user:secret@team.example", "https://team.example?token=secret",
    "https://team.example#secret", "https://team.example/path", "https://team.example:8443",
])
def test_rejects_unsafe_configured_url(url):
    with pytest.raises(StoreError, match="invalid_configuration"):
        TursoStore(url, "private-token", read_auth_token="readonly-token")


async def test_server_cannot_redirect_credentials(tmp_path):
    server = PipelineServer(tmp_path / "team.db")
    server.base_url = "https://other.example"
    store = TursoStore("https://team.example", "private-token", read_auth_token="readonly-token",
                       transport=httpx.MockTransport(server.handle))
    with pytest.raises(StoreError, match="invalid_response"):
        async with store.transaction():
            pytest.fail("untrusted base_url was accepted")
    assert len(server.calls) == 1
    for connection in server.streams.values():
        connection.close()


async def test_http_redirect_is_not_followed():
    calls = []

    def redirect(request):
        calls.append(request)
        return httpx.Response(307, headers={"Location": "https://other.example/v2/pipeline"})

    store = TursoStore("https://team.example", "private-token", read_auth_token="readonly-token",
                       transport=httpx.MockTransport(redirect))
    with pytest.raises(StoreError, match="unavailable"):
        await store.read("SELECT 1")
    assert len(calls) == 1


async def test_turso_read_credentials_are_isolated_from_write_transactions(tmp_path):
    server = PipelineServer(tmp_path / "team.db")
    store = TursoStore("https://team.example", "private-token", read_auth_token="readonly-token",
                       transport=httpx.MockTransport(server.handle))
    async with store.transaction() as tx:
        await tx.execute("CREATE TABLE leads (id INTEGER PRIMARY KEY, revision INTEGER)")
        await tx.execute("INSERT INTO leads VALUES (1, 1)")
    assert set(server.authorizations) == {"Bearer private-token"}
    server.authorizations.clear()

    assert (await store.read("SELECT revision FROM leads")).rows == [{"revision": 1}]
    with pytest.raises(StoreError):
        await store.read("UPDATE leads SET revision = 2 WHERE id = 1")
    assert (await store.read("SELECT revision FROM leads")).rows == [{"revision": 1}]
    assert server.authorizations == ["Bearer readonly-token"] * 3
    server.authorizations.clear()

    async with store.transaction() as tx:
        await tx.execute("UPDATE leads SET revision = 2 WHERE id = 1")
    assert set(server.authorizations) == {"Bearer private-token"}
    assert (await store.read("SELECT revision FROM leads")).rows == [{"revision": 2}]


async def test_rejected_read_token_never_falls_back_to_write_token(tmp_path):
    server = PipelineServer(tmp_path / "team.db")
    store = TursoStore("https://team.example", "private-token", read_auth_token="revoked-token",
                       transport=httpx.MockTransport(server.handle))
    with pytest.raises(StoreError, match="^unavailable$"):
        await store.read("SELECT 1")
    assert server.authorizations == ["Bearer revoked-token"]
    assert not server.path.exists()


@pytest.mark.parametrize("auth_token, read_auth_token", [
    ("private-token", ""), ("private-token", "private-token"), ("", "readonly-token"),
])
def test_turso_requires_distinct_nonempty_credentials(tmp_path, auth_token, read_auth_token):
    server = PipelineServer(tmp_path / "team.db")
    with pytest.raises(StoreError, match="^invalid_configuration$"):
        TursoStore("https://team.example", auth_token, read_auth_token=read_auth_token,
                   transport=httpx.MockTransport(server.handle))
    assert server.calls == []


def test_turso_requires_explicit_read_token(tmp_path):
    server = PipelineServer(tmp_path / "team.db")
    with pytest.raises(TypeError, match="read_auth_token"):
        TursoStore("https://team.example", "private-token", transport=httpx.MockTransport(server.handle))
    assert server.calls == []


@pytest.mark.parametrize("encoded, expected", [
    ("", b""), ("YWJj", b"abc"),
    ("YWI=", b"ab"), ("YWI", b"ab"),
    ("YQ==", b"a"), ("YQ", b"a"),
    ("AHNtb2tl/w", b"\x00smoke\xff"),
    ("A", None), ("AAAAA", None), ("YW@j", None), ("YW Jj", None),
])
async def test_turso_blob_response_decoding(encoded, expected):
    def respond(request):
        assert request.headers["Authorization"] == "Bearer readonly-token"
        return httpx.Response(200, json={"baton": None, "results": [
            {"type": "ok", "response": {"type": "execute", "result": {
                "cols": [{"name": "data"}],
                "rows": [[{"type": "blob", "base64": encoded}]], "affected_row_count": 0,
            }}},
            {"type": "ok", "response": {"type": "close"}},
        ]})

    store = TursoStore("https://team.example", "private-token", read_auth_token="readonly-token",
                       transport=httpx.MockTransport(respond))
    if expected is None:
        with pytest.raises(StoreError, match="^invalid_response$"):
            await store.read("SELECT data FROM leads")
    else:
        assert (await store.read("SELECT data FROM leads")).rows == [{"data": expected}]
