"""`treg call` prints the same answer, stderr lines and exit status from the Go CLI as from the
Python CLI, and sends the registry the same request."""
from __future__ import annotations

import email.parser
import email.policy
import json

import pytest

from .cli_go_harness import PYTHON_CLI, Canned, normalize, run, same_stdout, write_config

ASYNC = json.dumps({"id_from": "id", "poll": {"endpoint": "x.poll", "param": "id"},
                    "status": {"path": "status", "success": ["done"], "failure": ["failed"]},
                    "result": {"path": "result"}})

SCENARIOS = {
    "json_ok": [Canned(200, {"X-Treg-Call-Id": "c1", "X-Treg-Cost-Micro": "2500", "X-Treg-Hint": "review"},
                       json.dumps({"a": 1, "b": [1.5, 2], "t": "中文 <&>"}, ensure_ascii=False).encode())],
    "text_ok": [Canned(200, {"Content-Type": "text/plain; charset=utf-8"}, "hello\nwörld".encode())],
    "text_json": [Canned(200, {"Content-Type": "text/plain"}, b'{"looks":"json"}')],
    "binary_ok": [Canned(200, {"Content-Type": "image/png", "X-Treg-Cost-Micro": "1000000"}, b"\x89PNG\r\n\x1a\n\x00\x01")],
    "empty_204": [Canned(204, {}, b"")],
    "relayed_404": [Canned(404, {"X-Treg-Call-Id": "c4"}, b'{"error":"nope"}')],
    "relayed_binary_500": [Canned(500, {"Content-Type": "application/octet-stream"}, b"\x00\x01")],
    "treg_refusal": [Canned(403, {"X-Treg-Error": "1"}, b'{"detail":"this tool is denied for the team"}')],
    "saturated": [Canned(503, {"Retry-After": "5"}, b'{"treg_saturated":true,"detail":"busy"}')],
    "saturated_no_wait": [Canned(503, {}, b'{"treg_saturated":true}')],
    "capacity": [Canned(502, {"X-Treg-Capacity": "exhausted", "X-Treg-Cost-Micro": "0", "X-Treg-Call-Id": "c7"},
                        b'{"error":"out of credit"}')],
    "capacity_ok": [Canned(200, {"X-Treg-Capacity": "exhausted", "X-Treg-Cost-Micro": "0"}, b"{}")],
    "balance": [Canned(402, {"X-Treg-Error": "1"},
                       b'{"detail":{"error":"insufficient_balance","message":"top up","balance_micro":0}}')],
    "provider_402": [Canned(402, {}, b'{"detail":{"error":"insufficient_balance"}}')],
    "choose_org": [Canned(400, {"X-Treg-Error": "1"}, b'{"detail":"choose an org (send X-Treg-Org)"}')],
    "replay": [Canned(200, {"X-Treg-Idempotent-Replay": "1", "X-Treg-Original-Cost-Micro": "1000",
                            "X-Treg-Cost-Micro": "0", "X-Treg-Call-Id": "c9"}, b'{"ok":true}')],
    "async_submit": [Canned(200, {"X-Treg-Async": ASYNC, "X-Treg-Cost-Micro": "50000", "X-Treg-Call-Id": "c10"},
                            b'{"id":"task1"}')],
    "async_replay": [Canned(200, {"X-Treg-Async": ASYNC, "X-Treg-Cost-Micro": "0", "X-Treg-Idempotent-Replay": "1",
                                  "X-Treg-Original-Cost-Micro": "50000"}, b'{"id":"task1"}')],
    "upstream_failed": [Canned(200, {"X-Treg-Upstream-Failed": "status=failed", "X-Treg-Cost-Micro": "100"}, b'{"status":"failed"}')],
    "routed": [Canned(200, {"X-Treg-Routed-Tool": "treg.people.email.find", "X-Treg-Call-Id": "c11",
                            "X-Treg-Hint": "feedback"}, b'{"email":"a@b.c"}')],
    "old_review": [Canned(200, {"X-Treg-Review": "requested", "X-Treg-Call-Id": "c12"}, b"[]")],
    "large": [Canned(200, {}, json.dumps({"rows": ["x" * 100] * 400}).encode())],
    "notice": [Canned(200, {"X-Treg-Client-Notice": "treg 9.9.9 is available (you have 0.1). Run `treg update`."}, b"{}")],
    "unicode_escape": [Canned(200, {}, b'{"s":"caf\\u00e9 \\ud83d\\ude00","n":1.10,"big":12345678901234567890}')],
    "not_json": [Canned(200, {}, b"<html>not json</html>")],
    "bom_json": [Canned(200, {}, b'\xef\xbb\xbf{"bom":true}')],
}


def _homes(tmp_path, base: str):
    homes = []
    for name in ("py", "go"):
        home = tmp_path / name
        write_config(home, base_url=base, token="tok-1", active_org="acme", email="a@b.c", identity=False)
        homes.append(home)
    return homes


def _both(fake, tmp_path, go_bin, argv, *, env=None, stdin=None):
    py_home, go_home = _homes(tmp_path, fake.url)
    start = len(fake.seen)
    py = run(PYTHON_CLI + argv, home=py_home, env=env, stdin=stdin)
    middle = len(fake.seen)
    go = run([str(go_bin)] + argv, home=go_home, env=env, stdin=stdin)
    return py, go, fake.seen[start:middle], fake.seen[middle:]


def _assert_same(py, go):
    assert py.code == go.code, (py, go)
    assert same_stdout(py, go), (py.out, go.out)
    assert normalize(py.err) == normalize(go.err), (py.err, go.err)


