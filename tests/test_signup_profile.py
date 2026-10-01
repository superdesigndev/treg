""""Picked for you" (application.signup_profile): enrich through treg, then one jev request that
matches the person to the catalog's jobs.

Every upstream is a MockTransport keyed by URL, so these pin the flow and its guards: off unless
configured, personal mailboxes are asked instead of enriched, one card per job (routed when treg
routes it, never two with the same description), and a re-rank keeps showing the current tools.
"""
from __future__ import annotations

import json

import httpx
import pytest
from httpx import AsyncClient

from conftest import verified_signup
from treg.application import signup_profile as sp
from treg.config import get_settings
from treg.domain.catalog import store as catalog_store


def test_every_use_case_names_real_capabilities():
    caps = catalog_store.load().capabilities
    for key, uc in sp.USE_CASES.items():
        assert not [c for c in uc["capabilities"] if c not in caps], key


@pytest.fixture
def on(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "signup_profile_enabled", True, raising=False)
    monkeypatch.setattr(s, "jev_treg_token", "house-token", raising=False)
    seen: list[str] = []
    states: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        if req.url.path.endswith("/call/treg.people.enrich"):
            assert req.headers["X-Treg-Token"] == "house-token"
            return httpx.Response(200, headers={"X-Treg-Cost-Micro": "2600"}, json={"output": {
                "full_name": "Tim Shi", "title": "Head of Growth", "company": "Superdesign Dev",
                "company_domain": "superdesign.dev"},
                "raw": {"company": {"summary": {"name": "Superdesign Dev", "description": "AI design agent",
                                                "staff": {"range": {"start": 2, "end": 10}}},
                                    "link": {"domain": "https://www.superdesign.dev"}}}})
        if req.url.path.endswith("/call/thecompaniesapi.companies.enrich"):
            return httpx.Response(404, json={"message": "companyNotFound"})
        if req.url.path.endswith("/call/openrouter.ai-judge.decide"):
            body = json.loads(req.content)
            states.append(body["state"])
            answers = {k: {"type": "noul", "noul": 0.9 - int(k[1:]) * 0.005} for k in body["questions"] if k[0] == "c"}
            if "persona" in body["questions"]:
                answers["persona"] = {"type": "choice", "choice": "marketer", "probabilities": {"marketer": 0.9}}
            return httpx.Response(200, headers={"X-Treg-Cost-Micro": "50"}, json={"answers": answers})
        return httpx.Response(599)

    monkeypatch.setattr(sp, "_transport", httpx.MockTransport(handler))
    return seen, states


async def test_profile_is_off_until_configured(clients: AsyncClient):
    r = await clients.get("/onboard/profile")
    assert r.status_code == 200 and r.json() == {"status": "off"}
    assert (await clients.post("/onboard/profile/use-case", json={"use_case": "seo"})).status_code == 404


async def test_work_email_is_enriched_then_matched_in_one_jev_request(clients: AsyncClient, on):
    seen, states = on
    assert (await clients.get("/onboard/profile")).json()["status"] == "pending"
    await sp.drain()
    p = (await clients.get("/onboard/profile")).json()
    assert p["status"] == "ready" and p["persona"] == "marketer"
    assert p["person"]["title"] == "Head of Growth"
    assert p["company"] == {"name": "Superdesign Dev", "domain": "superdesign.dev", "tagline": "AI design agent",
                            "employees": "2-10"}
    assert seen.count("/call/treg.people.enrich") == 1 and seen.count("/call/openrouter.ai-judge.decide") == 1
    assert "cost_usd" not in p and "<company>" in states[0]

    tools = p["tools"]
    assert len(tools) == sp.TOOLS_KEEP
    assert len({t["capability"] for t in tools}) == len({t["job"].lower() for t in tools}) == len(tools)
    assert all(t["platform_label"] and t["cap_key"] and t["served"] != "treg" for t in tools)
    assert any(t["routed"] and t["id"].startswith("treg.") for t in tools)


