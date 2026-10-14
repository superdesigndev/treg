"""The first-task library: nine things a new team's agent can do on its first call.

Chosen from what new teams call first and keep using: finding emails and people, company data,
keywords and Google results, Maps, X and Reddit, TikTok, reading a page. Each task is a
sentence with one blank (`tpl`, `{}`), filled in for the user by the lookup or with its labelled
example, and editable.

`calls` turns the filled-in value into the catalog calls the first run makes. It is data the
dashboard applies too (`public_library`): `parse` is a regular expression with named groups over the
value, every string in `body`/`query` may name a group (`{value}` is the whole input), and a key
whose groups are all empty is dropped. Python and JavaScript read the same pattern; `(?<name>...)`
is rewritten to Python's `(?P<name>...)` here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ...domain.catalog import store as catalog_store


@dataclass(frozen=True)
class Call:
    endpoint: str
    method: str = "POST"
    body: dict = field(default_factory=dict)
    query: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Task:
    id: str
    tpl: str                   # the card: "Find the work email of {}"
    say: str                   # what the agent is asked: "find the work email of {} and ..."
    slot: str                  # what the blank is, for the input's label
    view: str                  # how the dashboard draws the result
    example: tuple[str, str]   # (value, note) when nothing about the user fills it
    calls: tuple[Call, ...]
    parse: str = ""            # named groups over the value; empty = the value is used whole
    question: str = ""         # what the judge is asked about the user, to rank this task
    unit: str = ""             # what one more run is, for the credit line: "work email lookups"


_URL = r"^(?:https?://)?(?<rest>.+)$"

TASKS: dict[str, Task] = {t.id: t for t in (
    Task("email", "Find the work email of {}", "find the work email of {} and tell me if it is deliverable.",
         "Person", "person", ("Karri Saarinen at linear.app", "an example person"),
         (Call("treg.people.email.find", body={"full_name": "{full_name}", "domain": "{domain}"}),),
         parse=r"^(?<full_name>.+?)\s+at\s+(?<domain>\S+)$",
         question="find the work email address of a specific person at a company (sales, recruiting or partnership outreach)",
         unit="work email lookups"),
    Task("people", "Find the decision makers at {}", "find the product and engineering decision makers at {}.",
         "Company", "people", ("linear.app", "an example company"),
         (Call("treg.people.search", body={"company_domain": "{value}"}),),
         question="find the people who work at a target company, with their titles (prospecting or recruiting)",
         unit="people searches"),
    Task("company", "See what data providers know about {}", "look up {} across company data providers and tell me what is missing or wrong.",
         "Company", "company", ("linear.app", "an example company"),
         (Call("treg.companies.enrich", body={"domain": "{domain}", "name": "{name}"}),),
         parse=r"^(?:(?<domain>[\w-]+(?:\.[\w-]+)+)|(?<name>.+))$",
         question="look up firmographic data about a company: industry, size, founding year, location",
         unit="company lookups"),
    Task("keywords", 'See how many people search for \u201c{}\u201d', 'show me the monthly Google searches for "{}" and the related keywords worth targeting.',
         "Keyword", "keywords", ("issue tracking software", "an example keyword"),
         (Call("treg.google.keywords.ideas", body={"keyword": "{value}"}),),
         question="research Google search volume and keyword ideas for SEO or ads",
         unit="keyword lookups"),
    Task("serp", 'See who ranks on Google for \u201c{}\u201d', 'show me who ranks on Google for "{}".',
         "Search", "serp", ("best issue tracking tool for startups", "an example search"),
         (Call("treg.google.serp.organic", body={"q": "{value}"}),),
         question="see which sites rank on Google for a search, to track SEO or competitors",
         unit="Google searches"),
    Task("maps", "List {} from Google Maps", "list {} from Google Maps with ratings, reviews and websites.",
         "Places", "maps", ("coffee shops in Austin, TX", "an example search"),
         (Call("treg.google.serp.maps", body={"q": "{value}"}),),
         question="build a list of local businesses from Google Maps (local lead generation)",
         unit="Maps searches"),
    Task("social", "See what people say about {} on X and Reddit", "find what people say about {} on X and Reddit this month.",
         "Brand", "social", ("Linear", "an example brand"),
         (Call("treg.x.search.posts", body={"q": "{value}"}),
          Call("scrapecreators.reddit.search.posts", method="GET", query={"query": "{value}", "sort": "relevance"})),
         question="monitor what people say about a brand or product on X and Reddit",
         unit="brand checks"),
    Task("videos", 'Find the top TikTok videos about \u201c{}\u201d', 'find the most-viewed TikTok videos about "{}" and what their hooks have in common.',
         "Topic", "videos", ("productivity app", "an example topic"),
         (Call("treg.tiktok.search.videos", body={"q": "{value}"}),),
         question="study top TikTok videos on a topic for content or UGC marketing",
         unit="TikTok searches"),
    Task("scrape", "Turn {} into a table", "read {} and turn it into a table.",
         "Page", "scrape", ("linear.app/pricing", "an example page"),
         (Call("treg.web.extract", body={"url": "https://{rest}"}),),
         parse=_URL,
         question="read a web page (a competitor's pricing page, a directory) and turn it into structured data",
         unit="pages read"),
)}

# With no evidence to rank by: what new teams call first most often and keep using.
DEFAULT_RANK = ("email", "videos", "people", "keywords", "company", "serp", "social", "scrape", "maps")
SHOWN = 5
RECOMMENDED = 0.6   # the judge's yes probability at which a task is tagged "Highly recommended"

# What a new user can say their agent is for, asked only when nothing public grounded a task. Each
# names the tasks it brings to the front, best first.
USE_CASES: dict[str, tuple[str, tuple[str, ...]]] = {
    "leads": ("Find leads and contacts", ("people", "email", "company")),
    "seo": ("SEO and search", ("keywords", "serp")),
    "social": ("Social listening", ("social", "videos")),
    "competitors": ("Research competitors", ("scrape", "serp", "company")),
    "local": ("Local businesses", ("maps", "people")),
}

_GROUP = re.compile(r"\{(\w+)\}")


def _groups(task: Task, value: str) -> dict[str, str] | None:
    value = value.strip()
    if not value:
        return None
    if not task.parse:
        return {"value": value}
    m = re.match(task.parse.replace("(?<", "(?P<"), value)
    if not m:
        return None
    return {"value": value, **{k: v or "" for k, v in m.groupdict().items()}}


def _fill(template, groups: dict[str, str]):
    if not isinstance(template, str):
        return template
    names = _GROUP.findall(template)
    if names and not any(groups.get(n) for n in names):
        return None
    return _GROUP.sub(lambda m: groups.get(m.group(1), ""), template)


def build_calls(task_id: str, value: str) -> list[dict] | None:
    """The calls a first run of `task_id` on `value` makes, or None when the value does not parse."""
    task = TASKS[task_id]
    groups = _groups(task, value)
    if groups is None:
        return None
    out = []
    for c in task.calls:
        body = {k: v for k, v in ((k, _fill(t, groups)) for k, t in c.body.items()) if v is not None}
        query = {k: v for k, v in ((k, _fill(t, groups)) for k, t in c.query.items()) if v is not None}
        out.append({"endpoint": c.endpoint, "method": c.method, "body": body, "query": query})
    return out


def unit_usd(task: Task) -> float | None:
    """What one run lists at in the catalog (the cheapest table row for a table price), summed over
    its calls; None when it lists as free, which the credit line cannot divide by."""
    cat = catalog_store.load()
    total = 0.0
    for c in task.calls:
        ep = cat.by_id.get(c.endpoint)
        view = cat.cost_view(ep.get("cost"), ep.get("provider")) if ep else None
        if view:
            total += float(view.get("usd_min") or view.get("usd") or 0.0)
    return round(total, 6) or None


def public_library() -> list[dict]:
    """The library as the dashboard reads it."""
    return [{"id": t.id, "tpl": t.tpl, "say": t.say, "slot": t.slot, "view": t.view, "parse": t.parse,
             "example": {"value": t.example[0], "note": t.example[1]},
             "unit": t.unit, "unit_usd": unit_usd(t),
             "calls": [{"endpoint": c.endpoint, "method": c.method, "body": c.body, "query": c.query}
                       for c in t.calls]}
            for t in TASKS.values()]
