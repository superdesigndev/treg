"""Shell mode (`treg shell`) — the MVP / Phase 1 transparent CLI interception.

These exercise the client-side pieces without a live server or a real vendor CLI: the tool→shim
selection, the shim contract (clean PATH + verbatim args + exit-code passthrough → no recursion), the
session wiring (shim dir first on PATH, REALPATH kept clean, teardown), and the stop/guard paths.
See docs/CLI-SHELL-MODE-PLAN.md §7.
"""

from __future__ import annotations

import argparse
import os
import signal
import stat
import subprocess

import pytest

from treg import cli, shell


# ---- selection + routing ------------------------------------------------------------------
def test_plan_shims_filters_and_sorts():
    tools = [
        {"name": "stripe-tool", "cli": {"bin": "stripe", "enabled": True}},
        {"name": "gh", "cli": {"bin": "gh", "enabled": True}},
        {"name": "off", "cli": {"bin": "flyctl", "enabled": False}},   # local runs not enabled → skip
        {"name": "nobin", "cli": {"enabled": True}},                    # no bin → skip
        {"name": "nocli"},                                               # no cli profile → skip
        {"name": "escape", "cli": {"bin": "../evil", "enabled": True}},  # not a plain filename → skip
        {"name": "dup", "cli": {"bin": "gh", "enabled": True}},          # same bin as gh → first wins
    ]
    entries, warnings = shell.plan_shims(tools)
    assert entries == [("gh", "gh", "local"), ("stripe", "stripe-tool", "local")]
    assert warnings == []


def test_plan_shims_routes_server_for_runnable_tools():
    tools = [
        {"name": "stripe-tool", "cli": {"bin": "stripe", "enabled": True}, "server_runnable": True},
        {"name": "gh", "cli": {"bin": "gh", "enabled": True}, "server_runnable": False},
    ]
    # by bin (stripe) → server; gh requested but NOT server-runnable → falls back to local + a warning
    entries, warnings = shell.plan_shims(tools, frozenset({"stripe", "gh"}))
    assert entries == [("gh", "gh", "local"), ("stripe", "stripe-tool", "server")]
    assert any("gh" in w and "server" in w for w in warnings)


def test_plan_shims_matches_server_for_by_tool_name():
    tools = [{"name": "stripe-tool", "cli": {"bin": "stripe", "enabled": True}, "server_runnable": True}]
    entries, _ = shell.plan_shims(tools, frozenset({"stripe-tool"}))  # by tool name, not bin
    assert entries == [("stripe", "stripe-tool", "server")]


# ---- the shim contract --------------------------------------------------------------------
def test_shim_execs_treg_run_with_clean_path_and_verbatim_args(tmp_path):
    """The heart of it: the shim must call `treg run <tool> -- <args>` verbatim, on the clean PATH,
    and hand back the CLI's exit code. Uses a fake `treg` that records its argv + PATH, then exits 7."""
    argv_out, path_out = tmp_path / "argv.out", tmp_path / "path.out"
    fake_treg = tmp_path / "faketreg"
    fake_treg.write_text(
        "#!/bin/sh\n"
        f'printf "%s" "$PATH" > {path_out}\n'
        f': > {argv_out}\n'
        f'for a in "$@"; do printf "%s\\n" "$a" >> {argv_out}; done\n'
        "exit 7\n"
    )
    fake_treg.chmod(0o755)

    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shell.write_shims(str(shim_dir), [("stripe", "stripe-tool", "local")], str(fake_treg))
    assert stat.S_IMODE((shim_dir / "stripe").stat().st_mode) == 0o755

    env = dict(os.environ, TREG_SHELL_REALPATH="/clean/only", PATH="/usr/bin:/bin")
    r = subprocess.run([str(shim_dir / "stripe"), "balance", "--live"], env=env, capture_output=True)

    assert r.returncode == 7  # the real CLI's exit code passes through
    assert path_out.read_text() == "/clean/only"  # ran on the clean PATH → no shim recursion
    assert argv_out.read_text().splitlines() == ["run", "stripe-tool", "--", "balance", "--live"]


