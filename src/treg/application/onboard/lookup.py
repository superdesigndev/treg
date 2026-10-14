"""The setup lookup: what we can learn about a new user from their email, and the first tasks it picks.

The chain, each step optional and each recorded as a row the dashboard shows as it lands:

1. GitHub: the login they signed in with, or the account whose public commits carry their email.
2. Company: the company record for their work-email domain, else for their GitHub blog or company.
3. Homepage: the company's own site, read.
4. Candidates: the LLM proposes task inputs from that evidence only, and may leave a slot empty.
5. Ranking: the judge rates all ten tasks for this user in one request; where they came from (the
   landing page, the referrer) is evidence here and is never shown to them.
6. Checks: a proposed search term is kept only when its Google results look like the user's own
   market; the first product site among those results is the competitor; a TikTok topic is kept
   only when its top videos fit. A slot nothing grounds keeps its labelled example.

Every catalog call is a house call (`house_calls`), capped per signup. A step that is not
configured, fails or runs out of budget or time is skipped; the lookup itself never raises.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from ... import analytics
from ...config import get_settings
from ...domain.catalog.routing.paths import email_domain
from ...infra import llm
from ..house_calls import HouseCalls
from . import page
from .tasks import DEFAULT_RANK, RECOMMENDED, TASKS

log = logging.getLogger("treg.onboarding")

DEADLINE_S = 40.0
COMPANY_TIMEOUT_S = 3.0      # a record is a bonus; a small company's waterfall takes minutes
HOMEPAGE_TIMEOUT_S = 5.0     # the catalog's readers, only for a page that is mostly script
THIN_PAGE_CHARS = 300
SERP_TIMEOUT_S = 6.0         # a term's results later than this are dropped; the slowest term holds the bento
SERP_PROVIDER = "scrapecreators"   # the routed SERP's fastest steady child (about 2s); a miss still falls through
# Hosts a GitHub "blog" field points at that are not the person's company site.
_NOT_A_COMPANY = ("github.com", "github.io", "medium.com", "substack.com", "linkedin.com", "x.com",
                  "twitter.com", "youtube.com", "notion.site", "dev.to", "hashnode.dev", "linktr.ee")
# Results that rank for everything and are nobody's competitor.
_NOT_A_COMPETITOR = ("reddit.com", "youtube.com", "wikipedia.org", "medium.com", "linkedin.com", "quora.com",
                     "g2.com", "capterra.com", "producthunt.com", "x.com", "twitter.com", "facebook.com",
                     "instagram.com", "tiktok.com", "github.com", "forbes.com", "amazon.com", "apple.com",
                     "google.com", "microsoft.com")


@dataclass
class Hints:
    door: str = ""             # email | github | google
    github_login: str = ""     # from the GitHub door
    name: str = ""             # from the Google door
    landing: str = ""          # first-touch landing path
    referrer: str = ""
    utm_source: str = ""


@dataclass
class State:
    steps: list[dict] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    ranking: list[dict] = field(default_factory=list)
    fills: dict[str, dict] = field(default_factory=dict)
    llm_cost_usd: float = 0.0
    # The tasks can be shown: ranked, with what needs no check filled in. The checks keep refining
    # the inputs after this; the dashboard picks the new values up as it polls.
    ready: bool = False
    pending: list[str] = field(default_factory=list)   # tasks a running check may still fill

    def step(self, key: str, state: str, detail: str = "") -> None:
        row = {"key": key, "state": state, "detail": detail}
        self.steps = [s for s in self.steps if s["key"] != key] + [row]

    def view(self) -> dict:
        return {"steps": self.steps, "evidence": self.evidence, "ranking": self.ranking, "fills": self.fills,
                "ready": self.ready, "pending": self.pending, "llm_cost_usd": round(self.llm_cost_usd, 6)}


Save = Callable[[State, int], Awaitable[None]]


def _host(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    host = urlparse(url if "://" in url else "https://" + url).hostname or ""
    return host.lower().removeprefix("www.")


def _is(host: str, suffixes: tuple[str, ...]) -> bool:
    return any(host == s or host.endswith("." + s) for s in suffixes)


class Lookup:
    def __init__(self, email: str, hints: Hints, http: httpx.AsyncClient, save: Save,
                 transport: httpx.AsyncBaseTransport | None = None):
        s = get_settings()
        self.s, self.email, self.hints, self.http, self.save, self.transport = s, email, hints, http, save, transport
        self.house = (HouseCalls(http, s.onboarding_treg_token, "onboarding", s.onboarding_treg_url)
                      if s.onboarding_treg_token else None)
        self.state = State()
        self._fuller_page: asyncio.Task | None = None

    # ---------------------------------------------------------------------------------- plumbing
    async def _commit(self) -> None:
        try:
            await self.save(self.state, self.house.cost_micro if self.house else 0)
        except Exception as exc:  # noqa: BLE001 - a lost progress row is not a failed lookup
            log.warning("onboarding progress not saved: %s", exc)

    def _budget_left(self) -> bool:
        return bool(self.house) and self.house.cost_micro < self.s.onboarding_max_house_micro

    async def _call(self, endpoint: str, body: dict, kind: str, *, method: str = "POST",
                    params: dict | None = None, timeout: float = 12, prefer: str = "") -> dict | None:
        if not self._budget_left():
            return None
        a = await self.house.request(method, endpoint, kind, json=body if method == "POST" else None,
                                     params=params, timeout=timeout,
                                     headers={"X-Treg-Route-Max-Cost": "0.02",
                                              **({"X-Treg-Route-Prefer": prefer} if prefer else {})})
        if a.status != 200:
            return None
        return a.body

    async def _ask(self, state: dict, questions: dict[str, str]) -> dict[str, float] | None:
        """Yes/no probabilities from Jev, a catalog endpoint like any other: one house call, every
        question judged independently over the same state. None when it could not answer."""
        if not questions:
            return {}
        d = await self._call(JEV_ENDPOINT, {"model": JEV_MODEL, "state": _markdown(state), "questions": {
            k: {"type": "noul", "instructions": q, "criteria": {"true": "yes", "false": "no"}}
            for k, q in questions.items()}}, "judge")
        answers = (d or {}).get("answers")
        if not isinstance(answers, dict):
            return None
        out = {}
        for k in questions:
            try:
                out[k] = float(answers[k]["noul"])
            except (KeyError, TypeError, ValueError):
                return None
        return out

    # ---------------------------------------------------------------------------------- the run
    async def run(self) -> State:
        try:
            async with asyncio.timeout(DEADLINE_S):
                await self._run()
        except TimeoutError:
            self.state.step("personal", "skip", "Ran out of time; the rest use examples")
        except Exception as exc:  # noqa: BLE001 - the lookup never fails the onboarding
            log.warning("onboarding lookup failed: %s", exc)
        self._finish_fills()
        if not self.state.ranking:
            self.state.ranking = [{"id": t, "score": None} for t in DEFAULT_RANK]
        await self._commit()
        return self.state

    async def _run(self) -> None:
        domain = email_domain(self.email) or ""
        self.state.evidence["signin"] = {
            "door": self.hints.door, "work_email_domain": domain, "personal_email": not domain,
            "landing_page": self.hints.landing, "referrer": self.hints.referrer, "utm_source": self.hints.utm_source,
            **({"name": self.hints.name} if self.hints.name else {}),
        }
        if domain:
            # A work address names the site: GitHub only adds the person, so it runs alongside the
            # company record and the homepage instead of in front of them.
            await asyncio.gather(self._github(), self._company(domain), self._homepage(domain))
        else:
            await self._github()
            await self._commit()
            # With the company's site on GitHub, reading it and asking for its record run side by
            # side; with only a name, the record comes first and names the site.
            site = (self.state.evidence.get("github") or {}).get("company_site", "")
            if site:
                await asyncio.gather(self._company(""), self._homepage(site))
            else:
                await self._company("")
                await self._commit()
                await self._homepage()
        await self._commit()
        if not self._has_evidence():
            self.state.step("personal", "skip", "Nothing public to go on yet; showing the tasks new teams start with")
            return
        # The tasks are shown once they are ranked and the LLM has proposed their inputs: by then it is
        # known which tasks have an input of the user's own (or one being checked), and those lead.
        # The checks land on them afterwards.
        candidates = asyncio.create_task(self._candidates())
        try:
            self.state.ranking = await self._rank()
            cand = await candidates or {}
            self._known_fills()
            self._proposed_fills(cand)
            work = self._check_work(cand)
            self.state.step("personal", "running", "Checking each idea against real results")
            self.state.ready = True
            await self._commit()
            await asyncio.gather(*work)
            self.state.pending = []
            picked = [k for k in self.state.fills if self.state.fills[k]["source"] != "example"]
            self.state.step("personal", "ok" if picked else "miss", "Checked each idea against real results"
                            if picked else "Nothing could be checked; showing examples")
        finally:
            candidates.cancel()
            if self._fuller_page is not None:
                self._fuller_page.cancel()

    def _has_evidence(self) -> bool:
        e = self.state.evidence
        return bool(e.get("github") or e.get("company") or e.get("homepage") or e["signin"]["work_email_domain"])

    # ---------------------------------------------------------------------------------- 1. GitHub
    async def _github(self) -> None:
        token = self.s.onboarding_github_token
        gh = {"Accept": "application/vnd.github+json", "User-Agent": "treg",
              **({"Authorization": f"Bearer {token}"} if token else {})}
        api = self.s.github_api_url.rstrip("/")
        login = self.hints.github_login
        try:
            if not login:
                if not token:
                    self.state.step("github", "skip")
                    return
                r = await self.http.get(f"{api}/search/commits", params={"q": f"author-email:{self.email}", "per_page": 1},
                                        headers=gh, timeout=8)
                items = (r.json().get("items") or []) if r.status_code == 200 else []
                login = ((items[0].get("author") or {}).get("login") or "") if items else ""
            if not login:
                self.state.step("github", "miss", "No public commits under this email")
                return
            r = await self.http.get(f"{api}/users/{login}", headers=gh, timeout=8)
            p = r.json() if r.status_code == 200 else {}
        except (httpx.HTTPError, ValueError) as exc:
            log.info("github lookup failed: %s", exc)
            self.state.step("github", "miss", "GitHub did not answer")
            return
        profile = {k: p.get(k) for k in ("login", "name", "company", "blog", "bio", "location", "public_repos",
                                         "avatar_url")
                   if p.get(k) not in (None, "")}
        profile.setdefault("login", login)
        if isinstance(profile.get("company"), str):
            profile["company"] = profile["company"].strip().lstrip("@").strip()
            if site := await self._org_site(api, gh, profile["company"]):
                profile["company_site"] = site
        self.state.evidence["github"] = profile
        detail = "@" + profile["login"] + (f" · {profile['company']}" if profile.get("company") else "")
        self.state.step("github", "ok", detail)

    async def _org_site(self, api: str, gh: dict, company: str) -> str:
        """The company's own site, from GitHub: the organisation named like the profile's company
        field, then its profile site or its most-starred repository's homepage. Free and fast, where
        a company lookup by name alone is slow and often misses."""
        want = re.sub(r"[^a-z0-9]", "", company.lower())
        if len(want) < 3:
            return ""
        try:
            r = await self.http.get(f"{api}/search/users", params={"q": f"{company} type:org", "per_page": 5},
                                    headers=gh, timeout=6)
            items = (r.json().get("items") or []) if r.status_code == 200 else []
            org = next((i["login"] for i in items if isinstance(i, dict) and isinstance(i.get("login"), str)
                        and re.sub(r"[^a-z0-9]", "", i["login"].lower()).startswith(want)), "")
            if not org:
                return ""
            r = await self.http.get(f"{api}/orgs/{org}", headers=gh, timeout=6)
            site = _host((r.json() if r.status_code == 200 else {}).get("blog") or "")
            if not site:
                r = await self.http.get(f"{api}/orgs/{org}/repos", params={"sort": "updated", "per_page": 100},
                                        headers=gh, timeout=6)
                repos = [x for x in (r.json() if r.status_code == 200 else []) if isinstance(x, dict)]
                repos.sort(key=lambda x: -(x.get("stargazers_count") or 0))
                site = next((_host(x["homepage"]) for x in repos if x.get("homepage")), "")
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            log.info("github org lookup failed: %s", exc)
            return ""
        return site if site and not _is(site, _NOT_A_COMPANY) else ""

    # ---------------------------------------------------------------------------------- 2. company
    async def _company(self, work_domain: str) -> None:
        # Most sure first: the work email's own domain, then the site of the company named on the
        # GitHub profile, then that name alone, then the profile's own site, which is as often a
        # personal blog as a company.
        gh = self.state.evidence.get("github") or {}
        name = gh.get("company") or ""
        blog = _host(gh.get("blog") or "")
        blog = blog if blog and not _is(blog, _NOT_A_COMPANY) else ""
        if work_domain or gh.get("company_site"):
            work_domain = work_domain or gh["company_site"]
            body, label = {"domain": work_domain}, work_domain
        elif name:
            body, label = {"name": name}, name
        elif blog:
            body, label = {"domain": blog}, blog
        else:
            self.state.step("company", "skip")
            return
        # A record is a bonus, not a step to wait on: a small company sends the waterfall through
        # every provider, which takes minutes.
        d = (await self._call("treg.companies.enrich", body, "company", timeout=COMPANY_TIMEOUT_S)
             if self.house else None)
        out = (d or {}).get("output") or {}
        domain = work_domain
        if not out.get("name") and not domain:
            self.state.step("company", "miss", f"No company record for {label}")
            return
        if out.get("name") and not _same_company(out["name"], [domain, name]):
            # A provider's record for another company (a domain's previous owner, a bad merge) would
            # steer every task; the domain alone is the better evidence.
            log.info("onboarding: company record %r does not match %r", out.get("name"), label)
            out = {}
        company = {k: out.get(k) for k in ("name", "domain", "website", "description", "industry", "employees",
                                           "founded", "location") if out.get(k) not in (None, "")}
        company.setdefault("domain", domain or _host(company.get("website") or ""))
        if not company.get("domain"):
            company.pop("domain")
        self.state.evidence["company"] = company
        label = " · ".join(str(x) for x in (company.get("name"), company.get("domain"), company.get("industry")) if x)
        self.state.step("company", "ok", label)

    # ---------------------------------------------------------------------------------- 3. homepage
    async def _homepage(self, site: str = "") -> None:
        c = self.state.evidence.get("company") or {}
        site = site or c.get("domain") or _host(c.get("website") or "")
        if not site:
            self.state.step("homepage", "skip")
            return
        read = await page.read(self.http, site)
        text = read.text
        if page.visible_chars(text) < THIN_PAGE_CHARS:   # a client-rendered page: ask the catalog's readers
            fuller = asyncio.create_task(self._call("treg.web.extract", {"url": f"https://{site}"}, "homepage",
                                                    timeout=HOMEPAGE_TIMEOUT_S))
            if text:
                # Its title and description are enough to rank the tasks; the fuller read goes on in
                # the background, for the LLM, which runs after the tasks are shown.
                self._fuller_page = fuller
            else:
                text = _page_text(await fuller)
        if not text:
            self.state.step("homepage", "miss", f"Could not read {site}")
            return
        # what the site's code loads is read from its HTML, so a client-rendered page still has it
        self.state.evidence["homepage"] = {"url": f"https://{site}", "text": text[:4000],
                                           **({"tools": read.tools} if read.tools else {}),
                                           **({"ad_pixels": read.pixels} if read.pixels else {}),
                                           **({"icon": read.icon} if read.icon else {})}
        self.state.step("homepage", "ok", f"Read {site}")

    # ---------------------------------------------------------------------------------- 4. candidates
    async def _candidates(self) -> dict | None:
        if not self.s.ai_gateway_api_key:
            return None
        if self._fuller_page is not None:
            fuller = _page_text(await self._fuller_page)
            hp = self.state.evidence.get("homepage") or {}
            if len(fuller) > len(hp.get("text") or ""):
                hp["text"] = fuller[:4000]
        a = await llm.structured(_SYSTEM, _evidence_prompt(self.state.evidence), _SLOTS_SCHEMA,
                                 api_key=self.s.ai_gateway_api_key, model=self.s.onboarding_llm_model,
                                 transport=self.transport)
        self.state.llm_cost_usd += a.cost_usd
        analytics.capture(self.email, "$ai_generation", {
            "$ai_model": self.s.onboarding_llm_model, "$ai_provider": "vercel-ai-gateway",
            "$ai_input_tokens": a.input_tokens, "$ai_output_tokens": a.output_tokens,
            "$ai_total_cost_usd": a.cost_usd, "$ai_latency": a.ms / 1000, "$ai_is_error": a.value is None,
            **({"$ai_error": a.error} if a.error else {}), "ai_call_purpose": "onboarding"})
        return a.value

    # ---------------------------------------------------------------------------------- 5. ranking
    async def _rank(self) -> list[dict]:
        questions = {t.id: f"As one of the first things they try, this new user would want their AI agent to "
                           f"{t.question}. <signin> says how and from where they arrived; <github>, <company> and "
                           "<homepage> are what is public about them. Judge from what they build and who they sell to."
                     for t in TASKS.values()}
        answers = await self._ask(_judge_state(self.state.evidence), questions)
        if not answers:
            return [{"id": t, "score": None} for t in DEFAULT_RANK]
        scored = [{"id": t, "score": round(float(answers.get(t) or 0.0), 3)} for t in TASKS]
        order = {t: i for i, t in enumerate(DEFAULT_RANK)}
        return sorted(scored, key=lambda r: (-r["score"], order[r["id"]]))

    # ---------------------------------------------------------------------------------- 6. checks
    def _known_fills(self) -> None:
        """What needs no LLM and no check: the user's own company and brand."""
        ev, fills = self.state.evidence, self.state.fills
        company, gh = ev.get("company") or {}, ev.get("github") or {}
        brand = company.get("name") or gh.get("company") or ""
        # A GitHub company that is only the domain run together ("calcom" for cal.com) is a handle,
        # not a name people write; searched as is it finds other things. The domain is what they write.
        if not company.get("name") and company.get("domain") and _squash(brand) == _squash(company["domain"]):
            brand = company["domain"]
        if brand:
            fills["social"] = {"value": brand, "note": "your company", "source": "company" if company.get("name") else "github"}
        if company.get("domain") or brand:
            fills["company"] = {"value": company.get("domain") or brand, "note": "your company",
                                "source": "company" if company.get("domain") else "github"}

    def _proposed_fills(self, cand: dict) -> None:
        """The LLM's inputs that need no check."""
        fills, company = self.state.fills, self.state.evidence.get("company") or {}
        if v := _first(cand.get("maps_query")):
            fills["maps"] = {"value": v, "note": "possible customers near you", "source": "llm"}
        own = company.get("domain") or ""
        target = next((_host(t["domain"]) for t in cand.get("target_companies") or []
                       if isinstance(t, dict) and isinstance(t.get("domain"), str) and _host(t["domain"])
                       and not (own and _is(_host(t["domain"]), (own,)))), "")
        if target:
            fills["people"] = {"value": target, "note": "a company like your customers", "source": "llm"}

    def _check_work(self, cand: dict) -> list:
        """The checks to run, and the tasks they may fill (`pending` until they land)."""
        ev = self.state.evidence
        company, gh = ev.get("company") or {}, ev.get("github") or {}
        brand = company.get("name") or gh.get("company") or ""
        terms = [c["value"] for c in cand.get("search_terms") or [] if c.get("value")][:3]
        topics = [c["value"] for c in cand.get("tiktok_topics") or [] if c.get("value")][:2]
        work = []
        if terms:
            work.append(self._check_terms(terms, brand, company.get("domain") or ""))
            self.state.pending += ["keywords", "serp", "scrape"]
        if topics:
            work.append(self._check_topics(topics + terms[:1]))
            self.state.pending.append("videos")
        return work

    async def _serp(self, term: str) -> dict | None:
        """One term's Google results from the fastest provider, or None past SERP_TIMEOUT_S: the
        bento waits on the slowest of the terms, so a slow answer is dropped, not waited for."""
        try:
            return await asyncio.wait_for(self._call("treg.google.serp.organic", {"q": term}, "check_serp",
                                                     prefer=SERP_PROVIDER, timeout=SERP_TIMEOUT_S), SERP_TIMEOUT_S)
        except TimeoutError:
            return None

    async def _check_terms(self, terms: list[str], brand: str, own: str) -> None:
        serps = await asyncio.gather(*(self._serp(t) for t in terms))
        results = {t: _serp_rows(d) for t, d in zip(terms, serps)}
        results = {t: rows for t, rows in results.items() if rows}
        if not results:
            return
        ids = list(results)
        answers = await self._ask(
            {"user": _judge_state(self.state.evidence),
             "searches": [{"i": i, "query": t, "results": results[t][:10]} for i, t in enumerate(ids)]},
            {f"s{i}": f"The Google results for search {i} in <searches> are mostly products, tools or companies "
                      "like the user's own, so a buyer of what the user sells would type this search."
             for i in range(len(ids))})
        if answers:
            best_i = max(range(len(ids)), key=lambda i: float(answers.get(f"s{i}") or 0.0))
            if float(answers.get(f"s{best_i}") or 0.0) < 0.5:
                return
        else:
            best_i = 0       # no judge: the LLM's first term, as long as it returned results
        term = ids[best_i]
        note = "from your site; its Google results are tools like yours"
        self.state.fills["keywords"] = {"value": term, "note": note, "source": "checked"}
        self.state.fills["serp"] = {"value": term, "note": note, "source": "checked"}
        competitor = await self._competitor(results[term], own, brand)
        if competitor:
            pos, host = competitor
            # A pricing page is the page worth a table; the home page when there is none.
            pricing = await page.exists(self.http, f"https://{host}/pricing")
            self.state.fills["scrape"] = {
                "value": f"{host}/pricing" if pricing else host, "source": "checked", "pos": pos, "term": term,
                "note": ("a competitor's pricing page" if pricing else "a competitor") + ": " + ranked(pos, term)}

    async def _competitor(self, rows: list[dict], own: str, brand: str) -> tuple[int, str] | None:
        """The highest-ranked result that is a product built for the same job as the user's, by the
        judge; without one, the first result that is not a giant or the user's own site."""
        brand_l = brand.lower()
        rows = [r for r in rows if r["host"] and not (own and _is(r["host"], (own,)))
                and not _is(r["host"], _NOT_A_COMPETITOR) and not (brand_l and brand_l in r["host"])]
        if not rows:
            return None
        answers = await self._ask(
            {"user": _judge_state(self.state.evidence), "results": [{"i": i, **r} for i, r in enumerate(rows)]},
            {f"r{i}": f"Result {i} in <results> is the site of a product built for the same job as the user's "
                      "own product (a direct competitor), not an article, a list or a general-purpose tool."
             for i in range(len(rows))})
        if answers is not None:
            rows = [r for i, r in enumerate(rows) if float(answers.get(f"r{i}") or 0.0) >= 0.5]
        if not rows:
            return None
        r = rows[0]
        return (int(r["pos"]) if str(r["pos"]).isdigit() else 0), r["host"]

    async def _check_topics(self, topics: list[str]) -> None:
        topics = list(dict.fromkeys(topics))
        found = await asyncio.gather(*(self._call("treg.tiktok.search.videos", {"q": t}, "check_tiktok") for t in topics))
        sets = {t: _video_titles(d) for t, d in zip(topics, found)}
        sets = {t: v for t, v in sets.items() if v}
        if not sets:
            return
        ids = list(sets)
        answers = await self._ask(
            {"user": _judge_state(self.state.evidence),
             "topics": [{"i": i, "topic": t, "top_videos": sets[t][:12]} for i, t in enumerate(ids)]},
            {f"t{i}": f"The top TikTok videos for topic {i} in <topics> are about the user's own product "
                      "category, so studying them would help the user make videos for it."
             for i in range(len(ids))})
        if not answers:
            return
        best_i = max(range(len(ids)), key=lambda i: float(answers.get(f"t{i}") or 0.0))
        if float(answers.get(f"t{best_i}") or 0.0) >= 0.5:
            self.state.fills["videos"] = {"value": ids[best_i], "note": "your category, checked against what TikTok returns",
                                          "source": "checked"}

    def _finish_fills(self) -> None:
        for tid, t in TASKS.items():
            if tid not in self.state.fills:
                self.state.fills[tid] = {"value": t.example[0], "note": t.example[1], "source": "example"}


