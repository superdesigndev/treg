"""`treg mcp install` — registering the treg MCP server into agents, header-authed, user-global.

The header path is deliberate: treg 200s a valid Authorization header, so a client never falls back
to OAuth discovery (verified against Claude Code). These tests lock the config each agent actually
writes and the user-global scope — a project-scoped MCP entry is a per-repo surprise, not a setup.
"""
from __future__ import annotations

import json
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from treg import mcp_install


@contextmanager
def _umask(mask):
    old = os.umask(mask)
    try:
        yield
    finally:
        os.umask(old)


def test_json_agents_write_user_global_header_config(tmp_path, monkeypatch):
    """Cursor + opencode: a header-authed entry in the per-USER config file (not a project ./ file),
    merged so anything already there survives, and idempotent on re-run."""
    monkeypatch.setattr(mcp_install, "HOME", tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    # pre-existing unrelated servers must be preserved
    cur = tmp_path / ".cursor"; cur.mkdir()
    (cur / "mcp.json").write_text(json.dumps({"mcpServers": {"other": {"url": "http://x"}}}))
    (tmp_path / ".config" / "opencode").mkdir(parents=True)

    out = mcp_install.install_mcp(base_url="https://treg.to", token="TESTKEY",
                                  only=["cursor", "opencode"])
    got = {d: (s, detail) for d, s, detail in out["results"]}
    assert got["Cursor"][0] == "ok" and got["opencode"][0] == "ok", out

    cursor = json.loads((cur / "mcp.json").read_text())
    assert cursor["mcpServers"]["other"] == {"url": "http://x"}          # untouched
    treg = cursor["mcpServers"]["treg"]
    assert treg["url"] == "https://treg.to/mcp/"
    assert treg["headers"]["Authorization"] == "Bearer TESTKEY"

    oc = json.loads((tmp_path / ".config" / "opencode" / "opencode.json").read_text())
    assert oc["mcp"]["treg"]["type"] == "remote" and oc["mcp"]["treg"]["enabled"] is True
    assert oc["mcp"]["treg"]["headers"]["Authorization"] == "Bearer TESTKEY"

    # idempotent: a second run leaves exactly one entry, still correct
    mcp_install.install_mcp(base_url="https://treg.to", token="TESTKEY", only=["cursor"])
    again = json.loads((cur / "mcp.json").read_text())
    assert list(again["mcpServers"].keys()) == ["other", "treg"]


@pytest.mark.skipif(os.name == "nt", reason="Windows ACLs are not POSIX mode bits")
@pytest.mark.parametrize(("existing_mode", "mask"), [
    (None, 0o022),
    (0o644, 0o022),
    (0o600, 0o022),
    (0o644, 0o077),
    (0o600, 0o777),
])
def test_json_agent_config_is_0600_for_new_and_existing_files(
        tmp_path, existing_mode, mask):
    target = tmp_path / "opencode.json"
    if existing_mode is not None:
        target.write_text(json.dumps({"mcp": {"other": {"url": "https://other.example"}}}))
        target.chmod(existing_mode)
    meta = {
        "path": lambda: target,
        "root": "mcp",
        "entry": lambda url, token: {"url": url, "token": token},
    }

    with _umask(mask):
        status, _ = mcp_install._write_json_agent(
            meta, "treg", "https://treg.example/mcp/", "synthetic-token")

    assert status == "ok"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    if existing_mode is not None:
        assert json.loads(target.read_text())["mcp"]["other"] == {
            "url": "https://other.example"
        }


@pytest.mark.skipif(os.name == "nt", reason="symlink behavior differs on Windows")
def test_json_agent_ignores_the_old_fixed_tmp_symlink(tmp_path):
    target = tmp_path / "opencode.json"
    victim = tmp_path / "victim.json"
    victim.write_text("do not overwrite")
    old_tmp = tmp_path / "opencode.json.tmp"
    old_tmp.symlink_to(victim)
    meta = {
        "path": lambda: target,
        "root": "mcp",
        "entry": lambda url, token: {"url": url, "token": token},
    }

    status, _ = mcp_install._write_json_agent(
        meta, "treg", "https://treg.example/mcp/", "synthetic-token")

    assert status == "ok"
    assert old_tmp.is_symlink()
    assert victim.read_text() == "do not overwrite"


@pytest.mark.skipif(os.name == "nt", reason="Windows ACLs are not POSIX mode bits")
def test_json_agent_creates_private_config_directory(tmp_path):
    target = tmp_path / "agent" / "opencode.json"
    meta = {
        "path": lambda: target,
        "root": "mcp",
        "entry": lambda url, token: {"url": url, "token": token},
    }

    with _umask(0o000):
        status, _ = mcp_install._write_json_agent(
            meta, "treg", "https://treg.example/mcp/", "synthetic-token")

    assert status == "ok"
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_json_agent_failure_cleans_temp_and_preserves_original(tmp_path, monkeypatch):
    target = tmp_path / "opencode.json"
    original = json.dumps({"mcp": {"other": {"url": "https://other.example"}}})
    target.write_text(original)
    meta = {
        "path": lambda: target,
        "root": "mcp",
        "entry": lambda url, token: {"url": url, "token": token},
    }

    def fail_replace(*_):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(mcp_install.os, "replace", fail_replace)

    status, detail = mcp_install._write_json_agent(
        meta, "treg", "https://treg.example/mcp/", "never-print-this-token")

    assert status == "error"
    assert "never-print-this-token" not in detail
    assert target.read_text() == original
    assert list(tmp_path.glob(".opencode.json.*.tmp")) == []


def test_json_agent_concurrent_writers_use_distinct_tempfiles(tmp_path, monkeypatch):
    target = tmp_path / "opencode.json"
    target.write_text(json.dumps({"mcp": {"other": {"url": "https://other.example"}}}))
    meta = {
        "path": lambda: target,
        "root": "mcp",
        "entry": lambda url, token: {"url": url, "token": token},
    }
    real_mkstemp = mcp_install.tempfile.mkstemp
    barrier = threading.Barrier(2)
    temp_names = []

    def synchronized_mkstemp(*args, **kwargs):
        opened = real_mkstemp(*args, **kwargs)
        temp_names.append(opened[1])
        barrier.wait(timeout=5)
        return opened

    monkeypatch.setattr(mcp_install.tempfile, "mkstemp", synchronized_mkstemp)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda token: mcp_install._write_json_agent(
                meta, "treg", "https://treg.example/mcp/", token),
            ["synthetic-token-one", "synthetic-token-two"],
        ))

    assert [status for status, _ in results] == ["ok", "ok"]
    assert len(set(temp_names)) == 2
    assert json.loads(target.read_text())["mcp"]["other"] == {
        "url": "https://other.example"
    }
    assert list(tmp_path.glob(".opencode.json.*.tmp")) == []


