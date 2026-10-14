"""The first-run flow behind `onboarding_v2`: a new user gets a team, a lookup and five first tasks.

`remember_hints` runs at sign-in (what the door knew: a GitHub login, a Google name). `start` makes
the user's first team through the ordinary signup door (so the signup credit, attribution and
referral behave exactly as for a named team), then runs the lookup in the background; the dashboard
polls `view`. The first call itself is the user's own `/call/` from the dashboard, on their own
credit; nothing here spends it. This module is the only writer of `OnboardingProfile`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import timedelta
from urllib.parse import unquote

import httpx
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from ... import crypto, ratestore
from ...config import get_settings
from ...domain.catalog.routing.paths import email_domain
from ...infra.db import session_maker
from ...models import Membership, OnboardingProfile, Org, User
from ...timeutil import utcnow_naive
from .. import signup
from ..house_calls import HouseCalls
from . import OnboardError
from .lookup import DEADLINE_S, Hints, Lookup, State, _squash, is_recommended, ranked
from .tasks import DEFAULT_RANK, SHOWN, TASKS, USE_CASES, public_library

log = logging.getLogger("treg.onboarding")

_owners: dict[int, asyncio.Task] = {}


def enabled(email: str) -> bool:
    """The flow for everyone, or for this listed address: no experiment arm decides."""
    s = get_settings()
    return s.onboarding_v2 or _listed(email)


def in_experiment(email: str) -> bool:
    """Whether this address may enter the `onboarding-v2` experiment: the experiment is on and its
    domain is not one anyone can get an address at (`paths.email_domain`: free, ISP, disposable and
    alias mail) or a school's. A personal domain gets in too; both arms take it alike, and a lookup
    that finds nothing asks what the agent will do first."""
    if not get_settings().onboarding_v2_experiment:
        return False
    d = email_domain(email)
    return bool(d) and not _SCHOOL.search(d)


_SCHOOL = re.compile(r"\.(edu|edu\.[a-z]{2}|ac\.[a-z]{2})$")


async def _named_team(db, email: str) -> bool:
    """Whether this account is in a team; the legacy door's personal team (named after the email) is
    not one the user named."""
    return (await db.execute(select(Membership.id).join(Org, Org.id == Membership.org_id).join(
        User, User.id == Membership.user_id).where(User.email == email, Org.name != email).limit(1))).first() is not None


async def allowed(email: str) -> bool:
    """Who may start and read the flow: everyone it is on for, and the experiment's work addresses
    (the dashboard sends only its `test` arm here)."""
    return enabled(email) or in_experiment(email)


def _pack(value: dict) -> str:
    return crypto.encrypt(json.dumps(value, ensure_ascii=True, separators=(",", ":")))


def _unpack(value: str) -> dict:
    return json.loads(crypto.decrypt(value)) if value else {}


async def remember_hints(user_id: int, email: str, *, door: str, github_login: str = "", name: str = "") -> None:
    """Keep what a sign-in door knew about a brand-new user until their setup runs. Best effort."""
    if not (enabled(email) or get_settings().onboarding_v2_experiment) or not (github_login or name):
        return
    try:
        async with session_maker() as db:
            if await _profile(db, user_id):
                return
            db.add(OnboardingProfile(user_id=user_id, status="pending", payload=_pack(
                {"hints": {"door": door, "github_login": github_login[:100], "name": name[:200]}})))
            await db.commit()
    except Exception as exc:  # noqa: BLE001 - a hint is never a reason a sign-in fails
        log.warning("onboarding hints not kept for user %s: %s", user_id, exc)


async def _profile(db, user_id: int) -> OnboardingProfile | None:
    return (await db.execute(select(OnboardingProfile).where(OnboardingProfile.user_id == user_id))).scalar_one_or_none()


def team_names(email: str) -> list[str]:
    """A first team's names before anyone has named it, best first: the work domain's company, then
    the person's, then a plain one. The next is tried when the registry refuses one (a reserved name)."""
    local = re.split(r"[+.]", email.split("@")[0])[0]
    names = [f"{local[:1].upper()}{local[1:]}'s team"] if local else []
    if domain := email_domain(email):
        label = domain.split(".")[0]
        names.insert(0, label[:1].upper() + label[1:])
    return names + ["My team"]


def team_name(email: str) -> str:
    return team_names(email)[0]


