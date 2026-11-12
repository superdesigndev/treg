"""The registry's answers to the binary CLI: release lookup, usage relay and the installers."""
from __future__ import annotations

import subprocess

import httpx
import pytest

from treg import analytics
from treg.application import cli_releases
from treg.config import get_settings
from treg.infra import github_releases
from treg.routers import cli_client

INSTALL_ID = "0b6f2b1e-7d1c-4c55-9a43-5d0c1f6a2b3c"


@pytest.fixture
def relay_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "posthog_key", "phc_test_suite", raising=False)
    analytics._queue.clear()
    cli_client._hits.clear()
    monkeypatch.setattr(analytics, "_ensure_flusher", lambda: None)
    yield analytics._queue
    analytics._queue.clear()


def _event(**props):
    return {"event": "cli_command_completed", "distinct_id": INSTALL_ID,
            "uuid": "11111111-2222-4333-8444-555555555555",
            "timestamp": "2026-11-07T01:02:03+00:00", "properties": props}


async def test_events_keep_only_the_vocabulary(clients, relay_on):
    r = await clients.post("/cli/events", headers={"X-Treg-Token": ""}, json={"events": [
        _event(command="call", exit_code=0, success=True, duration_ms=12, session="abc", agent="claude-code",
               cli_version="1.0.0", os="Darwin", arch="arm64", tool="tikhub.tiktok.user.profile",
               status_class="2xx", options=["--bogus", "not an option"], email="a@b.c", argv=["call", "x"],
               tool_url="https://x", in_command="org", missing_command="org_budgets"),
        _event(command="Bad Name!", exit_code="0", missing_command="treg claude --resume"),
        {"event": "something_else", "distinct_id": INSTALL_ID, "properties": {}},
        {"event": "cli_command_completed", "distinct_id": "not-a-uuid", "properties": {}},
    ]})
    assert r.status_code == 204
    assert [e["event"] for e in relay_on] == ["cli_command_completed", "cli_command_completed"]
    first, second = (e["properties"] for e in relay_on)
    assert first == {"command": "call", "exit_code": 0, "success": True, "duration_ms": 12, "session": "abc",
                     "agent": "claude-code", "cli_version": "1.0.0", "os": "Darwin", "arch": "arm64",
                     "tool": "tikhub.tiktok.user.profile", "status_class": "2xx", "options": ["--bogus"],
                     "in_command": "org", "missing_command": "org_budgets",
                     "$process_person_profile": False, "$geoip_disable": True, "$lib": "treg-cli"}
    assert second == {"$process_person_profile": False, "$geoip_disable": True, "$lib": "treg-cli"}
    assert relay_on[0]["uuid"] == "11111111-2222-4333-8444-555555555555"
    assert relay_on[0]["distinct_id"] == INSTALL_ID


async def test_event_timestamp_far_from_now_is_replaced(clients, relay_on):
    old = _event(command="call") | {"timestamp": "2001-01-01T00:00:00+00:00"}
    r = await clients.post("/cli/events", json={"events": [old]})
    assert r.status_code == 204
    assert not relay_on[0]["timestamp"].startswith("2001")


async def test_install_step_vocabulary(clients, relay_on):
    r = await clients.post("/cli/events", json={"events": [{
        "event": "cli_install_step", "distinct_id": INSTALL_ID,
        "properties": {"install_step": "installed", "reason": "", "method": "binary", "version": "1.0.0",
                       "prior_version": "0.30.0", "on_path": True, "os": "Linux", "arch": "x86_64",
                       "ad_link": False, "with_token": True, "channel": "beta", "token": "secret"}}]})
    assert r.status_code == 204
    props = relay_on[0]["properties"]
    assert "token" not in props and props["channel"] == "beta" and props["on_path"] is True


@pytest.mark.parametrize("body", [b"not json", b'{"events": "x"}', b"[]",
                                  b'{"events": [' + b",".join([b"{}"] * 51) + b"]}"])
async def test_malformed_batches_are_refused(clients, relay_on, body):
    r = await clients.post("/cli/events", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert relay_on == []


async def test_oversized_batch_is_refused(clients, relay_on):
    r = await clients.post("/cli/events", json={"events": [], "pad": "x" * 70_000})
    assert r.status_code == 413


async def test_relay_limits_each_address(clients, relay_on):
    for _ in range(cli_client._RATE_MAX):
        assert (await clients.post("/cli/events", json={"events": []})).status_code == 204
    assert (await clients.post("/cli/events", json={"events": []})).status_code == 429


async def test_relay_without_posthog_key_drops(clients, monkeypatch):
    monkeypatch.setattr(get_settings(), "posthog_key", "", raising=False)
    analytics._queue.clear()
    cli_client._hits.clear()
    r = await clients.post("/cli/events", json={"events": [_event(command="call")]})
    assert r.status_code == 204 and analytics._queue == []


@pytest.fixture
def release_settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "cli_stable_version", "", raising=False)
    monkeypatch.setattr(s, "cli_beta_version", "", raising=False)
    monkeypatch.setattr(s, "cli_download_base_url", "https://dl.example/v{version}", raising=False)
    github_releases._cache.clear()
    return s


