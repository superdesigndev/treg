"""`treg call --await` prints the same answer, stderr lines and exit status from the Go CLI as from
the Python CLI for the same provider behaviour, against a real registry.

The Python CLI reads `X-Treg-Async` and polls `/call/` itself; the Go CLI follows the registry's
`/tasks/{call_id}`. The provider is a scripted upstream behind a team's own key: the submission's
prompt names the scenario, its task id is the scenario name, and each submission restarts the
scenario's poll answers, so both CLIs see the same provider.
"""
from __future__ import annotations

import json
import re
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from .cli_go_harness import PYTHON_CLI, Canned, cli_env, normalize, run, same_stdout, write_config

PATH_MODE = "replicate.image-gen.flux-schnell"
FETCH_MODE = "replicate.video-gen.seedance-1-lite"
SLOW = "replicate.video-gen.veo-3.1-fast"
INSTANT = "replicate.image-gen.nano-banana-pro"
WINDOW = "replicate.image-gen.gpt-image-2"
POLL = "replicate.predictions.get"

_BASE = {"id_from": "id", "poll": {"endpoint": POLL, "param": {"in": "pathParams", "name": "id"}},
         "status": {"path": "status", "success": ["succeeded"], "failure": ["failed", "canceled"],
                    "billed_failure": ["billed"], "progress": ["starting", "processing"]},
         "result": {"path": "output"}, "interval": 0.2}
DESCRIPTORS = {
    PATH_MODE: _BASE,
    FETCH_MODE: _BASE | {"result": {"fetch": POLL, "ttl_note": "1h",
                                    "fetch_param": {"in": "pathParams", "name": "id", "value_from": "output.file"}}},
    SLOW: _BASE | {"interval": 1},
    INSTANT: _BASE | {"terminal_on_submission": True},
    WINDOW: _BASE | {"interval": 0.5, "max_age": 1.5},
}

JSON = "application/json"
PROCESSING = (200, JSON, {"status": "processing"})


def _ok(output) -> tuple:
    return 200, JSON, {"status": "succeeded", "output": output}


# name: (endpoint, submission answer, poll answers (the last repeats), extra argv)
SCENARIOS = {
    "ok": (PATH_MODE, None, [PROCESSING, PROCESSING, _ok(["https://example.test/ok.png"])], []),
    "odd": (PATH_MODE, None, [(200, JSON, {"status": "weird"}), (200, JSON, {"status": "weird"}),
                              (200, JSON, {}), (200, JSON, {"status": "中文\x1b[2J"}), _ok({"n": 1})], []),
    "unicode": (PATH_MODE, None, [_ok("完成 ✓")], []),
    "fail": (PATH_MODE, None, [PROCESSING, (200, JSON, {"status": "failed", "error": "moderated"})], []),
    "billed": (PATH_MODE, None, [(200, JSON, {"status": "billed"})], []),
    "fetched": (FETCH_MODE, None, [_ok({"file": "f; 9"})], []),
    "nofile": (FETCH_MODE, None, [_ok({})], []),
    "gone": (PATH_MODE, None, [(404, JSON, {"detail": "not found"})], []),
    "flaky": (PATH_MODE, None, [(500, JSON, {"error": "busy"})], []),
    "recovers": (PATH_MODE, None, [(502, JSON, {}), (503, JSON, {}), _ok([])], []),
    "text": (PATH_MODE, None, [(200, "text/plain", "not json")], []),
    "slow": (SLOW, None, [PROCESSING], ["--timeout", "2.5"]),
    "rejected": (PATH_MODE, (200, JSON, {"oops": 1}), [], []),
    "garbage": (PATH_MODE, (200, "text/plain", "accepted"), [], []),
    "instant": (INSTANT, (200, JSON, {"id": "instant", "status": "succeeded",
                                      "output": "https://example.test/i.png"}), [], []),
    "refused": (PATH_MODE, (422, JSON, {"detail": "bad prompt"}), [], []),
}


class ScriptedProvider:
    """Answers a submission (any POST) per its scenario and `GET /v1/predictions/<id>` with the
    scenario's next poll answer."""

    def __init__(self) -> None:
        self.cursor: dict[str, int] = {}
        provider = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                name = body["input"]["prompt"]
                provider.cursor[name] = 0
                answer = SCENARIOS[name][1] or (201, JSON, {"id": name, "status": "starting"})
                self._send(*answer)

            def do_GET(self):
                name = self.path.rsplit("/", 1)[-1]
                polls = SCENARIOS[name][2]
                index = min(provider.cursor.get(name, 0), len(polls) - 1)
                provider.cursor[name] = index + 1
                self._send(*polls[index])

            def _send(self, status, ctype, payload):
                data = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture(scope="module")