def _cookie_hints(utm_cookie: str, landing_cookie: str) -> dict:
    """Where the user came from (web/sitetrack.js): ranking evidence only, never shown to them."""
    utm = [p.strip()[:100] for p in unquote(utm_cookie or "").split("|")]
    landing = unquote(landing_cookie or "").strip()[:200]
    return {"landing": landing if landing.startswith("/") else "",
            "utm_source": utm[0] if utm else "", "referrer": utm[5] if len(utm) > 5 else ""}


async def start(user: User, *, ad_cookie: str, utm_cookie: str, referral_cookie: str, landing_cookie: str,
                http: httpx.AsyncClient) -> dict:
    if not await allowed(user.email):
        raise OnboardError("not_enabled")
    async with session_maker() as db:
        row = await _profile(db, user.id)
        if row is not None and row.org_id is not None:
            return await view(user)
        if await _named_team(db, user.email) or user.onboarded:
            raise OnboardError("already_onboarded")
    if not await _claim(user.id):
        return await _await_start(user)        # another request (a second tab) is making the team
    try:
        team = await _create_team(user, ad_cookie=ad_cookie, utm_cookie=utm_cookie, referral_cookie=referral_cookie)
    except BaseException:
        await _unclaim(user.id)
        raise
    async with session_maker() as db:
        row = await _profile(db, user.id)
        payload = _unpack(row.payload) if row else {}
        hints = {**(payload.get("hints") or {}), **_cookie_hints(utm_cookie, landing_cookie)}
        hints.setdefault("door", "email")
        payload = {"hints": hints, "team": {"slug": team["org"], "name": team["name"]}, "started": time.time()}
        if row is None:
            row = OnboardingProfile(user_id=user.id)
        row.org_id, row.status, row.payload = team["org_id"], "running", _pack(payload)
        db.add(row)
        await db.commit()
        profile_id = row.id
    task = asyncio.create_task(_run(profile_id, user.email, Hints(**hints), http))
    _owners[profile_id] = task
    task.add_done_callback(lambda t: _owners.pop(profile_id, None) if _owners.get(profile_id) is t else None)
    return await view(user)


CLAIM_STALE_S = 120     # a claim this old died with its process: the next start takes it over


async def _claim(user_id: int) -> bool:
    """Take the one right to make this user's first team: a `pending` row (or a stale claim) moves to
    `starting` in one statement, else a new `starting` row is inserted; the unique user_id decides
    a race. True for the request that won."""
    now = utcnow_naive()
    async with session_maker() as db:
        res = await db.execute(update(OnboardingProfile).where(
            OnboardingProfile.user_id == user_id, OnboardingProfile.org_id.is_(None),
            (OnboardingProfile.status == "pending") | ((OnboardingProfile.status == "starting")
                                                       & (OnboardingProfile.created_at < now - timedelta(seconds=CLAIM_STALE_S))),
        ).values(status="starting", created_at=now))
        if res.rowcount:
            await db.commit()
            return True
        if await _profile(db, user_id) is not None:
            return False
        db.add(OnboardingProfile(user_id=user_id, status="starting", created_at=now))
        try:
            await db.commit()
        except IntegrityError:
            return False
        return True


async def _unclaim(user_id: int) -> None:
    async with session_maker() as db:
        await db.execute(update(OnboardingProfile).where(
            OnboardingProfile.user_id == user_id, OnboardingProfile.org_id.is_(None)).values(status="pending"))
        await db.commit()


async def _await_start(user: User, wait_s: float = 15) -> dict:
    """A second start while the first is making the team: answer once the first has."""
    for _ in range(int(wait_s / 0.25)):
        async with session_maker() as db:
            row = await _profile(db, user.id)
        if row is not None and row.org_id is not None:
            return await view(user)
        await asyncio.sleep(0.25)
    raise OnboardError("starting")


async def _create_team(user: User, **cookies: str) -> dict:
    """The first team, under the first of `team_names` the registry accepts."""
    names = team_names(user.email)
    for name in names:
        try:
            return await signup.create_org(user=user, name=name, **cookies)
        except signup.SignupError as exc:
            if exc.kind not in ("reserved_name", "slug_conflict") or name == names[-1]:
                raise
    raise AssertionError("unreachable")