def test_server_route_shim_execs_treg_run_server(tmp_path):
    """A server-routed shim must call `treg run --server <tool>` through a real shell."""
    argv_out = tmp_path / "argv.out"
    fake_treg = tmp_path / "faketreg"
    fake_treg.write_text(
        "#!/bin/sh\n"
        f': > {argv_out}\n'
        f'for a in "$@"; do printf "%s\\n" "$a" >> {argv_out}; done\n'
    )
    fake_treg.chmod(0o755)
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shell.write_shims(str(shim_dir), [("stripe", "stripe-tool", "server")], str(fake_treg))
    env = dict(os.environ, TREG_SHELL_REALPATH="/usr/bin:/bin")
    r = subprocess.run([str(shim_dir / "stripe"), "balance"], env=env, capture_output=True)
    assert r.returncode == 0
    assert argv_out.read_text().splitlines() == ["run", "--server", "stripe-tool", "--", "balance"]


def test_completion_call_bypasses_treg_real_bin_runs(tmp_path):
    """Through a real shell: `gh __complete …` execs the real bin directly (no treg → no audit/cap),
    while `gh <normal>` still routes through treg run."""
    real_log, treg_log = tmp_path / "real.out", tmp_path / "treg.out"
    real_bin = tmp_path / "realgh"
    real_bin.write_text(f'#!/bin/sh\nprintf "REAL %s\\n" "$*" >> {real_log}\n')
    real_bin.chmod(0o755)
    fake_treg = tmp_path / "faketreg"
    fake_treg.write_text(f'#!/bin/sh\nprintf "TREG %s\\n" "$*" >> {treg_log}\n')
    fake_treg.chmod(0o755)

    shim = tmp_path / "bin" / "gh"
    shim.parent.mkdir()
    shim.write_text(shell.shim_script("gh", str(fake_treg), real_bin=str(real_bin)))
    shim.chmod(0o755)
    env = dict(os.environ, TREG_SHELL_REALPATH="/usr/bin:/bin")

    subprocess.run([str(shim), "__complete", "sta"], env=env)   # completion → real bin
    subprocess.run([str(shim), "repo", "list"], env=env)        # normal → treg run
    assert real_log.read_text() == "REAL __complete sta\n"      # completion never hit treg
    assert treg_log.read_text() == "TREG run gh -- repo list\n"  # normal did


def test_run_subshell_ttl_closes_the_session():
    """The TTL hard cap fires: a subshell that would sleep 30s is terminated by the 1s timer."""
    import time
    t0 = time.monotonic()
    rc = shell._run_subshell(["/bin/sh", "-c", "sleep 30"], dict(os.environ), ttl_seconds=1)
    assert time.monotonic() - t0 < 10  # the timer closed it; we did not wait the full 30s
    assert rc != 0                      # terminated, not a clean exit


def test_registered_bin_resolves_shim_in_a_real_shell(tmp_path):
    """Through a real /bin/sh: a registered CLI name resolves to our shim (first on PATH) while an
    unregistered command is untouched — the 'is this registered?' test done for free by name resolution."""
    argv_out = tmp_path / "argv.out"
    fake_treg = tmp_path / "faketreg"
    fake_treg.write_text(
        "#!/bin/sh\n"
        f': > {argv_out}\n'
        f'for a in "$@"; do printf "%s\\n" "$a" >> {argv_out}; done\n'
    )
    fake_treg.chmod(0o755)
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shell.write_shims(str(shim_dir), [("stripe", "stripe-tool", "local")], str(fake_treg))

    realpath = "/usr/bin:/bin"
    env = dict(os.environ, PATH=f"{shim_dir}{os.pathsep}{realpath}", TREG_SHELL_REALPATH=realpath)
    # `stripe` → our shim; `true` (not registered, no shim) resolves to the system binary normally
    r = subprocess.run(["/bin/sh", "-c", "stripe deploy --prod && true"], env=env, capture_output=True)
    assert r.returncode == 0
    assert argv_out.read_text().splitlines() == ["run", "stripe-tool", "--", "deploy", "--prod"]