@pytest.mark.skipif(os.name == "nt", reason="Windows ACLs are not POSIX mode bits")
def test_actual_mcp_install_cli_writes_restricted_config(tmp_path, monkeypatch):
    """Exercise parsing, token validation, agent detection and the end-user install command."""
    import httpx

    from treg import cli, cli_analytics

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, path):
            assert path == "/auth/me"
            return httpx.Response(200, json={"email": "synthetic@example.test"},
                                  request=httpx.Request("GET", "https://treg.example/auth/me"))

    def fail_external_command(*args, **kwargs):
        raise AssertionError("the isolated CLI test must not run an external command")

    config = tmp_path / "treg-config.json"
    config.write_text(json.dumps({
        "base_url": "https://treg.example",
        "token": "synthetic-cli-token",
    }))
    opencode_dir = tmp_path / ".config" / "opencode"
    opencode_dir.mkdir(parents=True)
    target = opencode_dir / "opencode.json"
    target.write_text(json.dumps({"mcp": {"other": {"url": "https://other.example"}}}))
    target.chmod(0o600)
    monkeypatch.setattr(cli, "CONFIG_PATH", config)
    monkeypatch.setattr(cli, "_client", lambda cfg, **kwargs: FakeClient())
    monkeypatch.setattr(cli_analytics, "track_command", lambda **kwargs: None)
    monkeypatch.setattr(mcp_install, "HOME", tmp_path)
    monkeypatch.setattr(mcp_install, "MCP_AGENTS", {
        "opencode": mcp_install.MCP_AGENTS["opencode"],
    })
    monkeypatch.setattr(mcp_install, "MANUAL_AGENTS", {})
    monkeypatch.setattr(mcp_install.subprocess, "run", fail_external_command)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.delenv("TREG_TOKEN", raising=False)
    monkeypatch.delenv("TREG_URL", raising=False)

    with _umask(0o022):
        cli.main(["mcp", "install"])

    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    written = json.loads(target.read_text())
    assert written["mcp"]["other"] == {"url": "https://other.example"}
    assert written["mcp"]["treg"]["headers"]["Authorization"] == \
        "Bearer synthetic-cli-token"


