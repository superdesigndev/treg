"""Optional, bounded Web Arena quality checks. Missing checks leave provider data intact."""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timezone
from urllib.parse import urlsplit

import httpx
from sqlalchemy import update

from ..config import get_settings
from ..domain import web_arena as rules
from ..infra.db import session_maker
from ..models import WebArenaJudgeBudget

log = logging.getLogger(__name__)
JEV_URL = "https://ai-gateway.vercel.sh/v4/ai/evaluation-model"
CHAT_URL = "https://ai-gateway.vercel.sh/v1/chat/completions"


async def _take_budget(user_id: int) -> bool:
    """Admit one external quality call; commit before the network request."""
    settings = get_settings()
    if not settings.ai_gateway_api_key:
        return False
    if settings.local_dev:
        return True
    day = datetime.now(timezone.utc).date().isoformat()
    async with session_maker() as db:
        dialect = db.bind.dialect.name
        insert = __import__(f"sqlalchemy.dialects.{dialect}", fromlist=["insert"]).insert
        limits = [(f"global:{day}", settings.web_arena_jev_ops_daily_cap),
                  (f"user:{user_id}:{day}", settings.web_arena_jev_user_daily_cap)]
        for key, _ in limits:
            await db.execute(insert(WebArenaJudgeBudget).values(id=key, calls=0)
                             .on_conflict_do_nothing(index_elements=["id"]))
        for key, cap in limits:
            changed = await db.execute(update(WebArenaJudgeBudget).where(
                WebArenaJudgeBudget.id == key, WebArenaJudgeBudget.calls < cap)
                .values(calls=WebArenaJudgeBudget.calls + 1))
            if changed.rowcount != 1:
                await db.rollback()
                return False
        await db.commit()
    return True


async def _jev(state: dict, questions: dict, user_id: int) -> tuple[dict | None, dict]:
    if not await _take_budget(user_id):
        return None, {"state": "budget_unavailable"}
    began = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            response = await http.post(JEV_URL, headers={"Authorization": f"Bearer {get_settings().ai_gateway_api_key}",
                "ai-model-id": "typesafe-ai/jev", "ai-evaluation-model-specification-version": "4",
                "ai-gateway-protocol-version": "0.0.1"},
                                       json={"state": state, "questions": questions})
            response.raise_for_status()
            data = response.json()
            answers = data.get("answers")
            usage = data.get("usage") or {}
            if not isinstance(usage, dict):
                usage = {}
            tokens = usage.get("inputTokens")
            return answers if isinstance(answers, dict) else None, {
                "jev_ms": round((time.monotonic() - began) * 1000),
                "jev_cost_usd": round(int(tokens) * 0.042 / 1_000_000, 8) if isinstance(tokens, (int, float)) else None}
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        log.warning("Web Arena Jev check unavailable", exc_info=True)
        return None, {"jev_ms": round((time.monotonic() - began) * 1000), "jev_cost_usd": None}


def _search_links(output: dict, task: str = "search") -> list[dict]:
    links = []
    for item in rules.result_items(task, output)[:5]:
        if not isinstance(item, dict):
            continue
        url = next((item.get(k) for k in ("url", "link", "href", "pageUrl") if isinstance(item.get(k), str)), None)
        if not url and task == "youtube" and isinstance(item.get("video_id"), str):
            url = "https://www.youtube.com/watch?v=" + item["video_id"]
        if not url or urlsplit(url).scheme not in {"http", "https"}:
            continue
        dated = next((item.get(k) for k in ("publishedDate", "published_at", "datePublished", "date", "published") if isinstance(item.get(k), str)), None)
        links.append({"url": url[:500], "title": str(item.get("title") or "")[:250],
                      "snippet": str(item.get("snippet") or item.get("description") or item.get("description_snippet") or item.get("text") or item.get("content") or "")[:500],
                      "source_date": dated[:40] if dated else None})
    return links


def _recent_share(links: list[dict]) -> dict:
    known = 0
    fresh = 0
    today = date.today()
    for link in links:
        raw = link["source_date"]
        if not raw:
            continue
        try:
            stamp = date.fromisoformat(raw[:10])
        except ValueError:
            continue
        known += 1
        fresh += 0 <= (today - stamp).days <= 30
    return {"known_dates": known, "recent_dated_links": fresh,
            "freshness_percent": round(100 * fresh / known) if known else None,
            "freshness_window_days": 30 if known else None}


