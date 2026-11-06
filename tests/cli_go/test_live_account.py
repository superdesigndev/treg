"""Sign-in, identity and calls against a real registry, the Go CLI beside the Python CLI."""
from __future__ import annotations

import json
import re

from cli_go_harness import PYTHON_CLI, normalize, run, same_stdout, write_config


def _pair(tmp_path, live, go_bin, argv, *, config=None, env=None, stdin=None):
    out = []
    for label, cli in (("py", PYTHON_CLI), ("go", [str(go_bin)])):
        home = tmp_path / label
        write_config(home, base_url=live.url, **(config or {}))
        out.append((home, run(cli + argv, home=home, env=env, stdin=stdin)))
    return out


def test_login_with_token_then_whoami(tmp_path, live, team, go_bin):
    (py_home, py), (go_home, go) = _pair(tmp_path, live, go_bin, ["login", "--token", team.token])
    assert (py.code, normalize(py.text)) == (go.code, normalize(go.text)), (py, go)
    py_cfg = json.loads((py_home / ".treg" / "config.json").read_text())
    go_cfg = json.loads((go_home / ".treg" / "config.json").read_text())
    assert py_cfg == go_cfg
    assert py_cfg["active_org"] == team.slug and py_cfg["token"] == team.token
    for argv in (["whoami"], ["--json", "whoami"], ["config"]):
        py2 = run(PYTHON_CLI + argv, home=py_home)
        go2 = run([str(go_bin)] + argv, home=go_home)
        assert py2.code == go2.code and same_stdout(py2, go2), (argv, py2, go2)


def test_login_rejected_token(tmp_path, live, go_bin):
    (_, py), (go_home, go) = _pair(tmp_path, live, go_bin, ["login", "--token", "not-a-token"])
    assert py.code == go.code == 1
    assert normalize(py.err) == normalize(go.err)
    assert json.loads((go_home / ".treg" / "config.json").read_text()).get("token") is None


def test_login_with_email_code(tmp_path, live, go_bin):
    import os
    import subprocess
    configs = []
    for label, cli in (("py", PYTHON_CLI), ("go", [str(go_bin)])):
        email = f"{label}-otp@example.com"
        home = tmp_path / label
        write_config(home, base_url=live.url)
        env = {k: v for k, v in os.environ.items() if not k.startswith("TREG_")}
        env |= {"HOME": str(home), "TREG_CONFIG": str(home / ".treg" / "config.json"), "TREG_TELEMETRY": "0"}
        proc = subprocess.Popen(cli + ["login", "--email", email], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=home)
        first = proc.stdout.readline().decode()
        assert "your code is" in first, (label, first)
        out, err = proc.communicate((first.rsplit(" ", 1)[1].strip() + "\n").encode(), timeout=60)
        assert proc.returncode == 0, (label, out, err)
        assert out.decode().endswith("Active org: None\n"), (label, out)
        cfg = json.loads((home / ".treg" / "config.json").read_text())
        configs.append({k: v for k, v in cfg.items() if k not in ("token", "email")})
        assert cfg["identity"] is True and cfg["email"] == email
    assert configs[0] == configs[1]


def test_call_own_tool(tmp_path, live, team, go_bin, echo):
    config = {"token": team.token, "active_org": team.slug, "identity": True}
    for argv in (
        ["call", "echo", "v1/items", "-p", "q=a b", "-p", "q=2"],
        ["call", "echo", "v1/items", "--data", '{"x": "ü"}'],
        ["--json", "call", "echo", "v1/items"],
        ["call", "echo", "status/404"],
        ["call", "echo", "text"],
        ["call", "echo", "bytes"],
        ["call", "nope", "x"],
    ):
        (_, py), (_, go) = _pair(tmp_path, live, go_bin, argv, config=config)
        assert py.code == go.code, (argv, py, go)
        assert same_stdout(_scrub(py), _scrub(go)), (argv, py.out, go.out)
        assert _lines(py.err) == _lines(go.err), (argv, py.err, go.err)


def _scrub(r):
    """Each call has its own id."""
    return type(r)(r.code, re.sub(rb'"call_id": ?"[0-9a-f]+"', b'"call_id":"-"', r.out), r.err)


def _lines(err):
    return [line for line in normalize(err).splitlines() if "call id" not in line]