# ---------------------------------------------------------------------------------- reading answers
def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _same_company(record_name: str, known: list[str]) -> bool:
    """Whether a record's name fits what we already know: a domain's first label or the name on the
    GitHub profile ("Cal.com" fits cal.com and calcom; "meetbird" fits neither)."""
    rec = _squash(record_name)
    keys = [_squash(k.split(".")[0] if "." in k and " " not in k else k) for k in known if k]
    keys = [k for k in keys if len(k) >= 2]
    if not rec or not keys:
        return True
    return any(k in rec or rec in k for k in keys)


def _first(cands) -> str:
    for c in cands or []:
        if isinstance(c, dict) and isinstance(c.get("value"), str) and c["value"].strip():
            return c["value"].strip()
    return ""



def _rows(d: dict | None, key: str) -> list[dict]:
    o = (d or {}).get("output") or {}
    rows = o.get(key) if isinstance(o, dict) else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _pick(row: dict, *keys: str):
    for k in keys:
        cur = row
        for part in k.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur not in (None, ""):
            return cur
    return None


def _serp_rows(d: dict | None) -> list[dict]:
    out = []
    for i, r in enumerate(_rows(d, "results"), 1):
        link = _pick(r, "link", "url", "href")
        if isinstance(link, str):
            out.append({"pos": _pick(r, "position", "rank") or i, "title": str(_pick(r, "title") or "")[:120],
                        "host": _host(link)})
    return out