def registry(tmp_path_factory):
    from .cli_go_live import LiveRegistry
    provider = ScriptedProvider()
    live = LiveRegistry(tmp_path_factory.mktemp("await"), catalog_patch={
        "providers": {"replicate": provider.url}, "async": DESCRIPTORS})
    team = live.signup("await@example.com", "Acme Await")
    live.http.post("/secrets", headers={"X-Treg-Token": team.token},
                   json={"name": "replicate", "value": "own-key"}).raise_for_status()
    yield live, team
    live.close()
    provider.close()


def _argv(name: str, json_flag: bool = False) -> list[str]:
    endpoint, _, _, extra = SCENARIOS[name]
    return (["--json"] if json_flag else []) + ["call", endpoint, "--data",
                                                json.dumps({"input": {"prompt": name}}), "--await", *extra]


def _home(tmp_path, name: str, live, team):
    home = tmp_path / name
    write_config(home, base_url=live.url, token=team.token, active_org=team.slug, email=team.email,
                 identity=False)
    return home


def _elapsed(text: str) -> str:
    """Stderr without what differs between two runs: the seconds waited and the call ids."""
    text = re.sub(r"\(\d+s elapsed\)", "(Ns elapsed)", normalize(text))
    return re.sub(r"call id [0-9a-f]{32}", "call id <id>", text)


def _assert_same(py, go):
    assert py.code == go.code, (py, go)
    assert same_stdout(py, go), (py.out, go.out)
    assert _elapsed(py.err) == _elapsed(go.err), (py.err, go.err)


EXPECTED_CODES = {"ok": 0, "odd": 0, "unicode": 0, "fail": 2, "billed": 2, "fetched": 0, "nofile": 1,
                  "gone": 3, "flaky": 3, "recovers": 0, "text": 3, "slow": 3, "rejected": 1,
                  "garbage": 1, "instant": 0, "refused": 1}


# `--json` changes nothing once a call is awaited; it is checked on a success, a failure and two refusals.
CASES = [(name, False) for name in sorted(SCENARIOS)] + [
    (name, True) for name in ("ok", "fail", "refused", "rejected")]


@pytest.mark.parametrize("name, json_flag", CASES)
def test_await_output(registry, tmp_path, go_bin, name, json_flag):
    live, team = registry
    py = run(PYTHON_CLI + _argv(name, json_flag), home=_home(tmp_path, "py", live, team))
    go = run([str(go_bin)] + _argv(name, json_flag), home=_home(tmp_path, "go", live, team))
    _assert_same(py, go)
    assert go.code == EXPECTED_CODES[name], go


def test_await_is_a_plain_call_without_an_async_answer(registry, tmp_path, go_bin):
    live, team = registry
    argv = ["call", POLL, "-p", "id=slow", "--await"]
    py = run(PYTHON_CLI + argv, home=_home(tmp_path, "py", live, team))
    go = run([str(go_bin)] + argv, home=_home(tmp_path, "go", live, team))
    _assert_same(py, go)
    assert go.code == 0 and json.loads(go.out) == {"status": "processing"}


