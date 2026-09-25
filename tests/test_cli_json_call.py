"""`treg --json call`: stdout is one JSON line a script can parse, and stderr stays silent, so a
caller that merges the two streams still parses every result. Driven through `main()`."""
import json

import httpx
import pytest

from treg import cli


def _serve(monkeypatch, response: httpx.Response):
    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, url, params=None, content=None, headers=None):
            return response

    monkeypatch.setattr(cli, "_client", lambda cfg, **k: FakeClient())
    monkeypatch.setattr(cli, "_load_config", lambda: {"token": "t", "base_url": "http://x"})


def _charged(status=200, **kw):
    return httpx.Response(status, headers={"X-Treg-Cost-Micro": "98000", "X-Treg-Call-Id": "abc123",
                                           "X-Treg-Hint": "review", **kw.pop("headers", {})}, **kw)


def test_json_call_prints_one_envelope_and_nothing_on_stderr(monkeypatch, capsys):
    _serve(monkeypatch, _charged(json={"items": [{"name": "A"}]}))
    cli.main(["--json", "call", "treg.people.search", "--data", '{"title":"CEO"}'])
    out, err = capsys.readouterr()
    assert err == ""
    assert out.count("\n") == 1
    assert json.loads(out) == {"result": {"items": [{"name": "A"}]}, "_treg": {
        "http_status": 200, "call_id": "abc123", "charged_micro": 98000, "hint": "review"}}


def test_json_call_error_is_still_one_envelope_and_exits_nonzero(monkeypatch, capsys):
    _serve(monkeypatch, _charged(402, json={"detail": {"error": "insufficient_balance"}}))
    with pytest.raises(SystemExit) as exc:
        cli.main(["call", "treg.people.search", "--json"])
    assert exc.value.code == 1
    out, err = capsys.readouterr()
    assert err == "" and json.loads(out)["_treg"]["http_status"] == 402
    assert json.loads(out)["result"] == {"detail": {"error": "insufficient_balance"}}


def test_json_call_carries_text_and_binary_bodies(monkeypatch, capsys):
    _serve(monkeypatch, httpx.Response(200, headers={"content-type": "text/csv"}, text="a,b\n1,2\n"))
    cli.main(["--json", "call", "x.csv"])
    assert json.loads(capsys.readouterr().out)["result"] == "a,b\n1,2\n"
    _serve(monkeypatch, httpx.Response(200, headers={"content-type": "image/png"}, content=b"\x89PNG"))
    cli.main(["--json", "call", "x.png"])
    assert json.loads(capsys.readouterr().out)["result"] == {"base64": "iVBORw==", "content_type": "image/png"}


def test_async_submission_reports_a_reservation_not_a_charge(monkeypatch, capsys):
    _serve(monkeypatch, _charged(202, json={"id": "t1"}, headers={"X-Treg-Async": "{}"}))
    cli.main(["--json", "call", "x.submit"])
    meta = json.loads(capsys.readouterr().out)["_treg"]
    assert meta["reserved_micro"] == 98000 and meta["async"] is True and "charged_micro" not in meta


def test_default_call_output_is_unchanged(monkeypatch, capsys):
    _serve(monkeypatch, _charged(json={"items": []}))
    cli.main(["call", "treg.people.search"])
    out, err = capsys.readouterr()
    assert json.loads(out) == {"items": []}
    assert "treg: charged $0.098" in err and "abc123" in err