# ---- session base dir ---------------------------------------------------------------------
def test_session_base_dir_prefers_private_per_user(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert shell.session_base_dir() == str(tmp_path)
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    assert shell.session_base_dir() == str(tmp_path)


# ---- start_session wiring (no real subshell) ----------------------------------------------
def test_start_session_wires_path_and_tears_down(tmp_path, monkeypatch):
    captured: dict = {}

    def fake_run(argv, env, ttl_seconds=None):
        captured["argv"] = argv
        captured["env"] = dict(env)
        captured["ttl_seconds"] = ttl_seconds
        # the session dir + the shim must exist WHILE the shell runs
        captured["dir_exists_during"] = os.path.isdir(env[shell.ENV_DIR])
        captured["shim_exists_during"] = os.path.exists(os.path.join(env[shell.ENV_DIR], "bin", "stripe"))
        return 3

    monkeypatch.setattr(shell, "_run_subshell", fake_run)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

    rc = shell.start_session([("stripe", "stripe", "local")], "/usr/local/bin/treg", ttl_minutes=30)

    assert rc == 3  # the subshell's exit code is returned
    env = captured["env"]
    shim_dir = os.path.join(env[shell.ENV_DIR], "bin")
    assert env["PATH"].startswith(shim_dir + os.pathsep)       # our shims resolve first
    assert env[shell.ENV_REALPATH] == "/usr/bin:/bin"           # REALPATH is clean (no shim dir)
    assert shim_dir not in env[shell.ENV_REALPATH]
    assert env[shell.ENV_ACTIVE] == "1"
    assert env[shell.ENV_PID] == str(os.getpid())
    assert captured["ttl_seconds"] == 30 * 60  # --ttl minutes → seconds for the hard cap
    assert captured["dir_exists_during"]
    assert captured["shim_exists_during"]  # a shim was written before the shell launched
    # teardown ran on exit
    assert not os.path.exists(env[shell.ENV_DIR])


# ---- stop / guards ------------------------------------------------------------------------
def test_stop_outside_a_session_exits(monkeypatch):
    monkeypatch.delenv("TREG_SHELL", raising=False)
    with pytest.raises(SystemExit):
        shell.stop_session()


def test_stop_signals_the_controller(monkeypatch):
    sent: dict = {}
    monkeypatch.setenv("TREG_SHELL", "1")
    monkeypatch.setenv("TREG_SHELL_PID", "4242")
    monkeypatch.setattr(shell.os, "kill", lambda pid, sig: sent.update(pid=pid, sig=sig))
    shell.stop_session()
    assert sent == {"pid": 4242, "sig": signal.SIGTERM}


# ---- cmd_shell_start guards ---------------------------------------------------------------
def test_cmd_shell_start_refuses_when_nested(monkeypatch):
    monkeypatch.setenv("TREG_SHELL", "1")
    with pytest.raises(SystemExit):
        cli.cmd_shell_start(object(), {"token": "t", "base_url": "http://x"})


def test_cmd_shell_start_needs_login(monkeypatch):
    monkeypatch.delenv("TREG_SHELL", raising=False)
    with pytest.raises(SystemExit):
        cli.cmd_shell_start(object(), {"base_url": "http://x"})


def test_cmd_shell_start_no_runnable_clis_exits(monkeypatch):
    monkeypatch.delenv("TREG_SHELL", raising=False)

    class _Resp:
        status_code = 200
        def json(self): return [{"name": "api-only", "cli": None}]

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, path): return _Resp()

    monkeypatch.setattr(cli, "_client", lambda cfg: _C())
    args = cli.build_parser().parse_args(["shell", "start"])  # server_for=None, ttl=None
    with pytest.raises(SystemExit):
        cli.cmd_shell_start(args, {"token": "t", "base_url": "http://x"})