def _video_titles(d: dict | None) -> list[str]:
    out = []
    for v in _rows(d, "videos"):
        v = v.get("aweme_info", v)
        t = _pick(v, "desc", "title", "description", "text")
        if isinstance(t, str) and t.strip():
            out.append(t.strip().replace("\n", " ")[:140])
    return out


def _page_text(d: dict | None) -> str:
    pages = _rows(d, "pages")
    if not pages:
        return ""
    p = pages[0]
    for k in ("markdown", "text", "content", "body", "html"):
        v = p.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return json.dumps(p, ensure_ascii=False)[:4000]


def ranked(pos: int, term: str) -> str:
    """A result's place on Google, as the page says it."""
    return f"#{pos} on Google for \u201c{term}\u201d"


JEV_ENDPOINT = "openrouter.ai-judge.decide"
JEV_MODEL = "typesafe/jev-1.13"
_STATE_MAX = 9000


def _markdown(state: dict) -> str:
    """Jev's state: bounded Markdown, one XML-style section per key, untrusted text escaped."""
    esc = lambda t: t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # noqa: E731
    parts = ["# A new user of treg and what is public about them"]
    for k, v in state.items():
        body = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, indent=1)
        parts.append(f"<{k}>\n{esc(body)}\n</{k}>")
    return "\n\n".join(parts)[:_STATE_MAX]