async def _run(profile_id: int, email: str, hints: Hints, http: httpx.AsyncClient) -> None:
    async def save(state: State, house_micro: int, *, final: bool = False) -> None:
        async with session_maker() as db:
            row = await db.get(OnboardingProfile, profile_id)
            if row is None:
                return
            payload = _unpack(row.payload)
            payload.update(state.view())
            row.payload, row.house_cost_micro = _pack(payload), house_micro
            if final:
                row.status, row.finished_at = "done", utcnow_naive()
            elif state.ready and row.status == "running":
                row.status = "ready"
            db.add(row)
            await db.commit()

    lookup = Lookup(email, hints, http, save)
    try:
        state = await lookup.run()
        await save(state, lookup.house.cost_micro if lookup.house else 0, final=True)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("onboarding run %s failed: %s", profile_id, exc)
        async with session_maker() as db:
            row = await db.get(OnboardingProfile, profile_id)
            if row is not None:
                row.status, row.finished_at = "failed", utcnow_naive()
                db.add(row)
                await db.commit()


async def view(user: User) -> dict:
    """What the dashboard shows: the setup rows so far and, once done, the ranked filled-in tasks.
    The evidence itself (and where the user came from) never leaves the server."""
    if not await allowed(user.email):
        raise OnboardError("not_enabled")
    async with session_maker() as db:
        row = await _profile(db, user.id)
    if row is None or row.org_id is None:
        return {"status": "none", "library": public_library(), "default_rank": list(DEFAULT_RANK)}
    return _shape(row.status, _unpack(row.payload))


def _titled(s: str) -> str:
    return re.sub(r"[-_]+", " ", s).strip().title() if s and s == s.lower() else s


def _homepage_line(text: str, label: str) -> str:
    m = re.search(rf"^{label}: (.+)$", text or "", re.M)
    return m.group(1).strip() if m else ""


def _facts(p: dict) -> list[dict]:
    """What the setup sheet shows as it lands: one card per fact the lookup found about the user,
    each saying where it came from. Only the user's own facts; where they arrived from (the landing
    page, the referrer) is ranking evidence and never shown."""
    ev, fills = p.get("evidence") or {}, p.get("fills") or {}
    gh, co, hp = ev.get("github") or {}, ev.get("company") or {}, ev.get("homepage") or {}
    out: list[dict] = []
    # The welcome greets the person, by the first name they signed in or commit with; the company is
    # only where they work (they may be anyone there), so it is the line above, never the greeting.
    signin = ev.get("signin") or {}
    person = (signin.get("name") or gh.get("name") or "").strip().split(" ")[0][:40]
    company = co.get("name") or (gh.get("company") or "").strip().lstrip("@") or ""
    # a GitHub company that is only the domain run together ("calcom") is a handle; the domain is the name
    domain = co.get("domain") or signin.get("work_email_domain") or ""
    if not co.get("name") and domain and (not company or _squash(company) == _squash(domain)):
        company = domain
    about = co.get("description") or _homepage_line(hp.get("text") or "", "Description") or ""
    if person or company:
        out.append({"key": "intro", "label": company, "value": person, "note": about[:280],
                    "avatar": hp.get("icon") or ("" if company else gh.get("avatar_url") or "")})
    if gh.get("login"):
        repos = gh.get("public_repos")
        out.append({"key": "github", "label": "GitHub", "value": "@" + gh["login"],
                    "note": f"{repos} public repos" if repos else "your public commits",
                    "avatar": gh.get("avatar_url") or ""})
    site = (hp.get("url") or "").removeprefix("https://")
    if co.get("domain") and not co.get("name") and co["domain"] != site:   # the site card already says it
        out.append({"key": "company", "label": "Your company", "value": co["domain"], "note": ""})
    if co.get("employees"):
        out.append({"key": "size", "label": "Company size", "value": str(co["employees"]), "note": ""})
    where = co.get("location") or gh.get("location")
    if where:
        out.append({"key": "location", "label": "Based in", "value": str(where), "note": ""})
    if co.get("industry"):
        out.append({"key": "industry", "label": "Industry", "value": _titled(str(co["industry"])), "note": ""})
    if co.get("founded"):
        out.append({"key": "founded", "label": "Founded", "value": str(co["founded"]), "note": ""})
    if site:
        out.append({"key": "site", "label": "Your site", "value": site, "note": ""})
    if hp.get("tools"):
        out.append({"key": "tools", "label": "Tools you use", "value": " · ".join(hp["tools"][:4]), "note": ""})
    if hp.get("ad_pixels"):
        out.append({"key": "ads", "label": "You track ads on", "value": " · ".join(hp["ad_pixels"][:4]), "note": ""})
    term = fills.get("serp") or {}
    if term.get("source") == "checked":
        out.append({"key": "search", "label": "What buyers search", "value": f'\u201c{term["value"]}\u201d', "note": ""})
    rival = fills.get("scrape") or {}
    if rival.get("source") == "checked":
        host = rival["value"].split("/")[0]
        rank = ranked(rival["pos"], rival["term"]) if rival.get("pos") and rival.get("term") else ""
        out.append({"key": "competitor", "label": "A competitor", "value": host, "note": rank})
    topic = fills.get("videos") or {}
    if topic.get("source") == "checked":
        out.append({"key": "tiktok", "label": "On TikTok", "value": f'\u201c{topic["value"]}\u201d', "note": ""})
    return out


