"""The beta channel end to end on this machine: the registry's installer, sign-in, a call, `treg
update` and the background update, against signed releases served locally.

Opt-in: TREG_GO_RELEASES names a directory holding `cli-v1.0.0/` and `cli-v1.0.1/`, each built by
cli/scripts/release.sh with TREG_UPDATE_KEY and signed with that key's secret half.
"""
from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from .cli_go_live import LiveRegistry

RELEASES = os.environ.get("TREG_GO_RELEASES")
pytestmark = pytest.mark.skipif(not RELEASES, reason="set TREG_GO_RELEASES to signed local releases")


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def downloads():
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=RELEASES))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _registry(tmp_path_factory, downloads, beta: str) -> LiveRegistry:
    return LiveRegistry(tmp_path_factory.mktemp(f"reg-{beta}"), extra_env={
        "TREG_CLI_BETA_VERSION": beta,
        "TREG_CLI_DOWNLOAD_BASE_URL": f"{downloads}/cli-v{{version}}",
        # Stable has no release here: no setting, and a repository with no releases to fall back to.
        "TREG_CLI_RELEASE_REPO": "superdesigndev/treg-no-releases-here",
    })


@pytest.fixture(scope="module")
def registries(tmp_path_factory, downloads):
    first = _registry(tmp_path_factory, downloads, "1.0.0")
    second = _registry(tmp_path_factory, downloads, "1.0.1")
    yield first, second
    first.close()
    second.close()


def _env(home: Path, **extra) -> dict[str, str]:
    path = ":".join(p for p in os.environ["PATH"].split(":") if not Path(p, "treg").exists())
    env = {k: v for k, v in os.environ.items() if not k.startswith("TREG_")}
    env |= {"HOME": str(home), "PATH": f"{home}/.local/bin:{path}", "TREG_TELEMETRY": "0", "SHELL": "/bin/zsh"}
    return env | extra


def _sh(cmd: str, env: dict[str, str], timeout: float = 180) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", "-c", cmd], env=env, capture_output=True, text=True, timeout=timeout)


def _installed(tmp_path, registry) -> tuple[Path, dict[str, str]]:
    home = tmp_path / "home"
    home.mkdir()
    env = _env(home)
    r = _sh(f"curl -fsSL '{registry.url}/install.sh?channel=beta' | sh", env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Installed treg 1.0.0" in r.stdout, r.stdout
    return home / ".local" / "bin" / "treg", env


def test_install_sign_in_and_call(tmp_path, registries):
    first, _ = registries
    treg, env = _installed(tmp_path, first)
    assert _sh(f"'{treg}' version", env).stdout.strip() == "treg 1.0.0"
    assert _sh("command -v treg", env).stdout.strip() == str(treg)
    cfg = json.loads((tmp_path / "home" / ".treg" / "config.json").read_text())
    assert cfg["base_url"] == first.url and cfg["channel"] == "beta"

    team = first.signup("beta@example.com", "Beta Testers")
    from .cli_go_live import EchoUpstream
    echo = EchoUpstream()
    try:
        first.own_tool(team, "echo", echo.url)
        assert _sh(f"'{treg}' login --token {team.token}", env).returncode == 0
        who = json.loads(_sh(f"'{treg}' --json whoami", env).stdout)
        assert who["team"] == team.slug
        called = _sh(f"'{treg}' call echo v1/ping -p a=1", env)
        assert called.returncode == 0 and json.loads(called.stdout)["query"] == [["a", "1"]], called
    finally:
        echo.close()


def test_update_and_background_update(tmp_path, registries):
    first, second = registries
    treg, env = _installed(tmp_path, first)
    same = _sh(f"'{treg}' update", env)
    assert same.returncode == 0 and "newest release on the beta channel" in same.stdout, same

    updated = _sh(f"TREG_URL={second.url} '{treg}' update", env)
    assert updated.returncode == 0 and "Updated treg 1.0.0 -> 1.0.1" in updated.stdout, updated
    assert _sh(f"'{treg}' version", env).stdout.strip() == "treg 1.0.1"

    # A fresh 1.0.0 that has not checked today updates itself in the background.
    shutil.rmtree(tmp_path / "home")
    treg, env = _installed(tmp_path, first)
    state = tmp_path / "home" / ".treg" / "update-state.json"
    state.write_text(json.dumps({"checked_at": 0}))
    assert _sh(f"TREG_URL={second.url} '{treg}' version", env).stdout.strip() == "treg 1.0.0"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if _sh(f"'{treg}' version", env | {"TREG_NO_AUTO_UPDATE": "1"}).stdout.strip() == "treg 1.0.1":
            break
        time.sleep(0.5)
    else:
        pytest.fail(f"no background update: {state.read_text()}")
    assert "updated 1.0.0 -> 1.0.1" in json.loads(state.read_text())["result"]


def test_leaving_the_beta_before_stable_has_a_release(tmp_path, registries):
    first, _ = registries
    treg, env = _installed(tmp_path, first)
    left = _sh(f"'{treg}' update --channel stable", env)
    assert left.returncode == 0 and "stable channel has no release yet, so this stays treg 1.0.0" in left.stdout, left
    assert json.loads((tmp_path / "home" / ".treg" / "config.json").read_text())["channel"] == "stable"
    again = _sh(f"'{treg}' update", env)
    assert again.returncode == 0 and "has no release yet" in again.stdout, again
    back = _sh(f"'{treg}' update --channel beta", env)
    assert back.returncode == 0 and "treg 1.0.0 is the newest release on the beta channel" in back.stdout, back


def test_a_tampered_release_is_refused(tmp_path, registries, tmp_path_factory, downloads):
    first, _ = registries
    treg, env = _installed(tmp_path, first)
    forged = Path(RELEASES) / "cli-v1.0.9"
    shutil.rmtree(forged, ignore_errors=True)
    shutil.copytree(Path(RELEASES) / "cli-v1.0.1", forged)
    for archive in forged.glob("treg_1.0.1_*"):
        archive.rename(forged / archive.name.replace("1.0.1", "1.0.9"))
    sums = (forged / "SHA256SUMS").read_text().replace("1.0.1", "1.0.9")
    (forged / "SHA256SUMS").write_text(sums)
    registry = _registry(tmp_path_factory, downloads, "1.0.9")
    try:
        r = _sh(f"TREG_URL={registry.url} '{treg}' update", env)
        assert r.returncode == 1 and "not signed by treg's release key" in r.stderr, r
        assert _sh(f"'{treg}' version", env).stdout.strip() == "treg 1.0.0"
    finally:
        registry.close()
        shutil.rmtree(forged, ignore_errors=True)
