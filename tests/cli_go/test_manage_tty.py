"""The prompts that only a person at a terminal sees: a hidden key, the auto top-up consent and the
tool-access checklist. Each CLI runs on its own pseudo-terminal, which is also its controlling
terminal, and is answered key by key; the requests each sent and the exit status are compared
(the two prompt libraries draw differently)."""
from __future__ import annotations

import copy
import json
import os
import select
import subprocess
import sys
import time

import pytest

from .cli_go_harness import PYTHON_CLI, Canned, write_config

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX pseudo-terminals")

# Makes the pty named in argv[1] the controlling terminal and stdio, then runs the rest of argv.
LAUNCH = ("import fcntl, os, sys, termios\n"
          "os.setsid()\n"
          "fd = os.open(sys.argv[1], os.O_RDWR)\n"
          "fcntl.ioctl(fd, termios.TIOCSCTTY, 0)\n"
          "for n in (0, 1, 2): os.dup2(fd, n)\n"
          "os.execvp(sys.argv[2], sys.argv[2:])\n")

ORGS = Canned(200, {}, b'[{"org_id": 7, "slug": "acme", "name": "Acme", "role": "owner", "active": true}]')
PROVIDERS = Canned(200, {}, json.dumps([
    {"service": "hunter", "auth_kind": "key", "display_name": "Hunter", "setup_url": "https://hunter.io/keys"},
    {"service": "tomba", "auth_kind": "key", "display_name": "Tomba", "needs_extra_credential": True,
     "extra_credential_label": "secret"}]).encode())


def _drive(cli, argv, home, steps, timeout=30.0):
    """Run on a fresh pty; for each (expected, keys) wait until `expected` was printed, then type keys."""
    master, slave = os.openpty()
    env = {k: v for k, v in os.environ.items() if not k.startswith("TREG_")}
    env |= {"HOME": str(home), "TREG_CONFIG": str(home / ".treg" / "config.json"), "TREG_TELEMETRY": "0",
            "TERM": "xterm-256color", "PYTHONIOENCODING": "utf-8"}
    env.pop("NO_COLOR", None)
    proc = subprocess.Popen([sys.executable, "-c", LAUNCH, os.ttyname(slave), *cli, *argv], env=env, cwd=home,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    seen = b""
    deadline = time.monotonic() + timeout
    pending = list(steps)
    # A pty master can stay readable with nothing to read once the child is gone, so reads never
    # block and an exited child with nothing left to read ends the loop.
    os.set_blocking(master, False)
    try:
        # The slave stays open here until the child exits, so the master never reads a premature EOF.
        while time.monotonic() < deadline:
            if pending and pending[0][0].encode() in seen:
                time.sleep(0.3)
                os.write(master, pending.pop(0)[1].encode())
                continue
            exited = proc.poll() is not None
            ready, _, _ = select.select([master], [], [], 0.1)
            chunk = b""
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    pass
                seen += chunk
            if exited and not chunk:
                break
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        os.close(master)
        os.close(slave)
    assert not pending, (pending, seen)
    return proc.returncode, seen.decode("utf-8", "replace")


def _both(fake, tmp_path, go_bin, argv, routes, steps):
    results = []
    for label, cli in (("py", PYTHON_CLI), ("go", [str(go_bin)])):
        home = tmp_path / label
        write_config(home, base_url=fake.url, token="tok-1", active_org="acme", identity=False)
        fake.routes = copy.deepcopy(routes)
        start = len(fake.seen)
        code, screen = _drive(cli, argv, home, steps)
        sent = [(s.method, s.path, s.body) for s in fake.seen[start:]]
        results.append((code, screen, sent))
    (py_code, py_screen, py_sent), (go_code, go_screen, go_sent) = results
    assert py_code == go_code, (py_screen, go_screen)
    assert py_sent == go_sent, (py_screen, go_screen)
    return py_screen, go_screen


def test_pasted_key_hidden_prompt(fake, tmp_path, go_bin):
    routes = {"/oauth/providers": [PROVIDERS], "/connections/token": [Canned(200, {}, b'{"id": 40, "name": "hunter"}')]}
    py, go = _both(fake, tmp_path, go_bin, ["connections", "connect", "--provider", "hunter"], routes,
                   [("(input hidden)", "hk_secret_123\r")])
    for screen in (py, go):
        assert "Get your API key: https://hunter.io/keys" in screen and "hk_secret_123" not in screen, screen
        assert "Connected Hunter. Connection id: 40 (hunter)" in screen, screen


def test_pasted_key_pair(fake, tmp_path, go_bin):
    routes = {"/oauth/providers": [PROVIDERS], "/connections/token": [Canned(200, {}, b'{"id": 41, "name": "tomba"}')],
              "/connections/41/extra-credential": [Canned(200, {}, b'{"id": 41, "name": "tomba"}')]}
    _both(fake, tmp_path, go_bin, ["connections", "connect", "--provider", "tomba"], routes,
          [("Tomba API key (input hidden)", "ta_key\r"), ("Tomba secret (input hidden)", "ts_secret\r")])


@pytest.mark.parametrize("key", ["\x03", "\x04"])
def test_pasted_key_cancelled(fake, tmp_path, go_bin, key):
    py, go = _both(fake, tmp_path, go_bin, ["connections", "connect", "--provider", "hunter"],
                   {"/oauth/providers": [PROVIDERS]}, [("(input hidden)", key)])
    for screen in (py, go):
        assert "nothing was connected" in screen, screen


def test_key_stdin_at_a_terminal(fake, tmp_path, go_bin):
    _both(fake, tmp_path, go_bin, ["connections", "connect", "--provider", "hunter", "--key-stdin"],
          {"/oauth/providers": [PROVIDERS]}, [])


def test_feedback_stdin_at_a_terminal(fake, tmp_path, go_bin):
    py, go = _both(fake, tmp_path, go_bin, ["feedback", "submit", "other", "-"], {}, [])
    assert json.loads(py.strip()) == json.loads(go.strip())


@pytest.mark.parametrize("answer,code", [("y", 0), ("n", 1), ("\r", 1)])
def test_auto_topup_consent(fake, tmp_path, go_bin, answer, code):
    routes = {"/billing/autotopup": [Canned(200, {}, b'{"autotopup": {"amount_micro": 20000000, "threshold_micro": 5000000, '
                                                     b'"monthly_cap_micro": 2000000000, "per_hour": 5}}')]}
    py, go = _both(fake, tmp_path, go_bin, ["topup", "--auto", "on", "--threshold", "5", "--amount", "20"], routes,
                   [("Authorize treg", answer)])
    for screen in (py, go):
        assert "Whenever your balance drops below $5.0, we charge $20.0" in screen, screen
        assert ("Auto top-up is on." in screen) == (code == 0), screen


@pytest.mark.parametrize("keys", [["\r"], ["y\r"], ["n\r", " \r"], ["n\r", "j \r"], ["n\r", "a\r"]])
def test_agent_tool_access_prompt(fake, tmp_path, go_bin, keys):
    routes = {"/orgs": [ORGS], "/tools": [Canned(200, {}, b'[{"id": 1, "name": "stripe"}, {"id": 2, "name": "gh"}]')],
              "/orgs/7/agents": [Canned(200, {}, b'{"token": "agent-token"}')]}
    steps = [("[Y/n]", keys[0])] + [("Tools this member may use", k) for k in keys[1:]]
    _both(fake, tmp_path, go_bin, ["org", "agent-new", "ci-bot"], routes, steps)