async def search(input_value: str, output: dict, user_id: int, *, task: str = "search") -> dict:
    links = _search_links(output, task)
    if not links:
        return {"state": "unknown", "links_checked": [], "estimated_match": None, "freshness_percent": None}
    questions = {f"link{i}": {"type": "boolean", "instructions":
        {"question": f"Does links[{i}] match the user's search query and likely intent?",
         "read": "Use only its title, URL, and snippet."}}
        for i in range(len(links))}
    questions["recent"] = {"type": "boolean", "instructions":
        {"question": "Does the query need current or recent information to answer well?"}}
    answers, meta = await _jev({"query": input_value[:500], "links": links}, questions, user_id)
    result = {"state": "unknown", "links_checked": links, "estimated_match": None,
              "recent_data_needed": None, **_recent_share(links), **meta}
    if not answers:
        return result
    try:
        probs = [float(answers[f"link{i}"]["probability"]) for i in range(len(links))]
        result["estimated_match"] = round(100 * sum(probs) / len(probs))
        result["recent_data_needed"] = float(answers["recent"]["probability"]) >= 0.5
        result["state"] = "checked"
        if not result["recent_data_needed"]:
            result["freshness_percent"] = None
    except (KeyError, ValueError, TypeError):
        pass
    return result


async def _facts(outputs: list[str], user_id: int) -> tuple[list[str] | None, dict]:
    if not await _take_budget(user_id):
        return None, {"state": "budget_unavailable"}
    began = time.monotonic()
    prompt = ("List at most 12 concrete, checkable facts found in these page extracts. "
              "Use only the text given. Return JSON with one key, facts, an array of short strings. "
              "Include facts from all extracts. Do not infer missing facts.\n" +
              json.dumps([text[:2000] for text in outputs[:12]], ensure_ascii=True))
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            response = await http.post(CHAT_URL,
                headers={"Authorization": f"Bearer {get_settings().ai_gateway_api_key}"},
                json={"model": get_settings().web_arena_fact_model,
                      "messages": [{"role": "user", "content": prompt}],
                      "response_format": {"type": "json_object"}, "max_tokens": 700})
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            facts = json.loads(content).get("facts")
            if isinstance(facts, list):
                usage = response.json().get("usage") or {}
                return [fact[:300] for fact in facts[:12] if isinstance(fact, str) and fact.strip()], {
                    "llm_ms": round((time.monotonic() - began) * 1000),
                    "llm_tokens": usage.get("total_tokens")}
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
        log.warning("Web Arena fact list unavailable", exc_info=True)
    return None, {"llm_ms": round((time.monotonic() - began) * 1000)}


async def fetch(attempts: list[dict], user_id: int) -> None:
    hits = [a for a in attempts if a["state"] == "hit" and rules.fetch_text(a.get("output") or {})]
    if len(hits) < 2:
        return
    texts = [rules.fetch_text(a["output"]) for a in hits]
    facts, fact_meta = await _facts(texts, user_id)
    if not facts:
        return
    for attempt, content in zip(hits, texts):
        questions = {f"fact{i}": {"type": "boolean", "instructions":
            {"question": f"Does the extract preserve this fact: {fact}?", "read": "Answer from extract only."}}
            for i, fact in enumerate(facts)}
        answers, jev_meta = await _jev({"extract": content[:12_000]}, questions, user_id)
        if not answers:
            continue
        try:
            kept = sum(float(answers[f"fact{i}"]["probability"]) >= 0.5 for i in range(len(facts)))
        except (KeyError, ValueError, TypeError):
            continue
        quality = attempt.setdefault("quality", {})
        quality.update(state="checked", relative_coverage=round(100 * kept / len(facts)),
                       kept_facts=kept, compared_facts=len(facts),
                       tokens_per_kept_fact=round(quality["tokens"] / kept, 1) if kept else None,
                       **fact_meta, **jev_meta,
                       limitation="The check cannot find a fact that every provider missed.")
    denominators = [a.get("quality", {}).get("tokens_per_kept_fact") for a in hits]
    known = [n for n in denominators if isinstance(n, (int, float)) and n > 0]
    if known:
        best = min(known)
        for attempt in hits:
            quality = attempt.get("quality") or {}
            n = quality.get("tokens_per_kept_fact")
            if isinstance(n, (int, float)) and n > 0:
                quality["token_efficiency"] = round(100 * best / n, 1)
