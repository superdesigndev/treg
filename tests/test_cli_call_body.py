"""What `treg call --data/--file` sends must be what the caller meant.

Three ways it was not: curl-style `--data @body.json` sent the literal string "@body.json"; a JSON
body mangled by PowerShell quoting (quotes stripped) went out as-is, unlabelled, and the provider
answered "POST Data Is Empty"/"invalid payload"; and a body file PowerShell wrote as UTF-16 (or
with a UTF-8 byte-order mark) was sent as those bytes, which no JSON API parses.
"""
import codecs
import io
import json
from types import SimpleNamespace

import httpx
import pytest

from treg import cli


def _capture(monkeypatch):
    seen: list[httpx.Request] = []

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, url, params=None, content=None, headers=None):
            seen.append(httpx.Request(method, httpx.URL("http://t").join(url), params=params,
                                      content=content, headers=headers))
            return httpx.Response(200, json={})

    monkeypatch.setattr(cli, "_client", lambda cfg, **k: FakeClient())
    monkeypatch.setattr(cli, "_show", lambda resp: None)
    return seen


def _args(data=None, file=None, content_type=None):
    return SimpleNamespace(target="dataforseo.google.keywords.volume", path="", method="POST",
                           query=[], data=data, file=file, content_type=content_type, header=[],
                           upload=[])


BODY = [{"keywords": ["café"], "location_code": 2250}]


def test_data_at_file_reads_the_file(monkeypatch, tmp_path):
    f = tmp_path / "body.json"
    f.write_text(json.dumps(BODY), encoding="utf-8")
    seen = _capture(monkeypatch)
    cli.cmd_call(_args(data=f"@{f}"), {})
    assert json.loads(seen[0].content) == BODY
    assert seen[0].headers["content-type"] == "application/json"


def test_data_at_dash_reads_stdin(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps(BODY).encode())))
    seen = _capture(monkeypatch)
    cli.cmd_call(_args(data="@-"), {})
    assert json.loads(seen[0].content) == BODY


def test_data_at_missing_file_is_refused(monkeypatch, tmp_path):
    seen = _capture(monkeypatch)
    with pytest.raises(SystemExit) as e:
        cli.cmd_call(_args(data=f"@{tmp_path / 'nope.json'}"), {})
    assert "--file" in str(e.value)
    assert not seen


def test_json_mangled_by_shell_quoting_is_refused(monkeypatch):
    # What PowerShell passes for --data '[{"keywords":["coffee"]}]': the inner quotes are gone.
    seen = _capture(monkeypatch)
    with pytest.raises(SystemExit) as e:
        cli.cmd_call(_args(data="[{keywords:[coffee]}]"), {})
    assert "not valid JSON" in str(e.value) and "--content-type" in str(e.value)
    assert not seen


def test_non_json_body_with_explicit_type_is_sent_as_is(monkeypatch):
    seen = _capture(monkeypatch)
    cli.cmd_call(_args(data="{{template}}", content_type="text/plain"), {})
    assert seen[0].content == b"{{template}}"


def test_plain_text_body_is_untouched(monkeypatch):
    seen = _capture(monkeypatch)
    cli.cmd_call(_args(data="hello world"), {})
    assert seen[0].content == b"hello world" and "content-type" not in seen[0].headers


@pytest.mark.parametrize("encoding,bom", [("utf-16-le", codecs.BOM_UTF16_LE),
                                          ("utf-16-be", codecs.BOM_UTF16_BE),
                                          ("utf-8", codecs.BOM_UTF8)])
def test_json_file_with_byte_order_mark_is_sent_as_utf8(monkeypatch, tmp_path, capsys, encoding, bom):
    f = tmp_path / "body.json"
    f.write_bytes(bom + json.dumps(BODY, ensure_ascii=False).encode(encoding))
    seen = _capture(monkeypatch)
    cli.cmd_call(_args(file=str(f)), {})
    assert seen[0].content == json.dumps(BODY, ensure_ascii=False).encode()
    assert seen[0].headers["content-type"] == "application/json"
    assert "byte-order mark" in capsys.readouterr().err


def test_binary_file_is_never_rewritten(monkeypatch, tmp_path):
    f = tmp_path / "blob.bin"
    raw = codecs.BOM_UTF16_LE + b"\x00\x01\x02not json"
    f.write_bytes(raw)
    seen = _capture(monkeypatch)
    cli.cmd_call(_args(file=str(f)), {})
    assert seen[0].content == raw
