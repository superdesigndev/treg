import gzip
import zlib
from dataclasses import replace

import httpx
import pytest
from starlette.responses import Response, StreamingResponse

from treg import mcp_output


async def _request(upstream, path="/call/example", method="GET", *, budget=mcp_output.STANDARD_BUDGET):
    transport = httpx.ASGITransport(app=mcp_output.BoundedCallResponse(upstream, budget=budget))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path)


@pytest.mark.parametrize(("headers", "name"), [
    ({"UsEr-AgEnT": "OpenAI-MCP/1.0.0"}, ""),
    ({"User-Agent": "Mozilla/5.0 (; compatible; Claude-User/1.0; )"}, ""),
    ({"Origin": "https://claude.ai"}, ""),
    ({}, " Claude-AI "),
])
def test_hosted_connectors_use_the_chat_sandbox_budget(headers, name):
    budget = mcp_output.budget_for(headers, name)
    assert budget is mcp_output.CHAT_SANDBOX_BUDGET
    assert (budget.text_bytes, budget.envelope_bytes, budget.media_bytes, budget.capture_bytes) == (
        192 * 1024, 224 * 1024, 720 * 1024, 384 * 1024)
    assert budget.compact_media_content


@pytest.mark.parametrize("ua", [
    "claude-code/2.1.0", "codex-mcp-client/0.160.1", "Cursor", "mcporter",
    "python-httpx/0.28", "node", "curl", "", "unknown",
])
def test_other_clients_use_the_standard_budget(ua):
    budget = mcp_output.budget_for(httpx.Headers({"User-Agent": ua}))
    assert budget is mcp_output.STANDARD_BUDGET
    assert (budget.text_bytes, budget.envelope_bytes, budget.media_bytes, budget.capture_bytes) == (
        2 * 1024 * 1024, 2304 * 1024, 3_932_160, 2 * 1024 * 1024)
    assert budget.media_bytes * 4 // 3 == 5 * 1024 * 1024
    assert not budget.compact_media_content


@pytest.mark.parametrize(("encoding", "compress"), [
    ("identity", bytes),
    ("gzip", gzip.compress),
    ("x-gzip", gzip.compress),
    ("  GZip  ", gzip.compress),
    ("deflate", zlib.compress),
    ("deflate", lambda body: zlib.compress(body, wbits=-zlib.MAX_WBITS)),
])
@pytest.mark.parametrize("size", [64, 256])
async def test_in_process_reader_bounds_decoded_text_before_httpx_collects_it(
    monkeypatch, encoding, compress, size,
):
    budget = replace(mcp_output.STANDARD_BUDGET, capture_bytes=128)
    monkeypatch.setattr(mcp_output, "PREVIEW_BYTES", 16)
    text = b'{"data":"' + b"A" * size + b'"}'
    raw = compress(text)
    consumed = []

    async def chunks():
        for i in range(0, len(raw), 11):
            yield raw[i:i + 11]
        consumed.append(True)

    upstream = StreamingResponse(chunks(), media_type="application/json",
                                 headers={"Content-Encoding": encoding, "X-Treg-Call-Id": "receipt"})
    result = await _request(upstream, budget=budget)
    assert consumed == [True] and result.headers["X-Treg-Call-Id"] == "receipt"
    assert "content-encoding" not in result.headers
    if size == 64:
        assert mcp_output.OMITTED_HEADER not in result.headers
        assert result.content == text
    else:
        assert result.headers[mcp_output.OMITTED_HEADER] == "1"
        receipt = result.json()
        assert receipt["omitted"] and receipt["size_bytes"] == len(raw)
        if encoding == "identity":
            assert receipt["head"] == text[:16].decode() and receipt["tail"] == text[-16:].decode()
        else:
            assert receipt["content_encoding"] == encoding.strip().lower() and "head" not in receipt


@pytest.mark.parametrize("raw", [
    gzip.compress(b"one") + gzip.compress(b"two"),
    gzip.compress(b"one")[:-1],
])
async def test_incomplete_or_concatenated_compressed_text_is_omitted(raw):
    result = await _request(Response(raw, media_type="text/plain", headers={"Content-Encoding": "gzip"}))
    assert result.json()["omitted"]