IGNORED = {"user-agent", "accept-encoding", "connection", "host", "x-treg-client-version",
           "x-treg-session", "x-treg-agent"}


def _assert_same_request(py_seen, go_seen):
    assert len(py_seen) == len(go_seen)
    for a, b in zip(py_seen, go_seen):
        assert (a.method, a.path) == (b.method, b.path)
        if (a.headers.get("content-type") or "").startswith("multipart/form-data"):
            assert _parts(a) == _parts(b)
        else:
            assert a.body == b.body
            assert {k: v for k, v in a.headers.items() if k not in IGNORED} == \
                   {k: v for k, v in b.headers.items() if k not in IGNORED}


def _parts(seen):
    raw = b"Content-Type: " + seen.headers["content-type"].encode() + b"\r\n\r\n" + seen.body
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(raw)
    return sorted((p.get_param("name", header="content-disposition"), p.get_filename(),
                   p.get_payload(decode=True)) for p in msg.iter_parts())


@pytest.mark.parametrize("json_flag", [False, True])
@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_call_output(fake, tmp_path, go_bin, name, json_flag):
    fake.route(f"/call/{name}", *SCENARIOS[name])
    argv = (["--json"] if json_flag else []) + ["call", name]
    py, go, py_seen, go_seen = _both(fake, tmp_path, go_bin, argv)
    _assert_same(py, go)
    _assert_same_request(py_seen, go_seen)


def test_waf_retry(fake, tmp_path, go_bin):
    blocked = Canned(403, {"Content-Type": "text/html"}, b"<html>blocked</html>")
    ok = Canned(200, {}, b'{"ok":1}')
    fake.route("/call/waf", blocked, ok, blocked, ok)
    py, go, py_seen, go_seen = _both(fake, tmp_path, go_bin, ["call", "waf", "--data", "SELECT * FROM t"])
    _assert_same(py, go)
    _assert_same_request(py_seen, go_seen)
    assert go_seen[1].headers["x-treg-body-encoding"] == "base64"


@pytest.mark.parametrize("argv", [
    ["call", "enc", "a b/{x}/ü/%41/q!*'()|^?inline=1&flag&e=", "-p", "k=a b", "-p", "k=c/d?&=+é~*"],
    ["call", "enc", "./x/../../y", "--query", "z=1"],
    ["call", "https://api.example.com/v1/items/", "--method", "DELETE"],
    ["call", "enc", "--data", '{"a": 1}'],
    ["call", "enc", "--data", "plain body", "--content-type", "text/csv"],
    ["call", "enc", "-H", "login-customer-id: 123", "--header", "X-Extra:  spaced  ",
     "--authorization-method", "oauth"],
    ["call", "enc", "--data", "{a:1}"],
    ["call", "enc", "--query", "novalue"],
    ["call", "enc", "--header", "no colon"],
    ["call", "enc", "--header", ": empty"],
    ["call", "enc", "--data", "@missing.json"],
])
def test_call_request(fake, tmp_path, go_bin, argv):
    fake.route("/call/enc", Canned(200, {}, b"{}"))
    fake.route("*", Canned(200, {}, b"{}"))
    py, go, py_seen, go_seen = _both(fake, tmp_path, go_bin, argv)
    _assert_same(py, go) if py.code == 0 else _assert_same_refusal(py, go)
    _assert_same_request(py_seen, go_seen)


def _assert_same_refusal(py, go):
    assert py.code == go.code, (py, go)
    assert py.out == go.out
    # A refusal the CLI decides names the same problem; a JSON parser's own wording may differ.
    assert normalize(py.err).split("(")[0] == normalize(go.err).split("(")[0], (py.err, go.err)


def test_call_bodies_from_files(fake, tmp_path, go_bin):
    fake.route("/call/files", Canned(200, {}, b"{}"))
    for home in ("py", "go"):
        (tmp_path / home).mkdir(exist_ok=True)
        (tmp_path / home / "body.json").write_bytes(b'\xef\xbb\xbf{"bom": 1}')
        (tmp_path / home / "u16.json").write_bytes('{"w": "ü"}'.encode("utf-16"))
        (tmp_path / home / "pic.png").write_bytes(b"\x89PNG data")
    for argv, stdin in (
        (["call", "files", "--data", "@body.json"], None),
        (["call", "files", "--file", "u16.json"], None),
        (["call", "files", "--data", "@-"], b'{"from": "stdin"}'),
        (["call", "files", "--upload", "image=@pic.png", "--upload", "caption=hi there"], None),
    ):
        py, go, py_seen, go_seen = _both(fake, tmp_path, go_bin, argv, stdin=stdin)
        _assert_same(py, go)
        _assert_same_request(py_seen, go_seen)


def test_rejected_env_token(fake, tmp_path, go_bin):
    fake.route("/call/auth", Canned(401, {"X-Treg-Error": "1"}, b'{"detail":"invalid token"}'))
    py, go, *_ = _both(fake, tmp_path, go_bin, ["call", "auth"], env={"TREG_TOKEN": "env-token"})
    _assert_same(py, go)


def test_org_override_and_session_headers(fake, tmp_path, go_bin):
    fake.route("/call/hdr", Canned(200, {}, b"{}"))
    env = {"TREG_TELEMETRY": "1", "TREG_SESSION_ID": "task-42", "CLAUDECODE": "1", "TREG_ORG": "other"}
    py, go, py_seen, go_seen = _both(fake, tmp_path, go_bin, ["call", "hdr"], env=env)
    _assert_same(py, go)
    for key in ("x-treg-session", "x-treg-agent", "x-treg-client", "x-treg-org", "x-treg-token"):
        assert py_seen[0].headers.get(key) == go_seen[0].headers.get(key), key