def _interrupted(argv: list[str], home) -> tuple[int, str]:
    proc = subprocess.Popen(argv, env=cli_env(home), cwd=home, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    seen = b""
    deadline = time.monotonic() + 30
    while b"async task slow: processing" not in seen and time.monotonic() < deadline:
        seen += proc.stderr.readline()
    proc.send_signal(signal.SIGINT)
    out, err = proc.communicate(timeout=30)
    assert out == b""
    return proc.returncode, (seen + err).decode()


def test_ctrl_c_leaves_the_task_recoverable(registry, tmp_path, go_bin):
    live, team = registry
    argv = ["call", SLOW, "--data", json.dumps({"input": {"prompt": "slow"}}), "--await"]
    py = _interrupted(PYTHON_CLI + argv, _home(tmp_path, "py", live, team))
    go = _interrupted([str(go_bin)] + argv, _home(tmp_path, "go", live, team))
    assert py[0] == go[0] == 3
    assert _elapsed(py[1]) == _elapsed(go[1])
    assert "waiting interrupted; the upstream task is still recoverable\nresume: " \
           "treg call replicate.predictions.get -p id=slow\n" in go[1]


def test_the_polling_window_is_counted_from_the_submission(registry, tmp_path, go_bin):
    """Go stops at the task's `max_age` as the registry counts it, from the submission; the Python
    CLI counted it from the start of its own wait and could poll once more past it."""
    live, team = registry
    SCENARIOS["window"] = (WINDOW, None, [PROCESSING], [])
    try:
        go = run([str(go_bin)] + _argv("window"), home=_home(tmp_path, "go", live, team))
    finally:
        del SCENARIOS["window"]
    assert go.code == 3 and go.out == b""
    lines = go.err.splitlines()
    assert lines[:2] == ["async task submitted: window", "resume: treg call replicate.predictions.get -p id=window"]
    assert lines[-1] == "treg: timed out while waiting for the async task"
    assert 1 <= sum(line.startswith("async task window: processing") for line in lines) <= 3


# The task endpoint's contract seen from the Go CLI alone: answers a fake registry gives.

ASYNC = json.dumps({"id_from": "id", "poll": {"endpoint": "x.poll", "param": {"in": "queryParams", "name": "id"}},
                    "status": {"path": "status", "success": ["done"], "failure": ["failed"]},
                    "result": {"path": "result"}})


def _task(state: str, **fields) -> Canned:
    view = {"call_id": "c1", "task_id": "t1", "task_shown": "t1", "state": state, "status": None,
            "status_shown": None, "billed": None, "warning": None, "response": None, "result": None,
            "result_text": None, "result_url": None, "fetch_command": None, "ttl_note": None,
            "resume": "treg call x.poll -p id=t1", "retry_after": 0.05, "max_wait": 86400.0,
            "error": None} | fields
    return Canned(200, {"Retry-After": "1"}, json.dumps(view).encode())


def _go_alone(fake, tmp_path, go_bin, argv):
    home = tmp_path / "go"
    write_config(home, base_url=fake.url, token="tok-1", active_org="acme", email="a@b.c", identity=False)
    return run([str(go_bin)] + argv, home=home)


def test_an_older_registry_without_the_task_endpoint(fake, tmp_path, go_bin):
    fake.route("/call/x.submit", Canned(200, {"X-Treg-Async": ASYNC, "X-Treg-Call-Id": "c1"}, b'{"id":"t1"}'))
    fake.route("/tasks/c1", Canned(404, {}, b'{"detail":"Not Found"}'))
    go = _go_alone(fake, tmp_path, go_bin, ["call", "x.submit", "--await"])
    assert go.code == 1 and go.out == b""
    assert go.err == ("treg: this registry has no /tasks/ endpoint, which --await needs (a registry newer "
                      "than treg 0.30.0 has it); ask its operator to update it\n")


def test_a_routed_pending_answer_is_followed_to_its_end(fake, tmp_path, go_bin):
    """The Python CLI cannot read a routed pending answer's descriptor and exits 1; Go asks the
    registry, which knows the child's task."""
    pending = json.dumps({"output": {"email": None}, "raw": {"id": "t1"},
                          "_treg": {"outcome": "pending", "async": {"task_id": "t1"}}}).encode()
    fake.route("/call/treg.people.email.verify", Canned(202, {
        "X-Treg-Async": json.dumps({"task_id": "t1", "poll": {"endpoint": "x.poll"}}),
        "X-Treg-Call-Id": "c1", "X-Treg-Route-Outcome": "pending"}, pending))
    response = {"source": "poll", "http_status": 200, "content_type": JSON,
                "body": '{"status":"done","result":"deliverable"}', "call_id": "p2", "cost_micro": 0}
    fake.route("/tasks/c1", _task("pending"),
               _task("pending", status="queued", status_shown="queued",
                     warning="unknown async status 'queued'; continuing to wait",
                     response=response | {"body": '{"status":"queued"}'}),
               _task("succeeded", status="done", status_shown="done", response=response,
                     result="deliverable", result_text="deliverable"))
    go = _go_alone(fake, tmp_path, go_bin, ["call", "treg.people.email.verify", "--data", '{"email":"a@b.c"}',
                                            "--await"])
    assert go.code == 0, go
    assert go.out == b'{"status":"done","result":"deliverable"}'
    assert go.err.splitlines() == [
        "async task submitted: t1", "resume: treg call x.poll -p id=t1",
        "warning: unknown async status 'queued'; continuing to wait",
        "async task t1: queued (0s elapsed)", "result: deliverable"]
    posted = [s for s in fake.seen if s.path == "/tasks/c1"]
    assert [s.method for s in posted] == ["POST", "GET", "GET"] and posted[0].body == pending

    home = tmp_path / "py"
    write_config(home, base_url=fake.url, token="tok-1", active_org="acme", email="a@b.c", identity=False)
    py = run(PYTHON_CLI + ["call", "treg.people.email.verify", "--data", '{"email":"a@b.c"}', "--await"],
             home=home)
    assert py.code == 1 and "does not contain the async task id" in py.err


def test_the_registry_overloaded_is_retried_then_recoverable(fake, tmp_path, go_bin):
    fake.route("/call/x.submit", Canned(200, {"X-Treg-Async": ASYNC, "X-Treg-Call-Id": "c1",
                                              "X-Treg-Cost-Micro": "250000"}, b'{"id":"t1"}'))
    busy = Canned(503, {}, b'{"treg_saturated":true}')
    fake.route("/tasks/c1", _task("pending"), busy, busy, busy, busy, busy)
    go = _go_alone(fake, tmp_path, go_bin, ["call", "x.submit", "--await"])
    assert go.code == 3
    assert go.err.splitlines() == [
        "async task submitted: t1", "resume: treg call x.poll -p id=t1", "generation reservation: $0.25",
        *[f"async poll retry {n}/5 after HTTP 503" for n in range(1, 6)],
        "treg: polling returned 503 five consecutive times"]