# ---- --proxy wiring (P4 of docs/LOCAL-PROXY-PLAN.md) ---------------------------------------
def test_start_session_publishes_the_proxy_env_and_stops_it(tmp_path, monkeypatch):
    """The seam `--proxy` uses: extra variables go into the subshell, and the proxy is stopped on the
    way out — a listener left running after the shell closed would keep answering for a session that
    no longer exists."""
    captured: dict = {}
    stopped: list = []

    def fake_run(argv, env, ttl_seconds=None):
        captured["env"] = dict(env)
        return 0

    monkeypatch.setattr(shell, "_run_subshell", fake_run)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

    shell.start_session(
        [("stripe", "stripe", "local")], "/usr/local/bin/treg",
        extra_env={"HTTPS_PROXY": "http://treg:tok@127.0.0.1:18791", "SSL_CERT_FILE": "/x/ca-bundle.pem"},
        on_close=lambda: stopped.append(True),
        captured_hosts=["api.stripe.com"],
    )
    assert captured["env"]["HTTPS_PROXY"] == "http://treg:tok@127.0.0.1:18791"
    assert captured["env"]["SSL_CERT_FILE"] == "/x/ca-bundle.pem"
    assert stopped == [True]


def test_extra_env_cannot_break_the_shims(tmp_path, monkeypatch):
    """Applied AFTER our own variables: an add-on that set PATH or a TREG_SHELL* marker would stop
    name resolution finding the shims, or make the session look like a nested one."""
    captured: dict = {}
    monkeypatch.setattr(shell, "_run_subshell",
                        lambda argv, env, ttl_seconds=None: captured.update(env=dict(env)) or 0)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

    shell.start_session([("stripe", "stripe", "local")], "/usr/local/bin/treg",
                        extra_env={"PATH": "/evil", shell.ENV_REALPATH: "/evil", shell.ENV_PID: "1"})
    env = captured["env"]
    assert env["PATH"].startswith(os.path.join(env[shell.ENV_DIR], "bin"))
    assert env[shell.ENV_REALPATH] == "/usr/bin:/bin"
    assert env[shell.ENV_PID] == str(os.getpid())


def test_the_banner_names_the_captured_hosts(capsys):
    """A member must never discover interception by accident: the hosts are listed, and everything
    else is named as untouched."""
    shell._print_banner([("stripe", "stripe", "local")], None, ["api.stripe.com", "api.intercom.io"])
    err = capsys.readouterr().err
    assert "Also captured (2 hosts)" in err
    assert {"api.stripe.com", "api.intercom.io"} <= set(err.split())   # whole hosts, not substrings
    assert "Every other address goes straight out" in err


def test_cmd_shell_start_seeds_the_allow_list_from_the_tool_listing(monkeypatch, tmp_path):
    """`--proxy` reuses the tools already fetched for the shims — every registered host, including
    ones with no CLI, and no second request to the registry."""
    from treg import localproxy as lpx

    tools = [
        {"name": "stripe", "host": "api.stripe.com", "cli": {"bin": "stripe", "enabled": True}},
        {"name": "intercom", "host": "API.INTERCOM.IO"},          # no CLI: still captured by the proxy
        {"name": "odd", "cli": {"bin": "odd", "enabled": True}},   # no host: nothing to capture
    ]
    seen: dict = {}

    class _Handle:
        def env(self, treg_host):
            seen["treg_host"] = treg_host
            return {"HTTPS_PROXY": "http://x"}

        def stop(self):
            seen["stopped"] = True

    monkeypatch.setattr(lpx, "ensure_ca", lambda renew=False: seen.setdefault("ca", object()))
    monkeypatch.setattr(lpx, "start", lambda cfg: seen.setdefault("cfg", cfg) and None or _Handle())

    args = argparse.Namespace(proxy=True, proxy_port=None, renew_ca=False)
    env, stop, hosts = cli._start_local_proxy(args, {"base_url": "https://treg.example", "token": "tok",
                                                     "active_org": "acme"}, tools)
    assert hosts == ["api.intercom.io", "api.stripe.com"]     # lowercased, sorted, host-less tool skipped
    assert seen["cfg"].hosts == frozenset({"api.stripe.com", "api.intercom.io"})
    assert seen["cfg"].treg_token == "tok" and seen["cfg"].org == "acme"
    assert seen["cfg"].base_url == "https://treg.example"
    assert seen["treg_host"] == "treg.example"                # the registry never comes back through us
    assert env["HTTPS_PROXY"] == "http://x"
    stop()
    assert seen["stopped"]


