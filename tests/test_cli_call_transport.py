"""`treg call` transport errors are a compact recovery refusal, not a Python traceback."""
import json
import socket
import threading

import httpx
import pytest

from treg import cli


def _incomplete_http(extra_headers: dict[str, str] | None = None) -> bytes:
    extra = "".join(f"{name}: {value}\r\n" for name, value in (extra_headers or {}).items())
    return (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: 100\r\n"
        f"{extra}"
        "\r\n"
        "{"
    ).encode()


def _serve_once(payload: bytes) -> int:
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def run() -> None:
        try:
            conn, _ = server.accept()
            try:
                conn.recv(65536)
                conn.sendall(payload)
            finally:
                conn.close()
        finally:
            server.close()

    threading.Thread(target=run, daemon=True).start()
    return port


def _point_at(monkeypatch, port: int) -> None:
    base = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("TREG_URL", base)
    monkeypatch.setenv("TREG_TOKEN", "dummy")
    monkeypatch.setenv("TREG_TELEMETRY", "0")
    monkeypatch.setattr(cli, "_load_config", lambda: {"token": "dummy", "base_url": base})


def _raising_client(exc: BaseException):
    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, url, params=None, content=None, headers=None):
            raise exc

    return FakeClient()


def _json_call(monkeypatch, argv=None):
    monkeypatch.setattr(cli, "_load_config", lambda: {"token": "t", "base_url": "http://x"})
    with pytest.raises(SystemExit) as caught:
        cli.main(argv or ["--json", "call", "example.tool"])
    return caught.value.code


def test_json_call_connection_reset_prints_one_error_envelope(monkeypatch, capsys):
    """The public repro: a registry that starts a 200 and then drops the body."""
    _point_at(monkeypatch, _serve_once(_incomplete_http()))
    with pytest.raises(SystemExit) as caught:
        cli.main(["--json", "call", "crawl4ai.web.scrape", "--data", '{"url":"https://example.com"}'])
    assert caught.value.code == 1
    out, err = capsys.readouterr()
    assert err == ""
    assert out.count("\n") == 1
    assert "Traceback" not in out
    body = json.loads(out)
    assert body["error"] == "network_error"
    assert "call_id" not in body
    assert "charged_micro" not in body
    assert "RemoteProtocolError" in body["message"]
    assert "unknown" in body["message"]
    assert "treg balance" in body["message"]
    assert "treg calls" in body["message"]
    assert body["help"] == ["treg balance", "treg calls"]


def test_call_connection_reset_is_compact_not_a_traceback(monkeypatch, capsys):
    _point_at(monkeypatch, _serve_once(_incomplete_http()))
    with pytest.raises(SystemExit) as caught:
        cli.main(["call", "crawl4ai.web.scrape", "--data", '{"url":"https://example.com"}'])
    assert caught.value.code == 1
    out, err = capsys.readouterr()
    text = out + err
    assert "Traceback" not in text
    assert "network_error" in text
    assert "treg balance" in text
    assert "treg calls" in text


@pytest.mark.parametrize("factory,kind", [
    (lambda req: httpx.RemoteProtocolError("peer closed", request=req), "RemoteProtocolError"),
    (lambda req: httpx.ReadTimeout("private transport details", request=req), "ReadTimeout"),
    (lambda req: httpx.ConnectError("private transport details", request=req), "ConnectError"),
    (lambda req: httpx.ConnectTimeout("private transport details", request=req), "ConnectTimeout"),
    (lambda req: OSError("private transport details"), "OSError"),
])
def test_json_call_transport_errors_are_envelopes_without_leaking_details(
        monkeypatch, capsys, factory, kind):
    req = httpx.Request("POST", "http://x/call/example.tool")
    monkeypatch.setattr(cli, "_client", lambda cfg, **k: _raising_client(factory(req)))
    assert _json_call(monkeypatch) == 1
    out, err = capsys.readouterr()
    assert err == ""
    body = json.loads(out)
    assert body["error"] == "network_error"
    assert kind in body["message"]
    assert "private transport details" not in out
    assert "unknown" in body["message"]
    assert "treg balance" in body["message"]


def test_partial_response_headers_are_reported_when_present(monkeypatch, capsys):
    req = httpx.Request("POST", "http://x/call/example.tool")
    exc = httpx.RemoteProtocolError("peer closed", request=req)
    exc.response = httpx.Response(
        200, headers={"X-Treg-Call-Id": "abc123", "X-Treg-Cost-Micro": "2500"}, request=req)
    monkeypatch.setattr(cli, "_client", lambda cfg, **k: _raising_client(exc))
    assert _json_call(monkeypatch) == 1
    body = json.loads(capsys.readouterr().out)
    assert body["error"] == "network_error"
    assert body["call_id"] == "abc123"
    assert body["charged_micro"] == 2500
    assert "call id abc123" in body["message"]
    assert "charged $0.0025" in body["message"]
    assert "might or might not repeat a charge" in body["message"]


def test_async_partial_response_reports_a_reservation_not_a_charge(monkeypatch, capsys):
    req = httpx.Request("POST", "http://x/call/example.tool")
    exc = httpx.RemoteProtocolError("peer closed", request=req)
    exc.response = httpx.Response(
        202, headers={"X-Treg-Call-Id": "abc123", "X-Treg-Cost-Micro": "98000",
                       "X-Treg-Async": "{}"}, request=req)
    monkeypatch.setattr(cli, "_client", lambda cfg, **k: _raising_client(exc))
    assert _json_call(monkeypatch) == 1
    body = json.loads(capsys.readouterr().out)
    assert body["call_id"] == "abc123"
    assert body["reserved_micro"] == 98000
    assert "charged_micro" not in body
    assert "reserved $0.098" in body["message"]