@pytest.mark.parametrize(("method", "status", "content_type", "encoding"), [
    ("GET", 204, "application/json", "gzip"),
    ("HEAD", 200, "video/mp4", "gzip"),
    ("GET", 304, "video/mp4", ""),
])
async def test_empty_responses_preserve_status_headers_and_body(method, status, content_type, encoding):
    headers = {"Content-Type": content_type, "Content-Length": "0"}
    if encoding:
        headers["Content-Encoding"] = encoding
    response = await _request(Response(status_code=status, headers=headers), method=method)
    assert response.status_code == status and response.content == b""
    assert response.headers == httpx.Headers(headers)


@pytest.mark.parametrize("status", [400, 502])
async def test_large_errors_keep_the_redacted_text_preview_without_archive_advice(status):
    raw = b"provider error: " + b"X" * mcp_output.MAX_TEXT_BYTES
    receipt = (await _request(Response(raw, status_code=status, media_type="text/plain"))).json()
    assert receipt["omitted"] and receipt["size_bytes"] == len(raw)
    assert receipt["head"].startswith("provider error: ") and receipt["tail"] == "X" * mcp_output.PREVIEW_BYTES
    assert receipt["note"] == "The error body exceeds the MCP budget and is not archived."


@pytest.mark.parametrize("location", ["https://example.invalid/movie.mp4", "https://@example.invalid/movie.mp4", ""])
async def test_unsupported_media_gives_an_honest_download_receipt(location):
    upstream = Response(bytes(range(64)), media_type="video/mp4", headers={"Content-Location": location})
    receipt = (await _request(upstream)).json()
    if location == "https://example.invalid/movie.mp4":
        assert receipt["content_location"] == location and "Use content_location" in receipt["note"]
    else:
        assert "content_location" not in receipt and "CLI or HTTP API" in receipt["note"]
    assert receipt["omitted"] and "head" not in receipt
    assert "not archived" in receipt["note"] and "call_result" not in receipt["note"]


async def test_unsupported_binary_drains_without_buffering_or_decompressing(monkeypatch):
    drained = []

    async def chunks():
        for _ in range(5):
            drained.append(True)
            yield b"x" * 2048

    def should_not_run(*args):
        pytest.fail("binary chunks must only be counted")

    monkeypatch.setattr(mcp_output, "memoryview", should_not_run, raising=False)
    monkeypatch.setattr(mcp_output, "_decompress", should_not_run)
    result = await _request(StreamingResponse(
        chunks(), media_type="video/mp4", headers={"Content-Encoding": "gzip"}))
    assert len(drained) == 5 and result.headers[mcp_output.OMITTED_HEADER] == "1"
    assert result.json()["size_bytes"] == 5 * 2048


@pytest.mark.parametrize("media_type", ["image/png", "audio/mpeg"])
@pytest.mark.parametrize("budget", [mcp_output.STANDARD_BUDGET, mcp_output.CHAT_SANDBOX_BUDGET])
async def test_media_capture_applies_the_profiles_raw_byte_limit(media_type, budget):
    raw = b"\xff" * budget.media_bytes
    result = await _request(Response(raw, media_type=media_type), budget=budget)
    assert result.content == raw and result.headers["content-type"] == media_type
    oversized = await _request(Response(raw + b"x", media_type=media_type), budget=budget)
    assert oversized.headers[mcp_output.OMITTED_HEADER] == "1"
    assert oversized.json()["size_bytes"] == budget.media_bytes + 1


async def test_svg_uses_the_text_capture_limit():
    raw = b"<svg>" + b" " * mcp_output.MAX_TEXT_BYTES + b"</svg>"
    receipt = (await _request(Response(raw, media_type="image/svg+xml"))).json()
    assert receipt["omitted"] and receipt["head"].startswith("<svg>") and receipt["tail"].endswith("</svg>")