def test_cmd_shell_start_explains_a_missing_certificate_library(monkeypatch):
    """The light CLI has no `cryptography`. Saying exactly what to install beats a traceback."""
    from treg import localproxy as lpx

    def _boom(renew=False):
        raise lpx.ProxyDependencyError('install it with: pip install "tools-registry[proxy]"')

    monkeypatch.setattr(lpx, "ensure_ca", _boom)
    args = argparse.Namespace(proxy=True, proxy_port=None, renew_ca=False)
    with pytest.raises(SystemExit) as exc:
        cli._start_local_proxy(args, {"base_url": "https://x", "token": "t"}, [])
    assert "tools-registry[proxy]" in str(exc.value)


def test_a_busy_port_is_a_readable_error(monkeypatch):
    from treg import localproxy as lpx

    monkeypatch.setattr(lpx, "ensure_ca", lambda renew=False: object())
    monkeypatch.setattr(lpx, "start", lambda cfg: (_ for _ in ()).throw(OSError("address in use")))
    args = argparse.Namespace(proxy=True, proxy_port=18791, renew_ca=False)
    with pytest.raises(SystemExit) as exc:
        cli._start_local_proxy(args, {"base_url": "https://x", "token": "t"}, [])
    assert "--proxy-port" in str(exc.value) and "18791" in str(exc.value)


# ---- treg serve (the daemon front door) -----------------------------------------------------
def test_serve_env_prints_shell_lines_and_the_way_back(tmp_path):
    """A daemon nobody can reach is useless, and typing ten exports by hand is worse. `--unset` is the
    way out, because `treg serve stop` cannot reach into a shell that already has the variables."""
    env = {"HTTPS_PROXY": "http://treg:a b@127.0.0.1:18791", "SSL_CERT_FILE": "/x/ca.pem"}
    lines = cli._serve_export_lines(env)
    assert "export SSL_CERT_FILE=/x/ca.pem" in lines
    assert "export HTTPS_PROXY='http://treg:a b@127.0.0.1:18791'" in lines   # quoted: the token is opaque
    assert cli._serve_export_lines(env, unset=True) == "unset HTTPS_PROXY\nunset SSL_CERT_FILE"


def test_serve_start_refuses_a_second_one(monkeypatch, capsys):
    from treg import localproxy as lpx

    monkeypatch.setattr(lpx, "running", lambda: {"port": 18791, "pid": 4242})
    with pytest.raises(SystemExit) as exc:
        cli.cmd_serve_start(argparse.Namespace(port=None, renew_ca=False, foreground=False),
                            {"token": "t", "base_url": "http://x"})
    assert "already running" in str(exc.value) and "treg serve stop" in str(exc.value)


def test_serve_start_needs_login(monkeypatch):
    with pytest.raises(SystemExit) as exc:
        cli.cmd_serve_start(argparse.Namespace(port=None, renew_ca=False, foreground=False),
                            {"base_url": "http://x"})
    assert "treg login" in str(exc.value)


def test_serve_status_says_what_is_captured(monkeypatch, capsys):
    from treg import localproxy as lpx

    monkeypatch.setattr(lpx, "running", lambda: {
        "port": 18791, "pid": 4242, "base_url": "https://treg.example", "org": "acme",
        "hosts": ["api.stripe.com", "api.vercel.com"]})
    cli.cmd_serve_status(argparse.Namespace(), {})
    out = capsys.readouterr().out
    assert "127.0.0.1:18791" in out and "acme" in out
    assert {"api.stripe.com", "api.vercel.com"} <= set(out.split())
    assert "Every other address goes straight out" in out


