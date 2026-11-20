"""Spooled settlement evidence: a large metered 2xx is read to disk, never into RAM.

`_spool_response` keeps `_buffer_response`'s contract (complete evidence before headers, never a
prefix, an oversized body fails uncharged) for endpoints that declare `spooled_response`. These
tests pin the byte-exact replay, the evidence projection, and that every path returns the file
and its budget: success, oversize, a busy process, an upstream failure, cancellation and a
response that is dropped without being closed.
"""

import asyncio
import gc
import json

import pytest

from treg.application.call import settle
from treg.application.call.settle import _spool_response
from treg.application.call.types import GatewayFailed, UpstreamResponse
from treg.config import get_settings

MB = 1024 * 1024
IMAGE = b"A" * (3 * MB)  # base64 is quote- and escape-free, like a real inlineData payload
USAGE = {"promptTokenCount": 17, "candidatesTokenCount": 1229,
         "candidatesTokensDetails": [{"modality": "IMAGE", "tokenCount": 1120}]}
BODY = (b'{"candidates":[{"content":{"parts":[{"inlineData":{"mimeType":"image/jpeg","data":"'
        + IMAGE + b'"}}]},"finishReason":"STOP"}],"usageMetadata":'
        + json.dumps(USAGE).encode() + b',"modelVersion":"gemini-3-pro-image"}')


@pytest.fixture(autouse=True)
def fresh_budget(monkeypatch):
    monkeypatch.setattr(settle, "_spool_in_use", 0)
    monkeypatch.setattr(settle, "_spool_gate", None)
    yield
    gc.collect()
    assert settle._spool_in_use == 0


def _upstream(body: bytes, *, chunk: int = 64 * 1024, fail_after: int | None = None,
              declared: int | None = None):
    state = {"closes": 0, "read": 0}

    async def stream():
        for start in range(0, len(body), chunk):
            if fail_after is not None and start >= fail_after:
                raise ConnectionResetError("synthetic upstream reset")
            state["read"] += 1
            yield body[start:start + chunk]

    async def close():
        state["closes"] += 1

    headers = [(b"content-type", b"application/json")]
    if declared is not None:
        headers.append((b"content-length", str(declared).encode()))
    return UpstreamResponse(200, tuple(headers), stream(), close), state


async def _drain(response: UpstreamResponse) -> bytes:
    return b"".join([chunk async for chunk in response.body_stream])


async def test_replay_is_byte_exact_and_evidence_is_the_declared_keys():
    upstream, state = _upstream(BODY, declared=len(BODY))
    response, evidence, size = await _spool_response(upstream, ("usageMetadata", "missing"))
    assert state["closes"] == 1  # the upstream is done before headers go out
    assert size == len(BODY)
    assert json.loads(evidence) == {"usageMetadata": USAGE}
    assert dict(response.raw_headers)[b"content-length"] == str(len(BODY)).encode()
    assert settle._spool_in_use == len(BODY)
    assert await _drain(response) == BODY
    await response.close()
    await response.close()  # close is idempotent: the router may close twice
    assert settle._spool_in_use == 0


@pytest.mark.parametrize("body", [b"not json", b'["a list"]', b'{"truncated": "'])
async def test_a_body_that_is_not_a_json_object_yields_no_evidence(body):
    upstream, _ = _upstream(body)
    response, evidence, size = await _spool_response(upstream, ("usageMetadata",))
    assert evidence == b"" and size == len(body)
    assert await _drain(response) == body
    await response.close()


async def test_an_oversized_body_fails_uncharged_and_returns_its_budget(monkeypatch):
    monkeypatch.setattr(get_settings(), "spool_max_bytes", 2 * MB)
    upstream, state = _upstream(BODY)
    with pytest.raises(GatewayFailed) as failure:
        await _spool_response(upstream, ("usageMetadata",))
    assert failure.value.kind == "response_buffer_limit"
    assert "not charged" in failure.value.detail["message"]
    assert state["closes"] == 1 and settle._spool_in_use == 0


async def test_a_busy_process_refuses_instead_of_filling_the_disk(monkeypatch):
    monkeypatch.setattr(get_settings(), "spool_budget_bytes", 4 * MB)
    first, _ = _upstream(BODY)
    held, _, _ = await _spool_response(first, ("usageMetadata",))
    second, state = _upstream(BODY)
    with pytest.raises(GatewayFailed) as failure:
        await _spool_response(second, ("usageMetadata",))
    assert "too many large responses" in failure.value.detail["message"]
    assert state["closes"] == 1 and settle._spool_in_use == len(BODY)
    await held.close()
    third, _ = _upstream(BODY)  # the budget came back with the first close
    again, _, _ = await _spool_response(third, ("usageMetadata",))
    await again.close()