async def test_latest_answers_each_channel_from_settings(clients, release_settings):
    release_settings.cli_stable_version = "1.2.0"
    release_settings.cli_beta_version = "1.3.0"
    stable = (await clients.get("/cli/latest", params={"os": "darwin", "arch": "arm64"})).json()
    assert stable == {"version": "1.2.0", "channel": "stable", "os": "darwin", "arch": "arm64",
                      "archive": "treg_1.2.0_darwin_arm64.tar.gz",
                      "url": "https://dl.example/v1.2.0/treg_1.2.0_darwin_arm64.tar.gz",
                      "checksums_url": "https://dl.example/v1.2.0/SHA256SUMS",
                      "signature_url": "https://dl.example/v1.2.0/SHA256SUMS.minisig"}
    beta = (await clients.get("/cli/latest", params={"channel": "beta", "os": "windows", "arch": "amd64"})).json()
    assert beta["version"] == "1.3.0" and beta["archive"] == "treg_1.3.0_windows_amd64.zip"


async def test_latest_falls_back_to_github_and_ignores_python_versions(clients, release_settings, monkeypatch):
    release_settings.cli_stable_version = "0.31.0"
    asked = []

    async def github(repo):
        asked.append(repo)
        return [github_releases.Release("v0.32.0", False), github_releases.Release("v1.1.0-beta.1", True),
                github_releases.Release("treg-v9.0.0", False), github_releases.Release("v1.0.4", False)]

    monkeypatch.setattr(github_releases, "published", github)
    stable = await clients.get("/cli/latest", params={"os": "linux", "arch": "amd64"})
    beta = await clients.get("/cli/latest", params={"channel": "beta", "os": "linux", "arch": "amd64"})
    assert stable.json()["version"] == "1.0.4" and beta.json()["version"] == "1.1.0-beta.1"
    assert asked == ["superdesigndev/treg"] * 2


async def test_latest_refuses_unknown_platforms_and_missing_releases(clients, release_settings, monkeypatch):
    async def none(repo):
        return []

    monkeypatch.setattr(github_releases, "published", none)
    assert (await clients.get("/cli/latest", params={"os": "aix", "arch": "amd64"})).status_code == 404
    assert (await clients.get("/cli/latest", params={"os": "linux", "arch": "amd64"})).status_code == 404
    assert (await clients.get("/cli/latest", params={"channel": "nightly", "os": "linux", "arch": "amd64"})).status_code == 422


async def test_latest_download_redirects_to_the_platforms_archive(clients, release_settings, monkeypatch):
    release_settings.cli_stable_version = "1.2.0"
    release_settings.cli_beta_version = "1.3.0"
    r = await clients.get("/cli/latest/darwin-arm64", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "https://dl.example/v1.2.0/treg_1.2.0_darwin_arm64.tar.gz"
    r = await clients.get("/cli/latest/windows-amd64", params={"channel": "beta"}, follow_redirects=False)
    assert r.headers["location"] == "https://dl.example/v1.3.0/treg_1.3.0_windows_amd64.zip"
    assert (await clients.get("/cli/latest/aix-ppc64", follow_redirects=False)).status_code == 404
    assert (await clients.get("/cli/latest/darwin", follow_redirects=False)).status_code in (404, 422)


async def test_github_lookup_leaves_out_drafts_and_is_kept_a_while(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=[{"tag_name": "v1.3.0", "draft": True},
                                         {"tag_name": "v1.2.0-beta.1", "prerelease": True},
                                         {"tag_name": "v1.1.0"}])

    real = httpx.AsyncClient
    monkeypatch.setattr(github_releases.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    github_releases._cache.clear()
    want = [github_releases.Release("v1.2.0-beta.1", True), github_releases.Release("v1.1.0", False)]
    assert await github_releases.published("o/r") == want
    assert await github_releases.published("o/r") == want
    assert seen == ["/repos/o/r/releases"]


def test_only_a_release_from_1_0_0_is_a_binary_one():
    assert [cli_releases.is_binary_release(v) for v in ("1.0.0", "2.1.0-beta.1", "0.31.0", "", "x.y", "1.0/../")] == \
        [True, True, False, False, False, False]


async def test_beta_installer_is_the_binary_one(clients):
    plain = (await clients.get("/install.sh")).text
    beta = (await clients.get("/install.sh", params={"channel": "beta"})).text
    assert "uv tool install" in plain and "{CHANNEL}" not in plain
    assert "/cli/latest?channel=$CHANNEL" in beta and 'CHANNEL="${TREG_CHANNEL:-beta}"' in beta
    assert "{BASE}" not in beta and "{AD_LINK}" not in beta
    ps1 = (await clients.get("/install.ps1", params={"channel": "beta"})).text
    assert "{BASE}" not in ps1 and "'beta'" in ps1


def test_binary_installer_is_valid_posix_sh(tmp_path):
    from treg.routers.web import _WEB_DIR
    script = tmp_path / "install.sh"
    script.write_text((_WEB_DIR / "install-bin.sh").read_text())
    assert subprocess.run(["sh", "-n", str(script)]).returncode == 0
