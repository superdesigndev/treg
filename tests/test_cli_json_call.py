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


def test_json_and_org_flags_last_one_invocation(monkeypatch, capsys):
    """`--json` and `--org` are per-invocation: a later `main()` in the same process, or a command
    called directly, starts from plain output and the saved team, even after an error exit."""
    _serve(monkeypatch, _charged(402, json={"detail": {"error": "insufficient_balance"}}))
    with pytest.raises(SystemExit):
        cli.main(["--json", "--org", "acme", "call", "treg.people.search"])
    assert cli._JSON_OVERRIDE is False and cli._ORG_OVERRIDE is None
    capsys.readouterr()
    _serve(monkeypatch, _charged(json={"items": []}))
    cli.main(["call", "treg.people.search"])
    assert json.loads(capsys.readouterr().out) == {"items": []}



def _refused(kind, **headers):
    return httpx.Response(402, headers={"X-Treg-Error": "1", **headers}, json={"detail": {"error": kind}})


@pytest.mark.parametrize("response, code", [
    (_refused("insufficient_balance"), 255),
    (_refused("route_max_cost"), 1),  # refuses this call only
    (_refused("tag_blocked"), 1),  # the next item may carry another tag
    (_refused("insufficient_balance", **{"X-Treg-Idempotent-Replay": "1"}), 1),  # a stored answer
    (httpx.Response(402, json={"detail": {"error": "insufficient_balance"}}), 1),  # the provider's
])
def test_only_a_refusal_for_the_teams_money_exits_255(monkeypatch, capsys, response, code):
    _serve(monkeypatch, response)
    for argv in (["call", "treg.people.search"], ["--json", "call", "treg.people.search"]):
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
        assert exc.value.code == code
    envelope = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert envelope["_treg"].get("stop") is (True if code == 255 else None)