async def test_a_due_rerank_keeps_showing_the_tools_and_reuses_the_enrichment(clients: AsyncClient, on, monkeypatch):
    seen, _ = on
    await clients.get("/onboard/profile")
    await sp.drain()
    monkeypatch.setattr(sp, "TOOLS_FRESH_S", 0)
    again = (await clients.get("/onboard/profile")).json()
    assert again["status"] == "ready" and again["tools"]          # never blanks while re-ranking
    await sp.drain()
    assert seen.count("/call/treg.people.enrich") == 1 and seen.count("/call/openrouter.ai-judge.decide") == 2


async def test_personal_email_is_asked_then_matched_from_the_answer(clients: AsyncClient, on):
    seen, states = on
    r = await verified_signup(clients, json={"email": "someone.new@gmail.com"})
    clients.headers["X-Treg-Token"] = r.json()["token"]
    await clients.get("/onboard/profile")
    await sp.drain()
    p = (await clients.get("/onboard/profile")).json()
    assert p["status"] == "ask" and "tools" not in p and len(p["use_cases"]) == len(sp.USE_CASES)
    assert seen == []   # a personal mailbox is never enriched, and there is nothing yet for jev

    assert (await clients.post("/onboard/profile/use-case", json={"use_case": "nope"})).status_code == 400
    assert (await clients.post("/onboard/profile/use-case", json={"use_case": "creative"})).json()["status"] == "pending"
    await sp.drain()
    p = (await clients.get("/onboard/profile")).json()
    assert p["status"] == "ready" and p["answer"] == "creative" and p["tools"]
    assert "<here_for>" in states[-1] and p["persona"] == "other"   # no person to judge a role from
    assert any(t["reason"] == "For ai video and images" for t in p["tools"])


async def test_use_case_changes_are_rate_limited(clients: AsyncClient, on, monkeypatch):
    monkeypatch.setattr(sp, "ANSWER_LIMIT", (2, 3600))
    for uc in ("leads", "ads"):
        assert (await clients.post("/onboard/profile/use-case", json={"use_case": uc})).status_code == 200
    assert (await clients.post("/onboard/profile/use-case", json={"use_case": "web"})).status_code == 429
    await sp.drain()


def test_candidates_are_jobs_routed_when_treg_routes_them():
    cat = catalog_store.load()
    routed = {e["capability"] for e in cat.endpoints if e.get("kind") == "routed"}
    direct = next(e for e in cat.endpoints if e.get("capability") == "tiktok.user.profile"
                  and e.get("kind") != "routed" and e.get("tier") == "core" and e.get("scope") != "own_account")
    cands = sp.candidates(cat, "seo", [{"id": direct["id"], "n": 5, "failed": 0}])
    caps = [j["capability"] for j, _, _ in cands]
    assert len(caps) == len(set(caps)) <= sp.MAX_CANDIDATES     # one candidate per job
    assert all(j["id"].startswith("treg.") == (j["capability"] in routed) for j, _, _ in cands)
    assert cands[0][1] == "route" and cands[0][0]["capability"] == "tiktok.user.profile" and cands[0][2] == direct["id"]
    assert {"answer", "platform", "profile"} <= {why for _, why, _ in cands}


def test_pick_never_shows_two_cards_for_the_same_job():
    cat = catalog_store.load()
    jobs = sp._jobs(cat)
    same = [jobs[c] for c in ("tiktok.search.videos", "youtube.search.videos") if c in jobs]
    assert len(same) == 2 and cat.capabilities[same[0]["capability"]] == cat.capabilities[same[1]["capability"]]
    other = jobs["people.email.find"]
    cands = [(same[0], "profile", ""), (same[1], "profile", ""), (other, "profile", "")]
    picked = sp.pick([(0.9, 0), (0.8, 1), (0.7, 2)], cands, cat)
    assert [i for _, i in picked] == [0, 2]
    # the bar is relative: a far weaker job does not make the cut
    assert [i for _, i in sp.pick([(0.9, 2), (0.3, 0)], cands, cat)] == [2]
