"""The embeddings client (infra.embed): answers in input order, abstains instead of raising, and caches
a query's vector by its folded text."""
from __future__ import annotations

import json

import httpx

from treg.infra import embed as embed_infra

KW = dict(api_key="k", model="voyageai/voyage-4-lite", url="https://embed.test/v1/embeddings", timeout_s=1.0)


def _transport(handler):
    return httpx.MockTransport(handler)


def _answer(vectors, tokens=7):
    return httpx.Response(200, json={"data": [{"index": i, "embedding": v} for i, v in reversed(list(enumerate(vectors)))],
                                     "usage": {"prompt_tokens": tokens}})


async def test_vectors_come_back_in_input_order_with_usage():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer k"
        return _answer([[1.0, 0.0], [0.0, 1.0]])

    e = await embed_infra.embed(["a", "b"], transport=_transport(handler), **KW)
    assert e.vectors == [[1.0, 0.0], [0.0, 1.0]] and e.tokens == 7 and e.error is None
    assert seen == [{"model": "voyageai/voyage-4-lite", "input": ["a", "b"]}]


async def test_it_abstains_on_timeout_http_error_bad_body_and_a_wrong_size():
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    cases = [
        (timeout, None, "timeout"),
        (lambda r: httpx.Response(502, text="bad gateway"), None, "http_502"),
        (lambda r: httpx.Response(200, json={"data": []}), None, "bad_body"),
        (lambda r: httpx.Response(200, text="not json"), None, "bad_body"),
        (lambda r: _answer([[1.0, 0.0, 0.0]]), 2, "dim_3"),
    ]
    for handler, dim, error in cases:
        e = await embed_infra.embed(["a"], transport=_transport(handler), dim=dim, **KW)
        assert e.vectors is None and e.error == error


async def test_a_query_is_embedded_once_per_folded_text():
    embed_infra.clear_cache()
    calls = []

    def handler(request):
        calls.append(json.loads(request.content)["input"])
        return _answer([[0.6, 0.8]])

    first = await embed_infra.embed_query("Café  Reviews", transport=_transport(handler), **KW)
    again = await embed_infra.embed_query("cafe reviews", transport=_transport(handler), **KW)
    assert first.vector == again.vector == [0.6, 0.8] and again.cached and not first.cached
    assert calls == [["cafe reviews"]]
    # a failed answer is not cached
    await embed_infra.embed_query("other", transport=_transport(lambda r: httpx.Response(500)), **KW)
    await embed_infra.embed_query("other", transport=_transport(handler), **KW)
    assert calls[-1] == ["other"]
    # another model is another vector
    await embed_infra.embed_query("cafe reviews", transport=_transport(handler), **{**KW, "model": "m2"})
    assert len(calls) == 3