def _judge_state(ev: dict) -> dict:
    hp = ev.get("homepage") or {}
    return {**{k: v for k, v in ev.items() if k != "homepage"},
            **({"homepage": {"url": hp.get("url"), "text": (hp.get("text") or "")[:1500],
                             **{k: hp[k] for k in ("tools", "ad_pixels") if hp.get(k)}}} if hp else {})}


# ---------------------------------------------------------------------------------- the LLM ask
_SYSTEM = ("You fill in the inputs of first tasks for a new user of treg, a tool catalog that lets an AI "
           "agent call data and generation APIs. Use only the evidence given. Do not invent companies, people "
           "or numbers that the evidence does not support; when a slot cannot be filled from the evidence, "
           "return an empty list for it.")


def _evidence_prompt(ev: dict) -> str:
    s = ev.get("signin") or {}
    parts = ["<evidence>"]
    parts.append(f"<signin>{'Work email at ' + s['work_email_domain'] if s.get('work_email_domain') else 'Personal email'}"
                 f"{'; name ' + s['name'] if s.get('name') else ''}.</signin>")
    for key in ("github", "company"):
        if ev.get(key):
            parts.append(f"<{key}>{json.dumps(ev[key], ensure_ascii=False)}</{key}>")
    if hp := ev.get("homepage"):
        parts.append(f'<homepage url="{hp["url"]}">\n{hp["text"]}\n</homepage>')
        if hp.get("tools") or hp.get("ad_pixels"):
            parts.append(f"<site_code>Loads: {', '.join(hp.get('tools') or []) or 'nothing known'}. "
                         f"Ad pixels: {', '.join(hp.get('ad_pixels') or []) or 'none'}.</site_code>")
    parts.append("</evidence>")
    parts.append(
        'Every candidate has "value" and "why" (a short quote or fact from the evidence that supports it).\n'
        '- "search_terms": up to 3, phrases a potential buyer of this product would type into Google (lowercase, 2-5 words).\n'
        '- "tiktok_topics": up to 2, topics whose top TikTok videos this user would want to study.\n'
        '- "target_companies": up to 3 named companies, with "domain", that plausibly use or would buy this product. '
        'Only if the evidence names them or makes them obvious; otherwise [].\n'
        '- "maps_query": at most 1, "<business type> in <city>", only if the product sells to local businesses; otherwise [].')
    return "\n".join(parts)


def _list_of(item: dict) -> dict:
    return {"type": "array", "items": item}


_CAND = {"type": "object", "additionalProperties": False, "required": ["value", "why"],
         "properties": {"value": {"type": "string"}, "why": {"type": "string"}}}
_COMPANY = {"type": "object", "additionalProperties": False, "required": ["value", "domain", "why"],
            "properties": {"value": {"type": "string"}, "domain": {"type": "string"}, "why": {"type": "string"}}}
_SLOTS_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["search_terms", "tiktok_topics", "target_companies", "maps_query"],
    "properties": {"search_terms": _list_of(_CAND), "tiktok_topics": _list_of(_CAND),
                   "target_companies": _list_of(_COMPANY), "maps_query": _list_of(_CAND)},
}


def is_recommended(score: float | None) -> bool:
    return score is not None and score >= RECOMMENDED