async def test_a_no_mime_text_preview_survives_splitting_a_utf8_character(monkeypatch):
    budget = replace(mcp_output.STANDARD_BUDGET, capture_bytes=65)
    monkeypatch.setattr(mcp_output, "PREVIEW_BYTES", 15)
    text = "a" + "🦦" * 50 + "b"
    result = await _request(Response(text.encode()), budget=budget)
    assert result.json()["head"] == "a" + "🦦" * 3
    assert result.json()["tail"] == "🦦" * 3 + "b"


def test_previews_replace_invalid_utf8(monkeypatch):
    monkeypatch.setattr(mcp_output, "PREVIEW_BYTES", 4)
    raw = b"a\xffcd......a\xffcd"
    receipt = mcp_output.omitted_body(size=len(raw), mime="text/plain", status=200,
                                      path="/call/test", head=raw, tail=raw)
    assert receipt["head"] == receipt["tail"] == "a�cd"


@pytest.mark.parametrize("budget", [mcp_output.STANDARD_BUDGET, mcp_output.CHAT_SANDBOX_BUDGET])
async def test_task_submission_keeps_verifiable_bytes_outside_the_model_preview(budget):
    from treg.mcp import _team_body

    prefix = b'{"id":"job", "message":"'
    raw = prefix + b"x" * (mcp_output.SUBMISSION_BYTES - len(prefix) - 2) + b'"}'
    upstream = Response(raw, media_type="application/json", headers={"X-Treg-Async": "{}"})
    answer = await _request(upstream, budget=budget)
    assert len(raw) == 8 * 1024 * 1024 and answer.content == raw
    shown = _team_body(answer, budget=budget)
    assert shown["omitted"] and len(shown["tail"]) == len(shown["head"]) == mcp_output.PREVIEW_BYTES


@pytest.mark.parametrize(("kwargs", "expected"), [
    ({"status": 400}, "error body"),
    ({"textual": False, "has_location": True}, "Use content_location"),
    ({"textual": False}, "download it"),
    ({"spooled": True}, "fields parameter"),
    ({"may_be_archived": True}, "Call call_result(call_id, offset=0)"),
    ({"may_be_archived": False, "has_location": True}, "Use content_location"),
    ({"may_be_archived": False}, "fetch the full response"),
    ({}, "check whether a copy was archived"),
    ({"path": "/calls/id/result"}, "stored result exceeds"),
])
def test_omitted_note_reports_the_available_recovery(kwargs, expected):
    defaults = {"status": 200, "textual": True, "has_location": False, "path": "/call/test"}
    assert expected in mcp_output.omitted_note(**(defaults | kwargs))


async def test_archive_result_route_bypasses_capture():
    raw = b"x" * (mcp_output.CHAT_SANDBOX_BUDGET.capture_bytes + 1)
    result = await _request(Response(raw, media_type="text/plain"), "/calls/id/result",
                            budget=mcp_output.CHAT_SANDBOX_BUDGET)
    assert result.content == raw and mcp_output.OMITTED_HEADER not in result.headers


async def test_upstream_cannot_label_its_body_as_an_omission_receipt():
    from treg.mcp import _team_body

    result = await _request(Response(b'{"omitted":true}', media_type="application/json",
                                     headers={mcp_output.OMITTED_HEADER: "1"}))
    assert mcp_output.OMITTED_HEADER not in result.headers
    assert _team_body(result) == {"omitted": True}


async def test_nonfinite_json_number_can_still_be_shown_as_text():
    from treg.mcp import _team_body

    response = await _request(Response(b'{"value":NaN}', media_type="application/json"))
    assert _team_body(response) == '{"value":NaN}'


async def test_hosted_capture_allows_pretty_json_to_fit_after_compaction():
    from treg.mcp import _team_body
    from treg.jsontext import dumps_bytes

    budget = mcp_output.CHAT_SANDBOX_BUDGET
    body = {"data": ["x" * 32] * 5000}
    raw = dumps_bytes(body, indent=18)
    assert budget.text_bytes < len(raw) < budget.capture_bytes
    response = await _request(Response(raw, media_type="application/json"), budget=budget)
    assert _team_body(response, budget=budget) == body
