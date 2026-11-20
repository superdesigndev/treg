"""What the CLI prints on a console that is not UTF-8, and whose answer it says a 503 is.
Driven through `main()`, except the hub folder reader, which runs under a non-UTF-8 locale."""
import io
import json
import os
import subprocess
import sys

import httpx
import pytest

from treg import cli


def _serve(monkeypatch, response: httpx.Response) -> list[dict]:
    sent: list[dict] = []

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, url, params=None, content=None, headers=None):
            sent.append({"method": method, "content": content, "headers": headers})
            return response

    monkeypatch.setattr(cli, "_client", lambda cfg, **k: FakeClient())
    monkeypatch.setattr(cli, "_load_config", lambda: {"token": "t", "base_url": "http://x"})
    return sent


def _saturated() -> httpx.Response:
    return httpx.Response(
        503, json={"detail": "treg's database pool is saturated — retry in a moment", "treg_saturated": True},
        headers={"Retry-After": "2"}, request=httpx.Request("POST", "http://x/call/x.search"))


def test_saturation_503_is_treg_not_the_provider(monkeypatch, capsys):
    _serve(monkeypatch, _saturated())
    with pytest.raises(SystemExit) as exc:
        cli.main(["call", "x.search"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "treg is overloaded, not the provider; retry in 2s" in err
    assert "the provider answered" not in err


def test_saturation_503_is_typed_in_the_json_envelope(monkeypatch, capsys):
    _serve(monkeypatch, _saturated())
    with pytest.raises(SystemExit):
        cli.main(["--json", "call", "x.search"])
    meta = json.loads(capsys.readouterr().out)["_treg"]
    assert meta == {"http_status": 503, "treg_saturated": True, "retry_after_seconds": 2}


def test_a_provider_503_is_still_the_providers(monkeypatch, capsys):
    _serve(monkeypatch, httpx.Response(503, json={"error": "busy"},
                                       request=httpx.Request("POST", "http://x/call/x.search")))
    with pytest.raises(SystemExit):
        cli.main(["call", "x.search"])
    assert "the provider answered; treg relayed it unchanged" in capsys.readouterr().err


def test_non_utf8_console_gets_utf8_instead_of_a_crash(monkeypatch):
    # A Windows pipe or Git Bash reports cp1252; `●` and non-Latin provider text raised before.
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    _serve(monkeypatch, httpx.Response(200, json={"name": "株式会社 ●"}))
    monkeypatch.setattr(sys, "stdout", console)
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))
    cli.main(["--json", "call", "x.search"])
    console.flush()
    assert json.loads(raw.getvalue().decode("utf-8"))["result"] == {"name": "株式会社 ●"}


def test_hub_folder_reads_as_utf8_under_a_non_utf8_locale(tmp_path):
    # A locale whose default encoding is not UTF-8 (here ASCII, on Windows cp1252).
    for name, text in {"recipe.json": "{}", "README.md": "# 名片 ●", "run.js": "// é", "data.csv": "名,值"}.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    env = {**os.environ, "LC_ALL": "C", "LANG": "C", "PYTHONCOERCECLOCALE": "0", "PYTHONUTF8": "0"}
    code = ("import json, sys; from treg import cli; "
            "print(json.dumps(cli._hub_read_folder(sys.argv[1]), ensure_ascii=True))")
    out = subprocess.run([sys.executable, "-c", code, str(tmp_path)], env=env,
                         capture_output=True, text=True, check=True).stdout
    body = json.loads(out)
    assert (body["readme"], body["script"], body["data"]) == ("# 名片 ●", "// é", "名,值")