def _shape(status: str, p: dict) -> dict:
    """`ready` = the tasks can be shown while their inputs are still being checked; `done` = final."""
    if status in ("running", "ready") and time.time() - float(p.get("started") or 0) > DEADLINE_S + 15:
        status = "done"        # a restart lost the task; what was saved stands, examples fill the rest
    fills = p.get("fills") or {}
    ranking = p.get("ranking") or []
    here_for = p.get("here_for")
    tasks, pending = [], set()
    if status in ("ready", "done", "failed"):
        order = [r["id"] for r in ranking if r.get("id") in TASKS] or list(TASKS)
        order += [t for t in TASKS if t not in order]
        scores = {r["id"]: r.get("score") for r in ranking}
        pending = set(p.get("pending") or []) if status == "ready" else set()
        # The tasks with an input of the user's own (or one still being checked) lead; a task the
        # judge rates highly recommended leads even on its example. The rest keep their order behind.
        lead = [t for t in order if is_recommended(scores.get(t)) or t in pending
                or (fills.get(t) or {}).get("source", "example") != "example"]
        # what the user said their agent is for leads everything, in its own order
        said = [t for t in USE_CASES[here_for][1] if t in TASKS] if here_for in USE_CASES else []
        lead = said + [t for t in lead if t not in said]
        order = lead + [t for t in order if t not in lead]
        for tid in order:
            t, f = TASKS[tid], fills.get(tid) or {}
            example = f.get("source", "example") == "example" or not f.get("value")
            tasks.append({"id": tid, "value": f.get("value") if not example else t.example[0],
                          "note": f.get("note") if not example else t.example[1], "example": example,
                          "score": scores.get(tid), "recommended": is_recommended(scores.get(tid))})
    return {
        "status": status, "team": p.get("team"), "steps": p.get("steps") or [], "facts": _facts(p),
        "tasks": tasks, "shown": SHOWN,
        "preselect": (tasks[0]["id"] if here_for in USE_CASES and tasks else
                      next((t["id"] for t in tasks if t["recommended"]), None)),
        # nothing public grounded a single task: ask what the agent is for (once the lookup is over,
        # so the answer never races the lookup's own saves)
        "ask": status in ("done", "failed") and bool(tasks) and not any(
            not t["example"] or t["recommended"] for t in tasks),
        "here_for": here_for if here_for in USE_CASES else None,
        "use_cases": [{"key": k, "label": v[0]} for k, v in USE_CASES.items()],
        "pending": sorted(pending),
        "library": public_library(), "default_rank": list(DEFAULT_RANK),
    }


# ------------------------------------------------------------------------------- preview
# The same flow for any email, for the people who tune it: the lookup runs for that address, no
# team is made, nobody is marked onboarded, and the first call goes out as a house call (so a
# registry without provider credit still shows a real answer). Everything it spends is the house
# team's, so it is open only to super-admins and the people `onboarding_v2_emails` lists.
PREVIEW_NS = "onboarding_preview"
PREVIEW_TTL_S = 3600
PREVIEW_CALLS = 10         # first calls one preview may make (a task makes one or two; runs can be retried)
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_previews: dict[str, asyncio.Task] = {}


async def answer(user: User, here_for: str) -> dict:
    """What the user says their agent is for: kept on their profile, and it leads their tasks."""
    if not await allowed(user.email):
        raise OnboardError("not_enabled")
    if here_for not in USE_CASES:
        raise OnboardError("unknown_use_case")
    async with session_maker() as db:
        row = await _profile(db, user.id)
        if row is None or row.org_id is None:
            raise OnboardError("not_started")
        payload = _unpack(row.payload)
        payload["here_for"] = here_for
        row.payload = _pack(payload)
        db.add(row)
        await db.commit()
        status = row.status
    return _shape(status, payload)


def may_preview(email: str, is_superadmin: bool) -> bool:
    return bool(is_superadmin) or _listed(email)


