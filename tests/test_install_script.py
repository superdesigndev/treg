"""install.sh's anonymous install telemetry, run as a real shell script against stand-in installers.

The script is fetched from the route so the templating is part of what runs. Stand-in `uv`, `treg`
and `curl` live in a PATH that holds nothing else but the POSIX tools the script uses, so a host's
own uv or pip3 can never leak into a run.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient

from treg.config import get_settings

TOOLS = ("sh", "tr", "awk", "cut", "od", "sed", "grep", "dirname", "mkdir", "cat", "uname", "head",
         "tail", "chmod", "cp")

FAKE_CURL = """#!/bin/sh
while [ $# -gt 0 ]; do
  if [ "$1" = "-d" ]; then printf '%s\\n' "$2" >> "$CURL_LOG"; shift; fi
  shift
done
"""

FAKE_TREG = """#!/bin/sh
case "$1" in --version) echo "treg $TREG_FAKE_VERSION" ;; esac
exit 0
"""


def _uv(exit_code: int) -> str:
    return f"""#!/bin/sh
cp "$FAKE_TREG_SRC" "$TREG_BIN_DIR/treg"
chmod +x "$TREG_BIN_DIR/treg"
exit {exit_code}
"""


@pytest.fixture
def posthog_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "posthog_key", "phc_test_suite", raising=False)
    monkeypatch.setattr(get_settings(), "posthog_host", "https://ph.example/", raising=False)


def _bin(tmp_path: Path, *, uv_exit: int | None) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in TOOLS:
        found = shutil.which(tool)
        assert found, tool
        (bin_dir / tool).symlink_to(found)
    (bin_dir / "curl").write_text(FAKE_CURL)
    (tmp_path / "treg-src").write_text(FAKE_TREG)
    if uv_exit is not None:
        (bin_dir / "uv").write_text(_uv(uv_exit))
    for f in bin_dir.iterdir():
        if not f.is_symlink():
            f.chmod(0o755)
    return bin_dir


def _run(script: str, tmp_path: Path, bin_dir: Path, **env: str) -> tuple[int, list[dict]]:
    (tmp_path / "home").mkdir(exist_ok=True)
    log = tmp_path / "curl.log"
    log.unlink(missing_ok=True)
    result = subprocess.run(
        ["sh", "-c", script], capture_output=True, text=True, timeout=30,
        env={"PATH": str(bin_dir), "HOME": str(tmp_path / "home"), "CURL_LOG": str(log),
             "FAKE_TREG_SRC": str(tmp_path / "treg-src"), "TREG_BIN_DIR": str(bin_dir),
             "TREG_FAKE_VERSION": "9.9.9", **env})
    events = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return result.returncode, events


def _analytics_id(tmp_path: Path) -> str:
    return (tmp_path / "home" / ".treg" / "analytics-id").read_text().strip()


async def test_install_script_has_no_telemetry_key_without_one(clients: AsyncClient):
    script = (await clients.get("/install.sh")).text
    assert 'PH_KEY=""' in script and "{POSTHOG_KEY}" not in script and "{POSTHOG_HOST}" not in script


async def test_install_reports_start_and_success_under_the_cli_installation_id(
        clients: AsyncClient, posthog_on, tmp_path):
    script = (await clients.get("/install.sh")).text
    code, events = _run(script, tmp_path, _bin(tmp_path, uv_exit=0))
    assert code == 0
    assert [e["properties"]["install_step"] for e in events] == ["started", "installed"]
    installation = _analytics_id(tmp_path)
    uuid.UUID(installation)
    assert {e["distinct_id"] for e in events} == {installation}
    assert {e["api_key"] for e in events} == {"phc_test_suite"}
    assert {e["event"] for e in events} == {"cli_install_step"}
    started, installed = (e["properties"] for e in events)
    assert started["prior_version"] == "" and started["method"] == ""
    assert installed["method"] == "uv" and installed["version"] == "9.9.9" and installed["on_path"] is True
    assert installed["$process_person_profile"] is False and installed["with_token"] is False


async def test_a_reinstall_keeps_the_id_and_reports_the_prior_version(
        clients: AsyncClient, posthog_on, tmp_path):
    script = (await clients.get("/install.sh")).text
    bin_dir = _bin(tmp_path, uv_exit=0)
    _run(script, tmp_path, bin_dir)
    first = _analytics_id(tmp_path)
    code, events = _run(script, tmp_path, bin_dir, TREG_FAKE_VERSION="10.0.0")
    assert code == 0 and _analytics_id(tmp_path) == first
    assert {e["distinct_id"] for e in events} == {first}
    assert events[0]["properties"]["prior_version"] == "10.0.0"


async def test_a_failed_install_reports_why(clients: AsyncClient, posthog_on, tmp_path):
    script = (await clients.get("/install.sh")).text
    code, events = _run(script, tmp_path, _bin(tmp_path, uv_exit=1))
    assert code == 1
    assert [(e["properties"]["install_step"], e["properties"]["reason"]) for e in events] == [
        ("started", ""), ("failed", "install_command")]


async def test_no_installer_reports_why(clients: AsyncClient, posthog_on, tmp_path):
    script = (await clients.get("/install.sh")).text
    code, events = _run(script, tmp_path, _bin(tmp_path, uv_exit=None))
    assert code == 1
    assert events[-1]["properties"]["install_step"] == "failed"
    assert events[-1]["properties"]["reason"] == "no_installer"


@pytest.mark.parametrize("opt_out", [{"DO_NOT_TRACK": "1"}, {"TREG_TELEMETRY": "OFF"}])
async def test_opting_out_sends_nothing_and_writes_no_id(clients: AsyncClient, posthog_on, tmp_path, opt_out):
    script = (await clients.get("/install.sh")).text
    code, events = _run(script, tmp_path, _bin(tmp_path, uv_exit=0), **opt_out)
    assert code == 0 and events == []
    assert not (tmp_path / "home" / ".treg" / "analytics-id").exists()


async def test_the_cli_reads_the_id_the_installer_wrote(clients: AsyncClient, posthog_on, tmp_path):
    from treg import cli_analytics

    script = (await clients.get("/install.sh")).text
    _run(script, tmp_path, _bin(tmp_path, uv_exit=0))
    config = tmp_path / "home" / ".treg" / "config.json"
    assert cli_analytics._installation_id(config) == _analytics_id(tmp_path)
    assert os.stat(config.with_name("analytics-id")).st_mode & 0o077 == 0
