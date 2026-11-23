import gzip
import zlib

import httpx
import pytest
from starlette.responses import Response, StreamingResponse

from treg import mcp_output


async def _request(upstream, path="/call/example", method="GET"):
    transport = httpx.ASGITransport(app=mcp_output.BoundedCallResponse(upstream))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path)


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
    monkeypatch.setattr(mcp_output, "TEXT_CAPTURE_BYTES", 128)
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
    result = await _request(upstream)
    assert consumed == [True] and result.headers["X-Treg-Call-Id"] == "receipt"
    assert "content-encoding" not in result.headers
    if size == 64:
        assert result.content == text
    else:
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
    raw = b"provider error: " + b"X" * mcp_output.TEXT_CAPTURE_BYTES
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


@pytest.mark.parametrize("media_type", ["image/png", "audio/mpeg"])
async def test_media_capture_keeps_more_than_256_kib(media_type):
    raw = b"\xff" * (300 * 1024)
    result = await _request(Response(raw, media_type=media_type))
    assert result.content == raw and result.headers["content-type"] == media_type


async def test_svg_uses_the_text_capture_limit():
    raw = b"<svg>" + b" " * mcp_output.TEXT_CAPTURE_BYTES + b"</svg>"
    receipt = (await _request(Response(raw, media_type="image/svg+xml"))).json()
    assert receipt["omitted"] and receipt["head"].startswith("<svg>") and receipt["tail"].endswith("</svg>")


async def test_a_no_mime_text_preview_survives_splitting_a_utf8_character(monkeypatch):
    monkeypatch.setattr(mcp_output, "TEXT_CAPTURE_BYTES", 64)
    monkeypatch.setattr(mcp_output, "PREVIEW_BYTES", 15)
    text = "评" * 50
    result = await _request(Response(text.encode()))
    assert result.json()["head"] == result.json()["tail"] == "评" * 5


async def test_task_submission_keeps_verifiable_bytes_outside_the_model_preview():
    from treg.mcp import _team_body

    prefix = b'{"id":"job", "message":"'
    raw = prefix + b"x" * (mcp_output.SUBMISSION_BYTES - len(prefix) - 2) + b'"}'
    upstream = Response(raw, media_type="application/json", headers={"X-Treg-Async": "{}"})
    answer = await _request(upstream)
    assert len(raw) == 8 * 1024 * 1024 and answer.content == raw
    shown = _team_body(answer)
    assert shown["omitted"] and len(shown["tail"]) == len(shown["head"]) == mcp_output.PREVIEW_BYTES


async def test_nonfinite_json_number_can_still_be_shown_as_text():
    from treg.mcp import _team_body

    response = await _request(Response(b'{"value":NaN}', media_type="application/json"))
    assert _team_body(response) == '{"value":NaN}'