def test_uninstalled_agents_are_skipped_not_written(tmp_path, monkeypatch):
    """No marker dir → the agent isn't touched (no stray config created for something not installed)."""
    monkeypatch.setattr(mcp_install, "HOME", tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    out = mcp_install.install_mcp(base_url="https://treg.to", token="K",
                                  only=["cursor", "opencode"])
    assert out["results"] == []                       # nothing installed → nothing written
    assert not (tmp_path / ".cursor").exists()


def test_the_mcp_url_carries_the_trailing_slash(tmp_path, monkeypatch):
    """The resource identifier is `/mcp/` — the transport is served there, and a client that resolves
    metadata uses that exact form.

    HOME is isolated even though `only=[]` writes nothing: this exact test once ran against the
    REAL home (empty list read as "no filter" by the old `if only and …` guard), so every suite run
    silently rewrote the developer's actual Claude/Cursor/opencode configs with `Bearer K` — and
    every MCP call on the machine failed days later with "invalid token". A test must never be one
    guard away from writing outside its sandbox."""
    monkeypatch.setattr(mcp_install, "HOME", tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    out = mcp_install.install_mcp(base_url="https://treg.to/", token="K", only=[])
    assert out["mcp_url"] == "https://treg.to/mcp/"


def test_only_empty_means_NONE_not_all(tmp_path, monkeypatch):
    """`only=[]` writes nothing anywhere — an empty restriction is "no agents", not "no filter".
    The old falsy check made [] behave like None, which is how the suite's dummy token reached
    real configs (see test above)."""
    monkeypatch.setattr(mcp_install, "HOME", tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    (tmp_path / ".cursor").mkdir()                       # cursor IS "installed" in this home…
    (tmp_path / ".config" / "opencode").mkdir(parents=True)
    out = mcp_install.install_mcp(base_url="https://treg.to", token="K", only=[])
    assert out["results"] == [] and out["manual"] == []  # …and still nothing is written
    assert not (tmp_path / ".cursor" / "mcp.json").exists()
    assert not (tmp_path / ".config" / "opencode" / "opencode.json").exists()


def test_cmd_mcp_install_REFUSES_a_bad_token_before_writing(tmp_path, monkeypatch):
    """The command validates the token against /auth/me before touching any agent config — the same
    check `treg login --token` runs. Without it a garbage token (stale TREG_TOKEN, mangled paste)
    fans out silently into every agent on the machine and surfaces days later as per-provider
    "invalid token" errors inside whichever agent tries a call."""
    from types import SimpleNamespace

    import httpx
    import pytest

    from treg import cli

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            assert url == "/auth/me"
            return httpx.Response(401, json={"detail": "invalid token"},
                                  request=httpx.Request("GET", "http://t/auth/me"))

    monkeypatch.setattr(cli, "_client", lambda cfg, **k: FakeClient())
    monkeypatch.setattr(mcp_install, "HOME", tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    (tmp_path / ".config" / "opencode").mkdir(parents=True)

    with pytest.raises(SystemExit) as e:
        cli.cmd_mcp_install(SimpleNamespace(name=None),
                            {"token": "K", "base_url": "https://treg.to"})
    assert "rejected" in str(e.value)
    assert not (tmp_path / ".config" / "opencode" / "opencode.json").exists()  # nothing written


def test_cmd_mcp_install_retries_transient_ssl_errors(tmp_path, monkeypatch):
    """The SSL: WRONG_VERSION_NUMBER bug from the repro: first mcp install attempt gets an SSL
    error, but a retry succeeds. The command must retry transient errors instead of failing."""
    import ssl
    import time as time_module
    from types import SimpleNamespace

    import httpx

    from treg import cli

    attempts = []

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            attempts.append(url)
            if len(attempts) == 1:
                raise ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number (_ssl.c:1029)")
            return httpx.Response(200, json={"email": "test@x.dev"},
                                  request=httpx.Request("GET", "http://t/auth/me"))

    monkeypatch.setattr(cli, "_client", lambda cfg, **k: FakeClient())
    monkeypatch.setattr(mcp_install, "HOME", tmp_path)
    monkeypatch.setattr(mcp_install, "install_mcp", lambda **k: {"results": [], "manual": [], "mcp_url": k["base_url"] + "/mcp/"})
    monkeypatch.setattr(time_module, "sleep", lambda s: None)

    cli.cmd_mcp_install(SimpleNamespace(name=None), {"token": "K", "base_url": "https://treg.to"})
    assert len(attempts) == 2  # first failed, second succeeded


def test_is_transient_network_error_detects_ssl_and_connection_errors():
    """_is_transient_network_error classifies errors so mcp install knows what to retry."""
    import ssl
    import httpx
    from treg import cli

    # Create an SSL error the same way Python's ssl module does
    ssl_err = ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number")
    assert cli._is_transient_network_error(ssl_err)
    assert cli._is_transient_network_error(ConnectionRefusedError())
    assert cli._is_transient_network_error(TimeoutError("connection timed out"))
    assert cli._is_transient_network_error(httpx.ConnectError("connection failed"))
    assert cli._is_transient_network_error(OSError("network unreachable"))
    assert cli._is_transient_network_error(Exception("SSL: WRONG_VERSION_NUMBER"))
    assert not cli._is_transient_network_error(ValueError("bad value"))
    assert not cli._is_transient_network_error(KeyError("missing key"))
