"""The relevance judge — TypeSafe's System One API (Jev), asked one yes/no per candidate.

Infra: this module knows the wire format and nothing about search. It returns probabilities in
candidate order or a reason it could not, and it never raises — a judge that is down is a judge
that abstains, and the caller (`application.search_experiment`) serves the baseline. The single
request carries every candidate as one `state` and one Noul question per row; the API scores
them in parallel, so the page costs one round trip regardless of `len(candidates)`.

Answers are cached in-process by (model, query, candidate ids): an agent that repeats a query — or
several agents asking the same thing — pays the judge once. Bounded and TTL'd because the catalog
changes and the process is long-lived.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass

import httpx

log = logging.getLogger("treg.judge")

_CACHE_MAX = 5000
_CACHE_TTL_S = 3600.0
_cache: "OrderedDict[str, tuple[float, tuple[list[float], dict[str, float]]]]" = OrderedDict()


@dataclass(frozen=True)
class Judgement:
    probs: list[float] | None       # one per candidate, in order; None = the judge abstained
    ms: int
    tokens_in: int | None = None
    tokens_out: int | None = None
    error: str | None = None        # timeout | http_<status> | <ExceptionType>; None when answered
    cached: bool = False
    extra: dict[str, float] | None = None   # answers to the caller's `extra` questions; None = abstained


def candidate_view(ep: dict, capability_text: str) -> dict:
    """What the judge reads about one endpoint — the row's own words, bounded, never the schema."""
    return {
        "id": ep["id"],
        "name": ep.get("name") or "",
        "summary": (ep.get("summary") or "")[:160],
        "capability": capability_text[:120],
        "platform": ep.get("platform") or "",
    }


def _question(i: int, criteria: dict | None = None) -> dict:
    q = {"type": "noul", "instructions": (
        f"Calling the API endpoint `candidates[{i}]` would directly accomplish, or be a necessary "
        f"step of, the task described in `task`, on the platform or data source the task implies.")}
    if criteria:
        q["criteria"] = criteria
    return q


def _cache_key(model: str, query: str, ids: list[str], criteria: dict | None, extra: dict | None) -> str:
    return hashlib.sha256(json.dumps([model, query.strip().lower(), ids, criteria, extra],
                                     sort_keys=True).encode()).hexdigest()


def _cache_get(key: str) -> tuple[list[float], dict[str, float]] | None:
    hit = _cache.get(key)
    if hit is None:
        return None
    stamp, probs = hit
    if time.monotonic() - stamp > _CACHE_TTL_S:
        _cache.pop(key, None)
        return None
    _cache.move_to_end(key)
    return probs


def _cache_put(key: str, answer: tuple[list[float], dict[str, float]]) -> None:
    _cache[key] = (time.monotonic(), answer)
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


def clear_cache() -> None:
    _cache.clear()


async def judge(query: str, candidates: list[dict], *, api_key: str, model: str, url: str,
                timeout_s: float, criteria: dict | None = None, extra: dict[str, dict] | None = None,
                transport: httpx.AsyncBaseTransport | None = None) -> Judgement:
    """Probabilities that each candidate accomplishes `query`. Never raises.

    `criteria` (Noul `true`/`false` descriptions) is attached to every candidate question. `extra`
    maps ids to further questions about the same state (the query alone, say); they ride in the same
    request and come back as `Judgement.extra`. Neither changes what a caller passing none sends."""
    if not candidates:
        return Judgement(probs=[], ms=0, extra={})
    ids = [c["id"] for c in candidates]
    key = _cache_key(model, query, ids, criteria, extra)
    cached = _cache_get(key)
    if cached is not None:
        return Judgement(probs=cached[0], ms=0, cached=True, extra=cached[1])
    questions = {f"c{i}": _question(i, criteria) for i in range(len(candidates))}
    questions.update({f"x_{k}": q for k, q in (extra or {}).items()})
    body = {
        "state": {"task": query, "candidates": [{"i": i, **c} for i, c in enumerate(candidates)]},
        "model": model,
        "questions": questions,
    }
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout_s, transport=transport) as client:
            r = await client.post(url, json=body, headers={"Authorization": f"Bearer {api_key}"})
        ms = int((time.perf_counter() - t0) * 1000)
        if r.status_code != 200:
            return Judgement(probs=None, ms=ms, error=f"http_{r.status_code}")
        data = r.json()
        answers = data.get("answers") or {}
        probs = [float(answers[f"c{i}"]["noul"]) for i in range(len(candidates))]
        extras = {k: float(answers[f"x_{k}"]["noul"]) for k in (extra or {})}
        usage = data.get("usage") or {}
        _cache_put(key, (probs, extras))
        return Judgement(probs=probs, ms=ms, tokens_in=usage.get("input_tokens"),
                         tokens_out=usage.get("output_tokens"), extra=extras)
    except httpx.TimeoutException:
        return Judgement(probs=None, ms=int((time.perf_counter() - t0) * 1000), error="timeout")
    except Exception as exc:  # noqa: BLE001 — an abstaining judge, never a failed search
        log.warning("relevance judge failed: %s", exc)
        return Judgement(probs=None, ms=int((time.perf_counter() - t0) * 1000),
                         error=type(exc).__name__)
