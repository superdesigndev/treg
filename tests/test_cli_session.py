"""Which agent task a CLI invocation belongs to, from a mocked process tree."""
import hashlib
import os
import time

import httpx
import pytest

from treg import cli, cli_analytics, cli_session
from treg.cli_session import Proc, resolve

CLAUDE = Proc(100, "2.1.296", 1)
CODEX = Proc(200, "codex", 2)
NESTED = [Proc(300, "zsh", 3), CODEX, Proc(250, "node", 2), Proc(150, "zsh", 1), CLAUDE]
INHERITED = {"CLAUDE_CODE_SESSION_ID": "claude-raw", "CLAUDE_PID": "100",
             "CODEX_THREAD_ID": "codex-raw"}


def _hash(agent, raw):
    return hashlib.sha256(f"{agent}:{raw}".encode()).hexdigest()[:16]


def test_nearest_agent_wins_over_inherited_variables(tmp_path):
    config = tmp_path / "config.json"
    assert resolve(config, INHERITED, NESTED) == cli_session.Session(_hash("codex", "codex-raw"), "codex")
    assert resolve(config, INHERITED, NESTED[2:]) == \
        cli_session.Session(_hash("claude-code", "claude-raw"), "claude-code")
    assert resolve(config, {**INHERITED, "TREG_SESSION_ID": "ci-run-7"}, NESTED).id == "ci-run-7"


def test_without_a_session_variable_the_agent_process_names_the_session(tmp_path):
    config = tmp_path / "config.json"
    assert resolve(config, {"GEMINI_CLI": "1"}, [Proc(9, "zsh", 6), Proc(8, "node", 5)]) == \
        cli_session.Session(_hash("gemini-cli", "8:5"), "gemini-cli")
    beside_tmux = resolve(config, {}, [Proc(9, "bash", 5), Proc(8, "tmux", 4)])
    assert beside_tmux.agent == "unknown" and beside_tmux == resolve(config, {}, [])


def test_idle_window_reuses_a_recent_id_and_mints_after_it(tmp_path):
    config = tmp_path / "config.json"
    first = resolve(config, {}, [])
    assert first.agent == "unknown" and resolve(config, {}, []) == first
    stale = time.time() - cli_session.IDLE_WINDOW_SECONDS - 1
    os.utime(tmp_path / "session-id", (stale, stale))
    assert resolve(config, {}, []) != first


def test_walk_never_raises(monkeypatch):
    monkeypatch.setattr(cli_session.os, "getppid", lambda: 1 / 0)
    assert cli_session._ancestors() == []


@pytest.fixture
def events(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(cli_session, "_ancestors", lambda: [CODEX])
    captured = []
    monkeypatch.setattr(cli_analytics, "track_command", lambda **kwargs: captured.append(kwargs))
    return captured


def test_usage_error_names_only_the_options(events, capsys):
    with pytest.raises(SystemExit) as raised:
        cli.main(["balance", "-Xsecret", "--token=secret", "secret"])
    assert raised.value.code == 2
    assert "unrecognized arguments: -Xsecret --token=secret secret" in capsys.readouterr().err
    (event,) = events
    assert event["command"] == "usage_error" and event["exit_code"] == 2
    assert event["detail"] == {"options": ["-X", "--token"]}


def test_catalog_get_records_its_form_and_refusal(events, monkeypatch):
    monkeypatch.setattr(cli, "_catalog_get", lambda *a, **k: cli._fail("unknown_endpoint", "no such"))
    with pytest.raises(SystemExit):
        cli.main(["catalog", "get", "acme.people.find"])
    (event,) = events
    assert event["command"] == "catalog_get"
    assert event["detail"] == {"tool": "acme.people.find", "error": "unknown_endpoint"}


def test_disabled_telemetry_sends_no_session_headers(monkeypatch):
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    with cli._client({"base_url": "http://127.0.0.1:1"}, auth=False) as client:
        assert "x-treg-session" not in client.headers and "x-treg-agent" not in client.headers
    monkeypatch.delenv("DO_NOT_TRACK")
    monkeypatch.setenv("TREG_TELEMETRY", "1")
    monkeypatch.setattr(cli_session, "_ancestors", lambda: [CODEX])
    monkeypatch.setenv("CODEX_THREAD_ID", "t")
    cli_session.reset()
    with cli._client({"base_url": "http://127.0.0.1:1"}, auth=False) as client:
        assert client.headers["x-treg-session"] == _hash("codex", "t")
        assert client.headers["x-treg-agent"] == "codex"
    cli_session.reset()


def test_an_awaited_call_records_the_last_answer_it_saw(events, monkeypatch):
    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, *a, **k):
            return httpx.Response(202, json={}, headers={"X-Treg-Async": "{}"})

        def get(self, *a, **k):
            return httpx.Response(404, json={})

    monkeypatch.setattr(cli, "_client", lambda cfg, **k: FakeClient())
    monkeypatch.setattr(cli, "await_async_task",
                        lambda descriptor, submission, call_fn, *a: call_fn("t", []) and {"code": 3})
    with pytest.raises(SystemExit):
        cli.main(["call", "x.video-gen.submit", "--await"])
    (event,) = events
    assert event["exit_code"] == 3 and event["detail"]["status_class"] == "4xx"