async def test_an_upstream_reset_mid_body_releases_everything():
    upstream, state = _upstream(BODY, fail_after=MB)
    with pytest.raises(ConnectionResetError):
        await _spool_response(upstream, ("usageMetadata",))
    assert state["closes"] == 1 and settle._spool_in_use == 0


async def test_cancellation_while_spooling_releases_everything():
    started = asyncio.Event()
    state = {"closes": 0}

    async def stream():
        yield b"x" * MB
        started.set()
        await asyncio.sleep(3600)
        yield b""

    async def close():
        state["closes"] += 1

    task = asyncio.create_task(_spool_response(UpstreamResponse(200, (), stream(), close), ()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert state["closes"] == 1 and settle._spool_in_use == 0


async def test_a_dropped_response_still_returns_its_budget():
    upstream, _ = _upstream(BODY)
    response, _, _ = await _spool_response(upstream, ("usageMetadata",))
    assert settle._spool_in_use == len(BODY)
    del response
    gc.collect()
    assert settle._spool_in_use == 0


async def test_parsing_is_gated_per_process(monkeypatch):
    monkeypatch.setattr(get_settings(), "spool_parse_concurrency", 1)
    active = peak = 0
    original = settle._spool_evidence

    def tracked(file, keys):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            return original(file, keys)
        finally:
            active -= 1

    monkeypatch.setattr(settle, "_spool_evidence", tracked)
    results = await asyncio.gather(*[
        _spool_response(_upstream(BODY)[0], ("usageMetadata",)) for _ in range(3)])
    assert peak == 1
    for response, evidence, _ in results:
        assert json.loads(evidence) == {"usageMetadata": USAGE}
        await response.close()


async def test_a_declared_length_is_admitted_or_refused_whole(monkeypatch):
    """Concurrent large answers must not all claim budget piecemeal and stall half-read: with a
    Content-Length the whole body is admitted up front, or refused before a byte is read."""
    monkeypatch.setattr(get_settings(), "spool_budget_bytes", len(BODY) + MB)
    first, _ = _upstream(BODY, declared=len(BODY))
    second, second_state = _upstream(BODY, declared=len(BODY))
    held, _, _ = await _spool_response(first, ("usageMetadata",))
    with pytest.raises(GatewayFailed) as failure:
        await _spool_response(second, ("usageMetadata",))
    assert "temporary" in failure.value.detail["message"]
    assert second_state["read"] == 0 and second_state["closes"] == 1
    await held.close()


async def test_a_declared_length_over_the_cap_is_refused_before_reading(monkeypatch):
    monkeypatch.setattr(get_settings(), "spool_max_bytes", 2 * MB)
    upstream, state = _upstream(BODY, declared=len(BODY))
    with pytest.raises(GatewayFailed):
        await _spool_response(upstream, ("usageMetadata",))
    assert state["read"] == 0 and state["closes"] == 1


async def test_a_temp_file_that_cannot_be_created_still_closes_the_upstream(monkeypatch):
    def no_disk(*args, **kwargs):
        raise OSError("synthetic: no space left on device")

    monkeypatch.setattr(settle.tempfile, "TemporaryFile", no_disk)
    upstream, state = _upstream(BODY, declared=len(BODY))
    with pytest.raises(OSError):
        await _spool_response(upstream, ("usageMetadata",))
    assert state["closes"] == 1


async def test_evidence_keeps_a_success_leaf_without_its_siblings():
    """A fixed-price answer settles on its `expect` leaf: `candidates.0.finishReason` survives, the
    inline media beside it does not."""
    upstream, _ = _upstream(BODY, declared=len(BODY))
    response, evidence, _ = await _spool_response(
        upstream, ("usageMetadata", "candidates.0.finishReason"))
    assert json.loads(evidence) == {"usageMetadata": USAGE,
                                    "candidates": [{"finishReason": "STOP"}]}
    await response.close()


def test_spool_evidence_paths_come_from_usage_and_the_success_rule():
    from treg.application.call.resolve import _spool_evidence_paths
    usage = {"settle": "usage", "usage": {"unit": "usd", "terms": [
        {"path": "usageMetadata.promptTokenCount", "rate": 1}]}}
    assert _spool_evidence_paths({"spooled_response": True}, usage) == ("usageMetadata",)
    rule = {"spooled_response": True,
            "expect": {"json_path": "candidates.0.finishReason", "equals": "STOP"}}
    assert _spool_evidence_paths(rule, {"type": "per_success", "value": 0.08}) == (
        "candidates.0.finishReason",)
    assert _spool_evidence_paths({}, usage) == ()
