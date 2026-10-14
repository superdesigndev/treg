"""The first-run onboarding behind `onboarding_v2`: team, lookup, ranked first tasks.

The lookup is exercised against one fake network (GitHub, treg's own `/call/` for the house calls,
the AI Gateway and the judge), so each test states what the world knows about a user and checks
which tasks and inputs come out, and that nothing ever lands on the user's own credit.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import select

from conftest import make_upstream
from treg.api import app
from treg.application.onboard import first_run, lookup, page
from treg.application.onboard.lookup import Hints, Lookup
from treg.application.onboard.tasks import DEFAULT_RANK, TASKS, build_calls
from treg.config import get_settings
from treg.domain.identity import session as sess
from treg.infra.db import reset_db, session_maker
from treg.infra.upstream import ssrf
from treg.models import LedgerEntry, Membership, OnboardingProfile, Org, User


# ---------------------------------------------------------------------------------- the library
def test_every_task_builds_its_example_calls():
    for tid, t in TASKS.items():
        calls = build_calls(tid, t.example[0])
        assert calls, tid
        for c in calls:
            assert c["body"] or c["query"], tid


def test_fill_in_values_parse_into_the_call():
    assert build_calls("email", "Karri Saarinen at linear.app")[0]["body"] == \
        {"full_name": "Karri Saarinen", "domain": "linear.app"}
    assert build_calls("email", "just a name") is None
    assert build_calls("company", "linear.app")[0]["body"] == {"domain": "linear.app"}
    assert build_calls("company", "Pixelforge")[0]["body"] == {"name": "Pixelforge"}
    assert build_calls("scrape", "https://uizard.io/pricing")[0]["body"] == {"url": "https://uizard.io/pricing"}
    assert build_calls("scrape", "uizard.io/pricing")[0]["body"] == {"url": "https://uizard.io/pricing"}
    social = build_calls("social", "Linear")
    assert [c["endpoint"] for c in social] == ["treg.x.search.posts", "scrapecreators.reddit.search.posts"]
    assert social[1] == {"endpoint": "scrapecreators.reddit.search.posts", "method": "GET", "body": {},
                         "query": {"query": "Linear", "sort": "relevance"}}
    assert build_calls("serp", "  ") is None


# ---------------------------------------------------------------------------------- the lookup
class World:
    """One fake network. `serp` maps a query to its result hosts; `judge` answers by question id."""

    def __init__(self, *, commits_login="", profile=None, company=None, homepage="", llm=None,
                 judge=None, serp=None, tiktok=None, call_cost=1000, orgs=None, pages=None):
        self.commits_login, self.profile, self.company, self.homepage = commits_login, profile or {}, company, homepage
        self.orgs, self.pages = orgs or {}, pages or {}
        self.llm, self.judge, self.serp, self.tiktok, self.call_cost = llm, judge, serp or {}, tiktok or {}, call_cost
        self.house_calls: list[tuple[str, dict, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "api.github.com/search/commits" in url:
            items = [{"author": {"login": self.commits_login}}] if self.commits_login else []
            return httpx.Response(200, json={"items": items})
        if "api.github.com/search/users" in url:
            return httpx.Response(200, json={"items": [{"login": o} for o in self.orgs]})
        if "api.github.com/orgs/" in url:
            org = url.split("/orgs/")[1].split("/")[0].split("?")[0]
            if url.split("?")[0].endswith("/repos"):
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=self.orgs.get(org, {})) if org in self.orgs else httpx.Response(404)
        if "api.github.com/users/" in url:
            return httpx.Response(200, json=self.profile)
        if "ai-gateway.vercel.sh" in url:
            if self.llm is None:
                return httpx.Response(500, json={})
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(self.llm)}}],
                                             "usage": {"prompt_tokens": 10, "completion_tokens": 5, "market_cost": 0.0002}})
        if "/call/" in url:
            endpoint = url.split("/call/")[1].split("?")[0]
            body = json.loads(request.content) if request.content else {}
            self.house_calls.append((endpoint, body, request.headers.get("X-Treg-Token")))
            out = self._call(endpoint, body)
            if out is None:
                return httpx.Response(404, json={})
            body_out = out if endpoint == "openrouter.ai-judge.decide" else {"output": out}
            return httpx.Response(200, json=body_out, headers={"X-Treg-Cost-Micro": str(self.call_cost)})
        if request.method == "GET" and url in self.pages:     # a site read directly
            return httpx.Response(200, text=self.pages[url], headers={"content-type": "text/html"})
        return httpx.Response(404)

    def _call(self, endpoint, body):
        if endpoint == "openrouter.ai-judge.decide":
            assert body["model"] == "typesafe/jev-1.13" and body["state"].startswith("# A new user")
            return None if self.judge is None else {"answers": {q: {"noul": self.judge.get(q, 0.3)} for q in body["questions"]}}
        if endpoint == "treg.companies.enrich":
            return self.company
        if endpoint == "treg.web.extract":
            return {"pages": [{"markdown": self.homepage}]} if self.homepage else None
        if endpoint == "treg.google.serp.organic":
            hosts = self.serp.get(body["q"])
            return {"results": [{"title": h, "link": f"https://{h}/x"} for h in hosts]} if hosts else None
        if endpoint == "treg.tiktok.search.videos":
            titles = self.tiktok.get(body["q"])
            return {"videos": [{"desc": t} for t in titles]} if titles else None
        return None


@pytest.fixture
def configured(monkeypatch):
    for k, v in {"TREG_ONBOARDING_V2": "1", "TREG_ONBOARDING_TREG_TOKEN": "house-token",
                 "TREG_ONBOARDING_GITHUB_TOKEN": "gh", "TREG_AI_GATEWAY_API_KEY": "gw"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(ssrf, "host_is_public", lambda host: True)   # the fake network has no DNS
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _lookup(world: World, email: str, hints: Hints | None = None):
    saves = []

    async def save(state, house):
        saves.append(house)

    transport = httpx.MockTransport(world.handler)
    async with httpx.AsyncClient(transport=transport) as http:
        state = await Lookup(email, hints or Hints(door="google"), http, save, transport=transport).run()
    return state, saves


PIXELFORGE = World(
    commits_login="ada-builds",
    profile={"login": "ada-builds", "name": "Ada", "company": "@Pixelforge", "blog": "", "bio": "builder"},
    company={"name": "Pixelforge", "domain": "pixelforge.dev", "industry": "Software"},
    homepage="Pixelforge. AI design platform. Design with AI, export code.",
    llm={"search_terms": [{"value": "ai design platform", "why": "title"}, {"value": "ai ui generator", "why": "features"}],
         "tiktok_topics": [{"value": "ai ui design", "why": "category"}],
         "target_companies": [], "maps_query": [{"value": "design agencies in Lisbon", "why": "location"}]},
    # s*: does a search's results look like the user's market; r*: is a result a direct competitor
    # (figma.com is r0, uxpilot.ai r1, uizard.io r2); t*: does a TikTok topic fit
    judge={"company": 0.45, "keywords": 0.62, "serp": 0.44, "videos": 0.41, "s0": 0.17, "s1": 0.84,
           "r0": 0.3, "r1": 0.2, "r2": 0.9, "t0": 0.9},
    serp={"ai design platform": ["canva.com", "kittl.com"],
          "ai ui generator": ["figma.com", "uxpilot.ai", "uizard.io", "pixelforge.dev"]},
    tiktok={"ai ui design": ["Designing a dashboard with AI", "UI design with AI in 60 seconds"]},
    orgs={"pixelforgedev": {"login": "pixelforgedev", "blog": "https://pixelforge.dev"}},
    pages={"https://pixelforge.dev": "<html><head><title>Pixelforge</title><meta name=description content='AI design "
                                      "platform'><script>var x=1</script><link rel='apple-touch-icon' href='/icon.png'>"
                                      "<script src='https://static.klaviyo.com/onsite/js/klaviyo.js'></script>"
                                      "<script src='https://connect.facebook.net/en_US/fbevents.js'></script></head><body><h1>Design with AI</h1><p>"
                                      + "Describe what you want, iterate in natural language and export code. " * 6
                                      + "</p></body></html>",
           "https://uizard.io/pricing": "<html><title>Pricing</title><body>Plans</body></html>"},
)


async def test_a_gmail_founder_is_read_from_github_their_company_and_site(configured):
    state, saves = await _lookup(PIXELFORGE, "someone@gmail.com")
    steps = {s["key"]: s for s in state.steps}
    assert steps["github"]["state"] == "ok" and steps["github"]["detail"] == "@ada-builds · Pixelforge"
    assert steps["company"]["detail"].startswith("Pixelforge · pixelforge.dev")
    assert steps["homepage"] == {"key": "homepage", "state": "ok", "detail": "Read pixelforge.dev"}
    assert steps["personal"]["state"] == "ok"
    # a Gmail address has no work domain: the company's site came from the GitHub organisation
    # named like the profile's company, and the site was read directly, not through the catalog
    assert ("treg.companies.enrich", {"domain": "pixelforge.dev"}, "house-token") in PIXELFORGE.house_calls
    assert "treg.web.extract" not in [c[0] for c in PIXELFORGE.house_calls]
    assert state.ranking[0] == {"id": "keywords", "score": 0.62}
    f = state.fills
    # the judge overruled the LLM's first term: its results are not products like the user's
    assert f["serp"]["value"] == f["keywords"]["value"] == "ai ui generator"
    # the first result the judge calls a direct competitor; never the user's own site
    assert f["scrape"] == {"value": "uizard.io/pricing", "source": "checked", "pos": 3, "term": "ai ui generator",
                           "note": 'a competitor\'s pricing page: #3 on Google for \u201cai ui generator\u201d'}
    assert f["videos"]["value"] == "ai ui design"
    assert f["company"]["value"] == "pixelforge.dev" and f["social"]["value"] == "Pixelforge"
    assert f["maps"]["value"] == "design agencies in Lisbon"
    # nothing grounded these: their labelled examples
    for tid in ("email", "people"):
        assert f[tid] == {"value": TASKS[tid].example[0], "note": TASKS[tid].example[1], "source": "example"}
    facts = {f["key"]: f for f in first_run._facts({"evidence": state.evidence, "fills": state.fills})}
    # the welcome greets the person; the company is the line above it
    assert (facts["intro"]["value"], facts["intro"]["label"]) == ("Ada", "Pixelforge")
    assert facts["github"]["value"] == "@ada-builds"
    # what the site's code loads: the tools, the ad pixels, and its square icon as the company's mark
    assert facts["tools"]["value"] == "Klaviyo" and facts["ads"]["value"] == "Meta"
    assert facts["intro"]["avatar"] == "https://pixelforge.dev/icon.png"
    # a note only where it adds a fact: the record's own fields need no "on the company record"
    assert facts["industry"] == {"key": "industry", "label": "Industry", "value": "Software", "note": ""}
    assert facts["site"]["value"] == "pixelforge.dev" and facts["site"]["note"] == ""
    assert facts["search"]["value"] == '\u201cai ui generator\u201d'
    assert facts["competitor"] == {"key": "competitor", "label": "A competitor", "value": "uizard.io",
                                   "note": '#3 on Google for \u201cai ui generator\u201d'}
    assert "signin" not in str(facts) and "landing" not in str(facts)     # where they came from stays inside
    # every answered house call is on the house; the one TikTok search that found nothing is free
    assert saves[-1] == PIXELFORGE.call_cost * (len(PIXELFORGE.house_calls) - 1)


async def test_the_tasks_are_ready_before_their_inputs_are_checked(configured):
    seen = []

    async def save(state, house):
        seen.append((state.ready, "serp" in state.fills, bool(state.ranking)))

    world = World(**{k: getattr(PIXELFORGE, k) for k in ("profile", "company", "llm", "judge", "serp", "tiktok", "orgs", "pages")},
                  commits_login="ada-builds")
    transport = httpx.MockTransport(world.handler)
    async with httpx.AsyncClient(transport=transport) as http:
        await Lookup("someone@gmail.com", Hints(door="google"), http, save, transport=transport).run()
    first_ready = next(s for s in seen if s[0])
    assert first_ready == (True, False, True)     # ranked and showable; the search term not checked yet
    assert seen[-1] == (True, True, True)


def test_tasks_with_the_users_own_input_lead_the_examples():
    import time
    ranking = [{"id": "email", "score": 0.45}, {"id": "people", "score": 0.44}, {"id": "maps", "score": 0.61},
               {"id": "company", "score": 0.40}, {"id": "keywords", "score": 0.30}, {"id": "social", "score": 0.20}]
    fills = {"company": {"value": "pixelforge.dev", "note": "your company", "source": "company"},
             "social": {"value": "Pixelforge", "note": "your company", "source": "github"}}
    p = {"ranking": ranking, "fills": fills, "pending": ["keywords"], "started": time.time()}
    ready = [t["id"] for t in first_run._shape("ready", p)["tasks"]]
    # maps leads on its example (highly recommended); company and social have the user's own input;
    # keywords is being checked; the examples (email, people) follow in their ranked order
    assert ready[:6] == ["maps", "company", "keywords", "social", "email", "people"]
    done = [t["id"] for t in first_run._shape("done", p)["tasks"]]
    assert done[:6] == ["maps", "company", "social", "email", "people", "keywords"]   # the check found nothing
    assert first_run._shape("ready", p)["preselect"] == "maps"


async def test_a_search_check_asks_the_fast_provider_and_drops_a_slow_answer(configured, monkeypatch):
    monkeypatch.setattr(lookup, "SERP_TIMEOUT_S", 0.2)
    asked = []

    async def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.headers.get("X-Treg-Route-Prefer"))
        if json.loads(request.content)["q"] == "slow":
            await asyncio.sleep(1)
        return httpx.Response(200, json={"output": {"results": [{"title": "a", "link": "https://a.io/x"}]}})

    async def save(state, house):
        pass

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        lk = Lookup("ada@acme.io", Hints(door="google"), http, save)
        fast, slow = await asyncio.gather(lk._serp("fast"), lk._serp("slow"))
    assert fast and slow is None
    assert asked == [lookup.SERP_PROVIDER] * 2


async def test_nothing_public_skips_the_judge_and_shows_the_default_order(configured):
    world = World()
    state, _ = await _lookup(world, "nobody@gmail.com")
    assert [r["id"] for r in state.ranking] == list(DEFAULT_RANK)
    assert all(r["score"] is None for r in state.ranking)
    assert all(f["source"] == "example" for f in state.fills.values())
    assert world.house_calls == []


async def test_an_llm_that_fails_still_ranks_and_keeps_the_facts(configured):
    world = World(company={"name": "Linear", "domain": "linear.app"}, homepage="Linear. Plan and build products.",
                  judge={"email": 0.7})
    state, _ = await _lookup(world, "karri@linear.app")
    assert ("treg.companies.enrich", {"domain": "linear.app"}, "house-token") in world.house_calls
    assert state.ranking[0] == {"id": "email", "score": 0.7}
    assert state.fills["company"]["value"] == "linear.app"
    assert state.fills["keywords"]["source"] == "example"


async def test_the_house_budget_stops_the_checks(configured, monkeypatch):
    monkeypatch.setenv("TREG_ONBOARDING_MAX_HOUSE_MICRO", "2000")
    get_settings.cache_clear()
    world = World(**{k: getattr(PIXELFORGE, k) for k in ("profile", "company", "homepage", "llm", "judge", "serp", "tiktok")},
                  commits_login="ada-builds")
    state, saves = await _lookup(world, "someone@gmail.com")
    assert [c[0] for c in world.house_calls] == ["treg.companies.enrich", "treg.web.extract"]
    assert state.fills["serp"]["source"] == "example"
    assert saves[-1] == 2000


async def test_without_any_key_the_lookup_still_finishes(monkeypatch):
    monkeypatch.setenv("TREG_ONBOARDING_V2", "1")
    monkeypatch.setattr(ssrf, "host_is_public", lambda host: True)
    get_settings.cache_clear()
    try:
        state, _ = await _lookup(World(), "karri@linear.app", Hints(door="github", github_login=""))
    finally:
        get_settings.cache_clear()
    assert {s["key"]: s["state"] for s in state.steps}["company"] == "ok"   # the work domain alone
    assert len(state.fills) == len(TASKS)


# ---------------------------------------------------------------------------------- the routes
@pytest.fixture
async def c(monkeypatch):
    # The lookup reads the user's homepage; the SSRF check before it resolves DNS, which a test must
    # not wait on (a slow resolver under a parallel run left the lookup running past the poll).
    monkeypatch.setattr(ssrf, "host_is_public", lambda host: True)
    await reset_db()
    app.state.http = AsyncClient(transport=ASGITransport(app=make_upstream()), base_url="http://upstream")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as client:
        yield client
    await first_run.shutdown()
    await app.state.http.aclose()


async def _new_user(email: str) -> int:
    async with session_maker() as db:
        u = User(email=email, email_verified_at=datetime.now(timezone.utc).replace(tzinfo=None))
        db.add(u)
        await db.commit()
        return u.id


async def test_the_routes_are_off_without_the_flag(c):
    c.cookies.set("treg_session", sess.make_session(await _new_user("new@gmail.com")))
    assert (await c.post("/onboarding/start")).status_code == 404
    assert (await c.get("/onboarding")).status_code == 404
    assert "onboarding_v2" not in (await c.get("/auth/me")).json()


async def test_the_experiment_is_for_addresses_not_anyone_can_get(c, monkeypatch):
    monkeypatch.setenv("TREG_ONBOARDING_V2_EXPERIMENT", "1")
    get_settings.cache_clear()
    try:
        for email, offered in (("ada@gmail.com", False), ("ada@duck.com", False), ("ada@cs.example.edu", False),
                               ("ada@ox.ac.uk", False), ("ada@acme.io", True), ("ada@ada.dev", True)):
            c.cookies.set("treg_session", sess.make_session(await _new_user(email)))
            me = (await c.get("/auth/me")).json()
            assert me.get("onboarding_v2_experiment", False) is offered, email
            assert "onboarding_v2" not in me        # the flag's arm decides, not the server
        assert (await c.get("/onboarding")).status_code == 200      # ada@ada.dev may read the flow
        c.cookies.set("treg_session", sess.make_session(await _new_user("bob@gmail.com")))
        assert (await c.get("/onboarding")).status_code == 404
        monkeypatch.setenv("TREG_ONBOARDING_V2_EXPERIMENT", "0")  # the off switch closes it
        get_settings.cache_clear()
        c.cookies.set("treg_session", sess.make_session(await _new_user("cy@acme.io")))
        assert (await c.get("/onboarding")).status_code == 404
    finally:
        get_settings.cache_clear()


async def test_start_makes_one_team_with_the_signup_credit_and_finishes(c, monkeypatch):
    monkeypatch.setenv("TREG_ONBOARDING_V2", "1")
    get_settings.cache_clear()
    try:
        uid = await _new_user("ada.l@gmail.com")
        c.cookies.set("treg_session", sess.make_session(uid))
        assert (await c.get("/auth/me")).json()["onboarding_v2"] is True
        c.cookies.set("treg_landing", "%2Fpeople-search")
        r = await c.post("/onboarding/start")
        assert r.status_code == 200, r.text
        assert r.json()["team"]["name"] == "Ada's team"
        again = await c.post("/onboarding/start")
        assert again.status_code == 200 and again.json()["team"] == r.json()["team"]
        for _ in range(200):
            d = (await c.get("/onboarding")).json()
            if d["status"] == "done":
                break
            await asyncio.sleep(0.05)
        assert d["status"] == "done"
        assert [t["id"] for t in d["tasks"]] == list(DEFAULT_RANK)
        assert d["preselect"] is None and d["shown"] == 5 and len(d["library"]) == len(TASKS)
        assert "evidence" not in d     # where they came from never reaches the page
        # nothing public grounded a task: the page asks what the agent is for, and the answer leads
        assert d["ask"] is True and d["here_for"] is None
        assert (await c.post("/onboarding/answer", json={"here_for": "nope"})).status_code == 400
        a = (await c.post("/onboarding/answer", json={"here_for": "seo"})).json()
        assert [t["id"] for t in a["tasks"]][:2] == ["keywords", "serp"] and a["preselect"] == "keywords"
        assert (await c.get("/onboarding")).json()["here_for"] == "seo"     # kept on the profile
        async with session_maker() as db:
            teams = (await db.execute(select(Membership).where(Membership.user_id == uid))).scalars().all()
            assert len(teams) == 1
            grants = (await db.execute(select(LedgerEntry).where(LedgerEntry.org_id == teams[0].org_id,
                                                                 LedgerEntry.kind == "grant"))).scalars().all()
            assert len(grants) <= 1
            row = (await db.execute(select(OnboardingProfile).where(OnboardingProfile.user_id == uid))).scalar_one()
            assert row.house_cost_micro == 0
            assert "people-search" not in row.payload    # encrypted at rest
    finally:
        get_settings.cache_clear()


async def test_start_survives_a_reserved_name_a_double_click_and_a_personal_team(c, monkeypatch):
    monkeypatch.setenv("TREG_ONBOARDING_V2", "1")
    get_settings.cache_clear()
    try:
        # a vendor's own domain names a reserved team: the person's name is used instead
        assert first_run.team_names("ada@apollo.io") == ["Apollo", "Ada's team", "My team"]
        uid = await _new_user("ada@apollo.io")
        c.cookies.set("treg_session", sess.make_session(uid))
        # two starts at once (two tabs) make one team between them
        a, b = await asyncio.gather(c.post("/onboarding/start"), c.post("/onboarding/start"))
        assert (a.status_code, b.status_code) == (200, 200), (a.text, b.text)
        assert a.json()["team"] == b.json()["team"]
        assert a.json()["team"]["name"] == "Ada's team"      # "Apollo" is a catalog vendor's reserved name
        async with session_maker() as db:
            assert len((await db.execute(select(Membership).where(Membership.user_id == uid))).scalars().all()) == 1

        # the legacy door's personal team (named after the email) is not a team the user named
        uid = await _new_user("cy@example.org")
        async with session_maker() as db:
            org = Org(name="cy@example.org", slug=f"cy-{uid}")
            db.add(org)
            await db.flush()
            db.add(Membership(user_id=uid, org_id=org.id, role="owner", token_hash=f"h{uid}"))
            await db.commit()
        c.cookies.set("treg_session", sess.make_session(uid))
        r = await c.post("/onboarding/start")
        assert r.status_code == 200, r.text
        assert r.json()["team"]["name"] == "Example"
    finally:
        get_settings.cache_clear()


async def test_an_account_with_a_team_is_not_onboarded_again(c, monkeypatch):
    monkeypatch.setenv("TREG_ONBOARDING_V2", "1")
    get_settings.cache_clear()
    try:
        uid = await _new_user("old@x.io")
        c.cookies.set("treg_session", sess.make_session(uid))
        assert (await c.post("/orgs", json={"name": "Old team"})).status_code == 200
        assert (await c.post("/onboarding/start")).status_code == 409
    finally:
        get_settings.cache_clear()


async def test_the_flow_can_be_on_for_listed_people_only(c, monkeypatch):
    monkeypatch.setenv("TREG_ONBOARDING_V2_EMAILS", "@pixelforge.dev, ada@example.com")
    get_settings.cache_clear()
    try:
        assert [first_run.enabled(e) for e in ("tim@pixelforge.dev", "ADA@example.com", "bob@example.com")] == \
            [True, True, False]
        c.cookies.set("treg_session", sess.make_session(await _new_user("bob@example.com")))
        assert (await c.post("/onboarding/start")).status_code == 404
        assert "onboarding_v2" not in (await c.get("/auth/me")).json()
    finally:
        get_settings.cache_clear()


async def test_a_preview_runs_the_flow_for_any_email_without_a_team(c, monkeypatch):
    monkeypatch.setenv("TREG_ONBOARDING_V2_EMAILS", "tuner@pixelforge.dev")
    monkeypatch.setenv("TREG_ONBOARDING_TREG_TOKEN", "house-token")
    monkeypatch.setenv("TREG_ONBOARDING_TREG_URL", "http://upstream")
    get_settings.cache_clear()
    try:
        c.cookies.set("treg_session", sess.make_session(await _new_user("someone@else.io")))
        assert (await c.post("/onboarding/preview", json={"email": "a@b.co"})).status_code == 404
        assert "onboarding_preview" not in (await c.get("/auth/me")).json()

        uid = await _new_user("tuner@pixelforge.dev")
        c.cookies.set("treg_session", sess.make_session(uid))
        assert (await c.get("/auth/me")).json()["onboarding_preview"] is True
        assert (await c.post("/onboarding/preview", json={"email": "not an email"})).status_code == 400
        r = await c.post("/onboarding/preview", json={"email": "Karri@Linear.app"})
        assert r.status_code == 200, r.text
        pid = r.json()["id"]
        assert r.json()["email"] == "karri@linear.app" and r.json()["team"] is None
        for _ in range(200):
            d = (await c.get(f"/onboarding/preview/{pid}")).json()
            if d["status"] == "done":
                break
            await asyncio.sleep(0.05)
        assert d["status"] == "done" and len(d["tasks"]) == len(TASKS)
        async with session_maker() as db:     # nothing was made for the previewer
            assert (await db.execute(select(Membership).where(Membership.user_id == uid))).first() is None
            assert (await db.execute(select(OnboardingProfile))).first() is None
            assert (await db.get(User, uid)).onboarded is False

        # only a call the task library makes, and only a few of them
        bad = await c.post(f"/onboarding/preview/{pid}/call", json={"endpoint": "stripe.charges.create", "body": {}})
        assert bad.status_code == 400
        call = {"endpoint": "treg.google.serp.organic", "method": "POST", "body": {"q": "ai ui generator"}}
        cap = first_run.PREVIEW_CALLS
        answers = [(await c.post(f"/onboarding/preview/{pid}/call", json=call)).status_code for _ in range(cap + 1)]
        assert answers[:cap] == [200] * cap and answers[cap] == 429

        other = await _new_user("other@pixelforge.dev")      # a preview is its owner's alone
        monkeypatch.setenv("TREG_ONBOARDING_V2_EMAILS", "tuner@pixelforge.dev,other@pixelforge.dev")
        get_settings.cache_clear()
        c.cookies.set("treg_session", sess.make_session(other))
        assert (await c.get(f"/onboarding/preview/{pid}")).status_code == 404
    finally:
        get_settings.cache_clear()


async def test_a_handle_like_company_name_is_searched_as_its_domain(configured):
    # cal.com's record names another company, and the GitHub company is the handle "calcom": the
    # record is dropped and X/Reddit search the domain, which is what people write
    world = World(commits_login="peer", profile={"login": "peer", "company": "@calcom"},
                  company={"name": "meetbird", "domain": "cal.com", "industry": "management-consulting"})
    state, _ = await _lookup(world, "team@cal.com")
    assert state.evidence["company"] == {"domain": "cal.com"}
    assert state.fills["social"]["value"] == "cal.com"
    facts = {f["key"]: f for f in first_run._facts({"evidence": state.evidence, "fills": state.fills})}
    # the welcome greets no one by the company's name: the company is the line above it, as its domain
    assert facts["intro"]["value"] == "" and facts["intro"]["label"] == "cal.com"


def test_a_company_record_for_another_company_is_not_believed():
    from treg.application.onboard.lookup import _same_company
    assert _same_company("Cal.com", ["cal.com", "calcom"])
    assert _same_company("Pixelforge", ["pixelforge.dev", ""])
    assert _same_company("Linear", ["", "Linear"])
    assert not _same_company("meetbird", ["cal.com", "calcom"])


def test_a_homepage_reads_as_title_meta_and_visible_text():
    html = ("<html><head><title> Pixelforge </title><meta name='description' content='AI design &amp; code'>"
            "<meta name='keywords' content='ui, design'><style>.x{}</style><script>var secret=1</script></head>"
            "<body><nav>Pricing</nav><h1>Design   with AI</h1><noscript>enable js</noscript><svg><text>logo</text></svg>"
            "<p>Export code.</p></body></html>")
    assert page.parse(html, "https://pixelforge.dev").text == ("Title: Pixelforge\nDescription: AI design & code\nKeywords: ui, design\n"
                                    "Pricing Design with AI Export code.")


def test_a_homepage_names_the_tools_and_pixels_in_its_code_and_its_icon():
    html = ("<html><head><link rel='icon' href='/favicon.ico'><link rel='icon' sizes='192x192' href='/i192.png'>"
            "<link rel='apple-touch-icon' sizes='180x180' href='https://cdn.example/touch.png'>"
            "<script src='https://cdn.shopify.com/s/files/app.js'></script>"
            "<script async src='https://www.googletagmanager.com/gtag/js?id=G-ABC123'></script>"
            "<script>gtag('config', 'AW-123456789');</script>"
            "<script src='https://analytics.tiktok.com/i18n/pixel/events.js'></script></head>"
            "<body><p>We love posthog and shopify as words, not code.</p></body></html>")
    got = page.parse(html, "https://shop.example/")
    assert got.tools == ["Shopify", "Google Analytics"]       # a word in the copy is not a tool
    assert got.pixels == ["Google Ads", "TikTok"]
    assert got.icon == "https://cdn.example/touch.png"         # the touch icon beats a large plain icon
    assert page.parse("<link rel='icon' href='/favicon.ico'>", "https://a.example/").icon == ""


async def test_a_plain_png_favicon_is_the_mark_only_when_it_is_big_enough(monkeypatch):
    monkeypatch.setattr(ssrf, "host_is_public", lambda host: True)

    def png(width: int) -> bytes:
        return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + width.to_bytes(4, "big") + width.to_bytes(4, "big")

    html = "<html><head><link rel='shortcut icon' href='//cdn.{s}/favicon.png?v=1'></head><body>Shop</body></html>"
    files = {"https://big.example": html.format(s="big.example"), "https://cdn.big.example/favicon.png?v=1": png(64),
             "https://small.example": html.format(s="small.example"), "https://cdn.small.example/favicon.png?v=1": png(16)}

    def serve(r: httpx.Request) -> httpx.Response:
        body = files.get(str(r.url).rstrip("/"))
        if body is None:
            return httpx.Response(404)
        return (httpx.Response(200, text=body, headers={"content-type": "text/html"}) if isinstance(body, str)
                else httpx.Response(200, content=body, headers={"content-type": "image/png"}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as http:
        assert (await page.read(http, "big.example")).icon == "https://cdn.big.example/favicon.png?v=1"
        assert (await page.read(http, "small.example")).icon == ""       # 16px would show blurred


async def test_a_homepage_read_checks_every_redirect_target(monkeypatch):
    seen = []
    monkeypatch.setattr(ssrf, "host_is_public", lambda host: seen.append(host) or host != "internal.example")
    pages = {"https://a.example": httpx.Response(301, headers={"location": "https://internal.example/"}),
             "https://b.example": httpx.Response(200, text="<title>B</title>", headers={"content-type": "text/html"})}
    transport = httpx.MockTransport(lambda r: pages.get(str(r.url).rstrip("/"), httpx.Response(404)))
    async with httpx.AsyncClient(transport=transport) as http:
        assert (await page.read(http, "a.example")).text == ""
        assert (await page.read(http, "b.example")).text == "Title: B"
    assert seen == ["a.example", "internal.example", "b.example"]