def _listed(email: str) -> bool:
    email = (email or "").strip().lower()
    return any(entry and (email == entry or (entry.startswith("@") and email.endswith(entry)))
               for entry in (e.strip().lower() for e in get_settings().onboarding_v2_emails.split(",")))


async def _preview_load(pid: str) -> dict | None:
    async with session_maker() as db:
        row = await ratestore.kv_get(db, PREVIEW_NS, pid)
    return row


async def _preview_put(pid: str, row: dict) -> None:
    async with session_maker() as db:
        await ratestore.kv_put(db, PREVIEW_NS, pid, row, PREVIEW_TTL_S)
        await db.commit()


async def preview_start(user: User, email: str, http: httpx.AsyncClient) -> dict:
    if not may_preview(user.email, user.is_superadmin):
        raise OnboardError("not_enabled")
    email = (email or "").strip().lower()
    if not _EMAIL.match(email):
        raise OnboardError("bad_email")
    pid = crypto.new_token()[:24]
    row = {"owner": user.id, "status": "running", "calls": 0,
           "payload": _pack({"email": email, "started": time.time()})}
    await _preview_put(pid, row)

    async def save(state: State, house_micro: int) -> None:
        cur = await _preview_load(pid) or row
        p = _unpack(cur["payload"])
        p.update(state.view())
        cur.update(payload=_pack(p), house_micro=house_micro)
        if state.ready and cur.get("status") == "running":
            cur["status"] = "ready"
        await _preview_put(pid, cur)

    async def run() -> None:
        lookup = Lookup(email, Hints(door="preview"), http, save)
        try:
            state = await lookup.run()
            await save(state, lookup.house.cost_micro if lookup.house else 0)
        finally:
            cur = await _preview_load(pid) or row
            cur["status"] = "done"
            await _preview_put(pid, cur)

    task = asyncio.create_task(run())
    _previews[pid] = task
    task.add_done_callback(lambda t: _previews.pop(pid, None))
    return await preview_view(user, pid)


async def _owned_preview(user: User, pid: str) -> dict:
    if not may_preview(user.email, user.is_superadmin):
        raise OnboardError("not_enabled")
    row = await _preview_load(pid)
    if not row or row.get("owner") != user.id:
        raise OnboardError("preview_not_found")
    return row


async def preview_view(user: User, pid: str) -> dict:
    row = await _owned_preview(user, pid)
    p = _unpack(row["payload"])
    return {**_shape(row["status"], p), "id": pid, "email": p.get("email"), "team": None,
            "house_micro": int(row.get("house_micro") or 0)}


async def preview_answer(user: User, pid: str, here_for: str) -> dict:
    row = await _owned_preview(user, pid)
    if here_for not in USE_CASES:
        raise OnboardError("unknown_use_case")
    p = _unpack(row["payload"])
    p["here_for"] = here_for
    row["payload"] = _pack(p)
    await _preview_put(pid, row)
    return await preview_view(user, pid)


async def preview_call(user: User, pid: str, call: dict, http: httpx.AsyncClient) -> dict:
    """One of the preview's first calls, as a house call. Only a call the task library makes."""
    await _owned_preview(user, pid)
    endpoint, method = str(call.get("endpoint") or ""), str(call.get("method") or "POST").upper()
    if not any(c.endpoint == endpoint and c.method == method for t in TASKS.values() for c in t.calls):
        raise OnboardError("bad_call")
    s = get_settings()
    if not s.onboarding_treg_token:
        raise OnboardError("no_house_token")
    async with session_maker() as db:
        allowed_call = await ratestore.rate_check(db, PREVIEW_NS + ":calls", [(pid, PREVIEW_CALLS)], PREVIEW_TTL_S)
        await db.commit()
    if not allowed_call:
        raise OnboardError("preview_spent")
    house = HouseCalls(http, s.onboarding_treg_token, "onboarding-preview", s.onboarding_treg_url)
    body = call.get("body") if isinstance(call.get("body"), dict) else {}
    query = call.get("query") if isinstance(call.get("query"), dict) else {}
    a = await house.request(method, endpoint, "first_call", json=body if method != "GET" else None,
                            params=query or None, headers={"X-Treg-Route-Max-Cost": "0.05"}, timeout=60)
    return {"status": a.status, "body": a.body, "cost_micro": a.cost_micro, "served_by": a.served_by}


async def shutdown() -> None:
    tasks = list(_owners.values()) + list(_previews.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
