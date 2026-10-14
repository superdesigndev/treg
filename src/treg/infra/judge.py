"""The relevance judge — TypeSafe's System One API (Jev), asked one yes/no per candidate.

Infra: this module knows the wire format and nothing about search. It returns probabilities in
candidate order or a reason it could not, and it never raises — a judge that is down is a judge
that abstains, and the caller (`application.search_experiment`) serves the baseline. The single
request carries every candidate as one `state` and one Noul question per row; the API scores
them in parallel, so the page costs one round trip regardless of `len(candidates)`.

A candidate is an endpoint (`candidate_view`) or a whole job (`job_view`, the find page's unit of
recall); each gets its own question wording. Extra questions about the same state ride in the same
request: a Noul answers with a probability, a Choice with the option picked, its confidence and
every option's probability.

Answers are cached in-process by (model, query, candidate ids, questions): an agent that repeats a
query — or several agents asking the same thing — pays the judge once. Bounded and TTL'd because the
catalog changes and the process is long-lived.
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
_cache: "OrderedDict[str, tuple[float, tuple[list[float], dict]]]" = OrderedDict()


@dataclass(frozen=True)
class Judgement:
    probs: list[float] | None       # one per candidate, in order; None = the judge abstained
    ms: int
    tokens_in: int | None = None
    tokens_out: int | None = None
    error: str | None = None        # timeout | http_<status> | <ExceptionType>; None when answered
    cached: bool = False
    # answers to the caller's `extra` questions: a Noul's probability, or a Choice's
    # {"choice", "confidence", "probabilities"}; None = abstained
    extra: dict[str, float | dict] | None = None


def candidate_view(ep: dict, capability_text: str) -> dict:
    """What the judge reads about one endpoint — the row's own words, bounded, never the schema."""
    return {
        "id": ep["id"],
        "name": ep.get("name") or "",
        "summary": (ep.get("summary") or "")[:160],
        "capability": capability_text[:120],
        "platform": ep.get("platform") or "",
    }


def job_view(job: str, description: str, platform: str, providers: int, examples: list[str]) -> dict:
    """What the judge reads about one job: what it does, where, how many vendors sell it, and a few
    of the names they sell it under."""
    return {
        "id": job,
        "job": description,
        "platform": platform,
        "providers": providers,
        "examples": "; ".join(examples[:4])[:240],
    }


_QUESTIONS = {
    "endpoint": "Calling the API endpoint `candidates[{i}]` would directly accomplish, or be a necessary "
                "step of, the task described in `task`, on the platform or data source the task implies.",
    "job": "Tools that do the job `candidates[{i}]` would directly accomplish, or be a necessary step "
           "of, the task described in `task`.",
}


def _question(i: int, criteria: dict | None = None, kind: str = "endpoint") -> dict:
    q = {"type": "noul", "instructions": _QUESTIONS[kind].format(i=i)}
    if criteria:
        q["criteria"] = criteria
    return q


def _cache_key(model: str, query: str, ids: list[str], criteria: dict | None, extra: dict | None,
               job_criteria: dict | None, kinds: list[str]) -> str:
    return hashlib.sha256(json.dumps([model, query.strip().lower(), ids, criteria, extra, job_criteria, kinds],
                                     sort_keys=True).encode()).hexdigest()


def _extra_answer(answer: dict) -> float | dict:
    if answer.get("type") == "choice" or "choice" in answer:
        return {"choice": answer.get("choice"), "confidence": float(answer.get("confidence") or 0.0),
                "probabilities": {k: float(v) for k, v in (answer.get("probabilities") or {}).items()}}
    return float(answer["noul"])


def _cache_get(key: str) -> tuple[list[float], dict] | None:
    hit = _cache.get(key)
    if hit is None:
        return None
    stamp, probs = hit
    if time.monotonic() - stamp > _CACHE_TTL_S:
        _cache.pop(key, None)
        return None
    _cache.move_to_end(key)
    return probs


def _cache_put(key: str, answer: tuple[list[float], dict]) -> None:
    _cache[key] = (time.monotonic(), answer)
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


def clear_cache() -> None:
    _cache.clear()


async def judge(query: str, candidates: list[dict], *, api_key: str, model: str, url: str,
                timeout_s: float, criteria: dict | None = None, extra: dict[str, dict] | None = None,
                job_criteria: dict | None = None,
                transport: httpx.AsyncBaseTransport | None = None) -> Judgement:
    """Probabilities that each candidate accomplishes `query`. Never raises.

    `criteria` (Noul `true`/`false` descriptions) is attached to every endpoint question. A candidate
    in `job_view`'s shape (it carries `job`) is asked the job question instead, with `job_criteria`.
    `extra` maps ids to further questions about the same state (the query alone, say), Noul or
    Choice; they ride in the same request and come back as `Judgement.extra`, and are asked even
    with no candidates (a request of extras only). None of these changes what a caller passing none
    sends."""
    if not candidates and not extra:
        return Judgement(probs=[], ms=0, extra={})
    ids = [c["id"] for c in candidates]
    kinds = ["job" if "job" in c else "endpoint" for c in candidates]
    key = _cache_key(model, query, ids, criteria, extra, job_criteria, kinds)
    cached = _cache_get(key)
    if cached is not None:
        return Judgement(probs=cached[0], ms=0, cached=True, extra=cached[1])
    questions = {f"c{i}": _question(i, job_criteria if kind == "job" else criteria, kind)
                 for i, kind in enumerate(kinds)}
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
        extras = {k: _extra_answer(answers[f"x_{k}"]) for k in (extra or {})}
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