def test_serve_status_and_env_when_nothing_runs(monkeypatch, capsys):
    from treg import localproxy as lpx

    monkeypatch.setattr(lpx, "running", lambda: None)
    cli.cmd_serve_status(argparse.Namespace(), {})
    assert "not running" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exc:
        cli.cmd_serve_env(argparse.Namespace(unset=False), {})
    assert "treg serve start" in str(exc.value)


def test_unset_works_after_the_proxy_is_gone(monkeypatch, capsys):
    """--unset must NOT need a running proxy: it is what you reach for right after `stop`. Requiring
    one leaves the shell wedged — its variables point at a dead port, every call fails, and the one
    command that fixes it refuses. Found in live testing."""
    from treg import localproxy as lpx

    monkeypatch.setattr(lpx, "running", lambda: None)
    cli.cmd_serve_env(argparse.Namespace(unset=True), {})
    out = capsys.readouterr().out
    assert "unset HTTPS_PROXY" in out and "unset SSL_CERT_FILE" in out
    assert "unset NODE_USE_ENV_PROXY" in out and "export" not in out


def test_serve_stop_reports_when_nothing_runs(monkeypatch):
    from treg import localproxy as lpx

    monkeypatch.setattr(lpx, "running", lambda: None)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_serve_stop(argparse.Namespace(), {})
    assert "no proxy is running" in str(exc.value)


# ---- `treg <command>` — per-launch opt-in, nothing global -----------------------------------
def test_bare_word_runs_a_program_but_a_typo_stays_a_treg_error(monkeypatch):
    """`treg claude` should launch claude. `treg toool ls` must stay an ordinary treg error, not an
    attempt to execute `toool` — which is why the word has to exist on PATH before we fall through."""
    seen: dict = {}
    monkeypatch.setattr(cli, "cmd_with", lambda args, cfg: seen.update(cmd=args.command, args=args.args))
    monkeypatch.setattr(cli, "_load_config", lambda: {"token": "t", "base_url": "http://x"})

    cli.main(["sh", "-c", "true"])                      # sh is on every machine
    assert seen == {"cmd": "sh", "args": ["-c", "true"]}

    with pytest.raises(SystemExit) as exc:
        cli.main(["toool", "ls"])
    assert exc.value.code == 2                          # argparse's "invalid choice", not an exec


def test_a_real_treg_command_is_never_shadowed(monkeypatch, tmp_path):
    """If someone has a program called `call` or `org` on their PATH, treg's own command still wins."""
    fake = tmp_path / "call"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    seen: dict = {}
    monkeypatch.setattr(cli, "cmd_call", lambda args, cfg: seen.update(ran="treg-call"))
    monkeypatch.setattr(cli, "cmd_with", lambda args, cfg: seen.update(ran="with"))
    monkeypatch.setattr(cli, "_load_config", lambda: {"token": "t", "base_url": "http://x"})
    cli.main(["call", "https://api.example.com/x"])
    assert seen == {"ran": "treg-call"}


def test_with_refuses_a_program_that_does_not_exist():
    with pytest.raises(SystemExit) as exc:
        cli.cmd_with(argparse.Namespace(command="definitely-not-a-real-binary", args=[], quiet=True),
                     {"token": "t", "base_url": "http://x"})
    assert "not a treg command" in str(exc.value)


def test_with_needs_login():
    with pytest.raises(SystemExit) as exc:
        cli.cmd_with(argparse.Namespace(command="sh", args=["-c", "true"], quiet=True), {})
    assert "treg login" in str(exc.value)


