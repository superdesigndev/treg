"""The registry's notice to an outdated CLI: worded on the server, printed as-is by the CLI."""
from __future__ import annotations

import httpx
import pytest
from httpx import AsyncClient

from treg import cli, client_notice
from treg.config import get_settings


@pytest.mark.parametrize(("version", "latest", "minimum", "expected"), [
    ("0.30.0", "", "", None),
    ("0.30.0", "0.30.0", "", None),
    ("0.31.1", "0.31.0", "0.25.0", None),
    ("0.30.0", "0.31.0", "", "treg 0.31.0 is available (you have 0.30.0). Run `treg update`."),
    ("0.9.0", "0.10", "", "treg 0.10 is available (you have 0.9.0). Run `treg update`."),
    ("0.22.0", "0.31.0", "0.25.0",
     "treg 0.22.0 is no longer supported and some commands may fail. Run `treg update` to install 0.31.0."),
    ("0.22.0", "", "0.25.0",
     "treg 0.22.0 is no longer supported and some commands may fail. Run `treg update` to install 0.25.0."),
    ("0.31.0.dev1", "0.32.0", "", None),
    ("", "0.31.0", "0.25.0", None),
    ("0.30.0", "not-a-version", "", None),
    ("0.\u00b2", "0.31.0", "", None),
    pytest.param("1" * 5000, "0.31.0", "", None, id="oversized"),
])
def test_notice_wording(version, latest, minimum, expected):
    assert client_notice.notice(version, latest=latest, minimum=minimum) == expected


@pytest.fixture
def released(monkeypatch):
    monkeypatch.setattr(get_settings(), "cli_latest_version", "0.31.0", raising=False)
    monkeypatch.setattr(get_settings(), "cli_min_version", "0.25.0", raising=False)


async def test_an_outdated_cli_gets_the_notice_on_any_answer(clients: AsyncClient, released):
    for path in ("/meta", "/no-such-route"):
        r = await clients.get(path, headers={"X-Treg-Client-Version": "0.22.0"})
        assert "no longer supported" in r.headers[client_notice.NOTICE_HEADER]


async def test_no_notice_for_a_current_cli_a_browser_or_an_unset_release(clients: AsyncClient, released,
                                                                         monkeypatch):
    assert client_notice.NOTICE_HEADER not in (await clients.get(
        "/meta", headers={"X-Treg-Client-Version": "0.31.0"})).headers
    assert client_notice.NOTICE_HEADER not in (await clients.get("/meta")).headers
    monkeypatch.setattr(get_settings(), "cli_latest_version", "", raising=False)
    monkeypatch.setattr(get_settings(), "cli_min_version", "", raising=False)
    assert client_notice.NOTICE_HEADER not in (await clients.get(
        "/meta", headers={"X-Treg-Client-Version": "0.1.0"})).headers


async def test_a_malformed_version_gets_no_notice_and_no_error(clients: AsyncClient, released):
    for raw in (b"0.\xb2", b"1" * 5000):
        r = await clients.get("/meta", headers={"X-Treg-Client-Version": raw})
        assert r.status_code == 200 and client_notice.NOTICE_HEADER not in r.headers


def _answer(text: str) -> httpx.Response:
    return httpx.Response(200, headers={client_notice.NOTICE_HEADER: text})


@pytest.fixture
def fresh_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "not-yet" / "config.json")
    monkeypatch.setattr(cli, "_client_notice_seen", False)
    monkeypatch.setattr(cli, "_JSON_OVERRIDE", False)


def test_the_cli_prints_a_notice_once_a_day(fresh_cli, monkeypatch, capsys):
    cli._print_client_notice(_answer("treg 0.31.0 is available"))
    cli._print_client_notice(_answer("treg 0.31.0 is available"))
    assert capsys.readouterr().err.count("treg 0.31.0 is available") == 1

    monkeypatch.setattr(cli, "_client_notice_seen", False)  # the next command, same day
    cli._print_client_notice(_answer("treg 0.31.0 is available"))
    assert capsys.readouterr().err == ""

    monkeypatch.setattr(cli, "_client_notice_seen", False)  # a newer release changes the notice
    cli._print_client_notice(_answer("treg 0.32.0 is available"))
    assert "treg 0.32.0 is available" in capsys.readouterr().err


def test_the_cli_stays_silent_under_json_and_without_a_notice(fresh_cli, monkeypatch, capsys):
    cli._print_client_notice(httpx.Response(200))
    monkeypatch.setattr(cli, "_JSON_OVERRIDE", True)
    cli._print_client_notice(_answer("treg 0.31.0 is available"))
    assert capsys.readouterr().err == ""
