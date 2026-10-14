"""Text embeddings from an OpenAI-compatible `/embeddings` API (OpenRouter by default).

Infra: this module knows the wire format and nothing about search. Like `infra.judge` it never
raises: a timeout, a non-200, a malformed body or a vector of the wrong size all come back as
`vectors=None` with a reason, and the caller answers without the semantic channel.

A query's vector is cached in-process by (model, folded query): the same text typed twice, or by two
people, is embedded once. Bounded and TTL'd like the judge's cache. Not `infra.kv`: that store
serves one tenant until it has proved itself.
"""
from __future__ import annotations

import logging
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass

import httpx

log = logging.getLogger("treg.embed")

_CACHE_MAX = 5000
_CACHE_TTL_S = 3600.0
_cache: "OrderedDict[tuple[str, str], tuple[float, list[float]]]" = OrderedDict()


@dataclass(frozen=True)
class Embedding:
    vectors: list[list[float]] | None   # one per input, in order; None = no answer
    ms: int
    tokens: int | None = None
    error: str | None = None            # timeout | http_<status> | bad_body | dim_<n> | <ExceptionType>
    cached: bool = False

    @property
    def vector(self) -> list[float] | None:
        return self.vectors[0] if self.vectors else None


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    return " ".join("".join(c for c in decomposed if not unicodedata.combining(c)).lower().split())


def clear_cache() -> None:
    _cache.clear()


async def embed(texts: list[str], *, api_key: str, model: str, url: str, timeout_s: float,
                dim: int | None = None, transport: httpx.AsyncBaseTransport | None = None) -> Embedding:
    """Vectors for `texts`, in order. Never raises. `dim` refuses an answer of another size (a model
    that changed under the same name must not be mixed with vectors already made)."""
    if not texts:
        return Embedding(vectors=[], ms=0)
    t0 = time.perf_counter()

    def ms() -> int:
        return int((time.perf_counter() - t0) * 1000)
    try:
        async with httpx.AsyncClient(timeout=timeout_s, transport=transport) as client:
            r = await client.post(url, json={"model": model, "input": texts},
                                  headers={"Authorization": f"Bearer {api_key}"})
        if r.status_code != 200:
            return Embedding(vectors=None, ms=ms(), error=f"http_{r.status_code}")
        body = r.json()
        data = sorted(body.get("data") or [], key=lambda d: d.get("index", 0))
        vectors = [[float(x) for x in d["embedding"]] for d in data]
        if len(vectors) != len(texts) or not vectors[0]:
            return Embedding(vectors=None, ms=ms(), error="bad_body")
        sizes = {len(v) for v in vectors}
        if len(sizes) != 1 or (dim is not None and sizes != {dim}):
            return Embedding(vectors=None, ms=ms(), error=f"dim_{min(sizes)}")
        return Embedding(vectors=vectors, ms=ms(), tokens=(body.get("usage") or {}).get("prompt_tokens"))
    except httpx.TimeoutException:
        return Embedding(vectors=None, ms=ms(), error="timeout")
    except (KeyError, TypeError, ValueError):
        return Embedding(vectors=None, ms=ms(), error="bad_body")
    except Exception as exc:  # noqa: BLE001 - no vector is a find without its semantic channel, never a failed one
        log.warning("embedding failed: %s", type(exc).__name__)
        return Embedding(vectors=None, ms=ms(), error=type(exc).__name__)


async def embed_query(text: str, *, api_key: str, model: str, url: str, timeout_s: float,
                      dim: int | None = None, transport: httpx.AsyncBaseTransport | None = None) -> Embedding:
    """One query's vector, from the cache when the same folded text was embedded within the hour."""
    key = (model, _fold(text))
    hit = _cache.get(key)
    if hit is not None:
        stamp, vector = hit
        if time.monotonic() - stamp <= _CACHE_TTL_S and (dim is None or len(vector) == dim):
            _cache.move_to_end(key)
            return Embedding(vectors=[vector], ms=0, cached=True)
        _cache.pop(key, None)
    e = await embed([key[1]], api_key=api_key, model=model, url=url, timeout_s=timeout_s,
                    dim=dim, transport=transport)
    if e.vector is not None:
        _cache[key] = (time.monotonic(), e.vector)
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return e
