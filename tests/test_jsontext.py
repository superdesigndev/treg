"""A lone UTF-16 surrogate in an upstream's JSON (`"\\ud835"`, a JavaScript scraper truncating a
LinkedIn post mid-pair) must not cost the caller the answer. Byte paths keep the escape, so the
relay stays faithful; the MCP path replaces it, because its serializer cannot write one."""
import json

import httpx
import pytest

from treg import cli, jsontext

# One correctly paired mathematical-bold letter, then a high surrogate whose pair was cut off.
UPSTREAM = b'{"text":"\\ud835\\udc01old \\ud835 cut"}'
PARSED = json.loads(UPSTREAM)


def test_a_paired_surrogate_is_ordinary_text():
    assert PARSED["text"].startswith("\U0001d401old")


def test_dumps_writes_a_lone_surrogate_as_its_escape_instead_of_raising():
    text = jsontext.dumps(PARSED)
    assert "\\ud835" in text and json.loads(text) == PARSED
    assert jsontext.dumps_bytes(PARSED, separators=(",", ":")) == UPSTREAM


def test_dumps_keeps_non_ascii_readable_when_nothing_is_malformed():
    assert jsontext.dumps({"t": "Café 𝐁"}) == '{"t": "Café 𝐁"}'
    assert jsontext.dumps_bytes({"t": "Café"}) == '{"t": "Café"}'.encode()


def test_scrub_replaces_only_the_unpaired_surrogate():
    scrubbed = jsontext.scrub_surrogates({"k\ud835": [PARSED, 1, None]})
    assert scrubbed == {"k�": [{"text": "\U0001d401old � cut"}, 1, None]}
    json.dumps(scrubbed, ensure_ascii=False).encode("utf-8")


def test_the_scrub_gate_sees_only_surrogate_escapes():
    assert jsontext.may_hold_surrogates(UPSTREAM)
    assert jsontext.may_hold_surrogates(b'"\\uDC01"')
    assert not jsontext.may_hold_surrogates(b'{"t":"\\u00e9 \\ud7ff"}')


def test_json_call_prints_a_lone_surrogate_as_valid_utf8(monkeypatch, capsys):
    """`treg --json call` printed this body with ensure_ascii=False, which a real stdout refuses
    to encode: the caller got a traceback for a call it was charged for."""
    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, url, params=None, content=None, headers=None):
            return httpx.Response(200, content=UPSTREAM, headers={
                "content-type": "application/json", "X-Treg-Call-Id": "abc123",
                "X-Treg-Cost-Micro": "4000"})

    monkeypatch.setattr(cli, "_client", lambda cfg, **k: FakeClient())
    monkeypatch.setattr(cli, "_load_config", lambda: {"token": "t", "base_url": "http://x"})
    cli.main(["--json", "call", "harvestapi.linkedin.post.search"])
    out = capsys.readouterr().out
    out.encode("utf-8")
    assert json.loads(out) == {"result": PARSED, "_treg": {
        "http_status": 200, "call_id": "abc123", "charged_micro": 4000}}


def test_an_awaited_result_with_a_lone_surrogate_still_prints(monkeypatch, capsys):
    """`treg call --await` printed the terminal result to stderr with ensure_ascii=False (or str()
    for a scalar); a lone surrogate there raised after the paid task had finished."""
    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, url, params=None, content=None, headers=None):
            return httpx.Response(202, json={}, headers={"X-Treg-Async": "{}"})

    monkeypatch.setattr(cli, "_client", lambda cfg, **k: FakeClient())
    monkeypatch.setattr(cli, "_load_config", lambda: {"token": "t", "base_url": "http://x"})
    for value, shown in ((PARSED, '{"text": "\\ud835\\udc01old \\ud835 cut"}'),
                         (PARSED["text"], "\U0001d401old \\ud835 cut")):
        monkeypatch.setattr(cli, "await_async_task",
                            lambda *a, value=value, **k: {"result": value, "code": 0})
        with pytest.raises(SystemExit) as done:
            cli.main(["call", "x.video-gen.submit", "--await"])
        assert done.value.code == 0
        err = capsys.readouterr().err
        err.encode("utf-8")
        assert f"result: {shown}\n" in err
