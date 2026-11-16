"""`treg update` must never run Python code against the install it just replaced."""

from __future__ import annotations

import pytest

from treg import cli


class _Exited(Exception):
    def __init__(self, code):
        self.code = code


class _Response:
    status_code = 200
    text = "echo installing"


class _Client:
    def __init__(self, fetched): self.fetched = fetched
    def __enter__(self): return self
    def __exit__(self, *args): return False

    def get(self, path):
        self.fetched.append(path)
        return _Response()


@pytest.fixture
def update(monkeypatch):
    fetched, ran, spawned = [], [], []
    monkeypatch.setattr(cli, "_client", lambda cfg, auth=True: _Client(fetched))
    monkeypatch.setattr(cli.subprocess, "run", lambda argv, **kw: ran.append(argv) or type("P", (), {"returncode": 0})())
    monkeypatch.setattr(cli.subprocess, "Popen", lambda argv, **kw: spawned.append((argv, kw)))

    def _exit(code):
        raise _Exited(code)

    monkeypatch.setattr(cli.os, "_exit", _exit)
    return fetched, ran, spawned


def test_posix_update_ends_the_process_without_returning_to_python(update, monkeypatch):
    fetched, ran, spawned = update
    monkeypatch.setattr(cli, "_windows", lambda: False)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/bin/sh" if name == "sh" else None)
    with pytest.raises(_Exited) as exited:
        cli.cmd_update(None, {"base_url": "https://registry.example"})
    assert exited.value.code == 0
    assert fetched == ["/install.sh"]
    assert ran == [["/bin/sh", "-c", "echo installing"]]
    assert spawned == []


def test_missing_sh_names_the_direct_install_instead_of_a_traceback(update, monkeypatch):
    fetched, ran, _ = update
    monkeypatch.setattr(cli, "_windows", lambda: False)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit) as exited:
        cli.cmd_update(None, {})
    assert "uv tool install --force" in str(exited.value.code)
    assert ran == [] and fetched == []


def test_windows_update_hands_off_to_a_detached_installer(update, monkeypatch):
    fetched, ran, spawned = update
    monkeypatch.setattr(cli, "_windows", lambda: True)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "C:/uv.exe" if name == "uv" else None)
    with pytest.raises(_Exited) as exited:
        cli.cmd_update(None, {})
    assert exited.value.code == 0
    assert ran == [] and fetched == []
    (argv, kw), = spawned
    assert argv[0] == "powershell" and "-EncodedCommand" in argv
    script = __import__("base64").b64decode(argv[-1]).decode("utf-16-le")
    assert "uv tool install --force" in script and "Get-Process" in script
    # After the wait deadline, refuse the install while any install-prefix process remains.
    assert "Still running after 2 minutes" in script
    assert script.index("if ($busy)") < script.index("uv tool install --force")
    assert kw["creationflags"]


def test_windows_without_uv_or_pipx_prints_the_direct_install(update, monkeypatch):
    _, ran, spawned = update
    monkeypatch.setattr(cli, "_windows", lambda: True)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit) as exited:
        cli.cmd_update(None, {})
    assert "uv tool install --force" in str(exited.value.code)
    assert ran == [] and spawned == []