def test_with_starts_its_own_proxy_and_stops_it(monkeypatch):
    """Nothing is left running: the proxy belongs to that one command. Port 0 means the operating
    system picks, so two `treg claude` sessions can never collide."""
    from treg import localproxy as lpx

    events = []

    class _Handle:
        port = 41234
        def env(self, treg_host):
            return {"HTTPS_PROXY": "http://treg:tok@127.0.0.1:41234"}
        def stop(self):
            events.append("stopped")

    monkeypatch.setattr(lpx, "running", lambda: None)
    monkeypatch.setattr(cli, "_proxy_tools", lambda cfg: [{"host": "api.stripe.com"}])
    monkeypatch.setattr(cli, "_start_proxy_handle",
                        lambda cfg, tools, port=None, renew_ca=False:
                            (events.append(f"started port={port}"), (_Handle(), ["api.stripe.com"], "x"))[1])
    monkeypatch.setattr("treg.shell._run_subshell", lambda argv, env: events.append(("ran", argv[0], env["HTTPS_PROXY"])) or 0)

    with pytest.raises(SystemExit) as exc:
        cli.cmd_with(argparse.Namespace(command="sh", args=["-c", "true"], quiet=True),
                     {"token": "t", "base_url": "http://x"})
    assert exc.value.code == 0
    assert events[0] == "started port=0"                       # an OS-chosen port, never a fixed one
    assert events[1][0] == "ran" and events[1][2].endswith("41234")
    assert events[-1] == "stopped"                             # and it does not outlive the command


def test_with_attaches_to_a_running_daemon_and_leaves_it_up(monkeypatch):
    """A `treg serve` daemon is someone's deliberate choice — borrowing it must not shut it down."""
    from treg import localproxy as lpx

    events = []
    monkeypatch.setattr(lpx, "running", lambda: {
        "port": 18800, "pid": 1, "token": "daemon-tok", "base_url": "https://treg.example",
        "org": "", "hosts": ["api.stripe.com"]})
    monkeypatch.setattr(cli, "_start_proxy_handle",
                        lambda *a, **k: pytest.fail("must not start a second proxy"))
    monkeypatch.setattr("treg.shell._run_subshell",
                        lambda argv, env: events.append(env["HTTPS_PROXY"]) or 7)

    with pytest.raises(SystemExit) as exc:
        cli.cmd_with(argparse.Namespace(command="sh", args=[], quiet=True),
                     {"token": "t", "base_url": "https://treg.example"})
    assert exc.value.code == 7                                  # the command's own exit code
    assert events == ["http://treg:daemon-tok@127.0.0.1:18800"]


def test_with_runs_the_program_for_real_and_passes_args_and_exit_code(monkeypatch, tmp_path):
    """End to end through a real process: argv verbatim, the proxy variables present, exit code kept."""
    from treg import localproxy as lpx

    out = tmp_path / "argv.txt"
    monkeypatch.setattr(lpx, "running", lambda: {
        "port": 18800, "pid": 1, "token": "tok", "base_url": "https://treg.example",
        "org": "", "hosts": []})
    script = f'printf "%s\\n" "$@" > {out}; printf "%s" "$HTTPS_PROXY" >> {out}; exit 9'
    with pytest.raises(SystemExit) as exc:
        cli.cmd_with(argparse.Namespace(command="sh", args=["-c", script, "_", "a b", "--flag"],
                                        quiet=True), {"token": "t", "base_url": "https://treg.example"})
    assert exc.value.code == 9
    body = out.read_text()
    assert "a b\n--flag\n" in body                    # spaces survive; no re-splitting
    assert "127.0.0.1:18800" in body                  # the child really saw the proxy


# ---- the certificate library: offered, not just described ----------------------------------
def test_the_install_hint_matches_how_treg_was_installed(monkeypatch):
    """`pip install "tools-registry[proxy]"` is right for exactly ONE of the four ways people install
    treg — and the installer's own way is not it. A uv-tool or Homebrew venv is not on the ambient
    pip's path, so that advice silently does nothing."""
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda n: object() if n == "pip" else None)
    for prefix, label in {"/opt/homebrew/Cellar/treg/0.6.0/libexec": "Homebrew",
                          "/Users/x/venv": "pip"}.items():
        monkeypatch.setattr(cli.sys, "prefix", prefix)
        got, argv = cli._proxy_install_hint()
        assert got == label and argv[:3] == [cli.sys.executable, "-m", "pip"]
        assert argv[-1] == "cryptography>=43"


def test_a_pip_less_environment_uses_uv_instead(monkeypatch):
    """Found by running it: a `uv venv` has NO pip, so the obvious `python -m pip install` fails with
    'No module named pip'. Probing beats guessing from the path."""
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda n: None)
    monkeypatch.setattr(cli.shutil, "which", lambda n: "/usr/bin/uv" if n == "uv" else None)
    monkeypatch.setattr(cli.sys, "prefix", "/Users/x/.local/share/uv/tools/tools-registry")
    label, argv = cli._proxy_install_hint()
    assert label == "uv tool"
    assert argv[:2] == ["uv", "pip"] and cli.sys.executable in argv


def test_pipx_is_injected_not_pip_installed(monkeypatch):
    monkeypatch.setattr(cli.sys, "prefix", "/Users/x/.local/pipx/venvs/tools-registry")
    monkeypatch.setattr(cli.shutil, "which", lambda n: f"/usr/bin/{n}" if n == "pipx" else None)
    label, argv = cli._proxy_install_hint()
    assert label == "pipx" and argv[:2] == ["pipx", "inject"]


def test_an_environment_with_no_installer_says_so(monkeypatch):
    """Nothing to install with: say that plainly instead of running a command that cannot exist."""
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda n: None)
    monkeypatch.setattr(cli.shutil, "which", lambda n: None)
    monkeypatch.setattr(cli.sys, "prefix", "/opt/weird")
    assert cli._proxy_install_hint()[1] == []
    _hide_cryptography(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        cli.ensure_proxy_dependency(assume_yes=True)
    assert "no installer" in str(exc.value)


def test_nothing_happens_when_the_library_is_already_there():
    cli.ensure_proxy_dependency()          # cryptography is installed in the test env — must be silent


def _hide_cryptography(monkeypatch):
    """Pretend the certificate library is not installed, without uninstalling it."""
    import builtins
    real = builtins.__import__

    def _fake(name, *a, **k):
        if name == "cryptography":
            raise ModuleNotFoundError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _fake)


@pytest.fixture
def proxy_installer(monkeypatch):
    # Prompt/error tests need an installer, independently of the machine's PATH and Python setup.
    # Installer discovery itself is covered by the tests above.
    monkeypatch.setattr(cli, "_proxy_install_hint", lambda: (
        "pip", [cli.sys.executable, "-m", "pip", "install", "cryptography>=43"],
    ))


def test_a_non_interactive_run_prints_the_command_instead_of_prompting(monkeypatch, proxy_installer):
    """In CI or a pipe there is nobody to answer, so it must exit saying exactly what to run — never
    hang on a prompt nobody can see."""
    _hide_cryptography(monkeypatch)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit) as exc:
        cli.ensure_proxy_dependency()
    assert "cryptography>=43" in str(exc.value)


def test_declining_leaves_the_command_behind(monkeypatch, proxy_installer):
    _hide_cryptography(monkeypatch)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _p: "n")
    monkeypatch.setattr(cli.subprocess, "call", lambda *a, **k: pytest.fail("must not install"))
    with pytest.raises(SystemExit) as exc:
        cli.ensure_proxy_dependency()
    assert "cryptography>=43" in str(exc.value)


def test_a_failed_install_says_so_rather_than_carrying_on(monkeypatch, proxy_installer):
    _hide_cryptography(monkeypatch)
    monkeypatch.setattr(cli.subprocess, "call", lambda *a, **k: 1)
    with pytest.raises(SystemExit) as exc:
        cli.ensure_proxy_dependency(assume_yes=True)
    assert "did not work" in str(exc.value)
