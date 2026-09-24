"""The endpoint catalog served over HTTP (`/catalog/*`) + stamped onto provisioned tools.

The data lives in src/treg/catalog/*.yaml (see docs/context/architecture/catalog.md). These tests
assert the API CONTRACT the dashboard and `treg catalog` are written against, plus the two things
the loader has to get right on its own: merging a provider's `<service>.extended.yaml` into the same
provider, and never letting a caller-supplied id reach the filesystem.
"""

from __future__ import annotations

import dataclasses
import json
import re
import shlex

from httpx import AsyncClient

from treg.domain.catalog import store as cs
from treg import oauth_providers as P


def test_tavily_surface_keeps_only_safe_synchronous_data_tools():
    cat = cs.load()
    rows = {ep["id"]: ep for ep in cat.for_provider("tavily")}
    assert set(rows) == {
        "tavily.web.search", "tavily.web.extract", "tavily.web.map", "tavily.web.crawl",
    }
    assert all(ep["platform"] == "web" and ep["scope"] == "any_account" for ep in rows.values())
    assert all(cat.platform_eligible(ep) for ep in rows.values())
    assert rows["tavily.web.extract"]["input"]["body"]["urls"]["maxItems"] == 20
    assert rows["tavily.web.search"]["input"]["body"]["include_usage"] == {
        "type": "boolean", "required": False, "enum": [True], "example": True,
        "note": "Optional with your own key; platform Search requires true so treg can settle "
                "from this request's reported usage.",
    }
    assert all("include_usage" not in rows[eid]["input"]["body"] for eid in (
        "tavily.web.extract", "tavily.web.map", "tavily.web.crawl",
    ))
    assert all(ep["verified"] == "2026-09-21" and ep["example_file"] for ep in rows.values())
    assert cat.credit_rates["tavily"] == 0.008
    shown = {eid: cat.cost_view(ep["cost"], "tavily") for eid, ep in rows.items()}
    assert {eid: (cost["usd"], cost["unit"]) for eid, cost in shown.items()} == {
        "tavily.web.search": (0.016, "call"),
        "tavily.web.extract": (0.0032, "result"),
        "tavily.web.map": (0.0016, "page"),
        "tavily.web.crawl": (0.0048, "result"),
    }
    assert shown["tavily.web.search"]["usd_min"] == 0.008
    assert shown["tavily.web.extract"]["usd_min"] == 0.0016
    assert shown["tavily.web.map"]["usd_min"] == 0.0008
    assert shown["tavily.web.crawl"]["usd_min"] == 0.0024
    assert shown["tavily.web.extract"]["tavily_rates"] == {"basic": 0.2, "advanced": 0.4}
    assert shown["tavily.web.map"]["tavily_rates"] == {"regular": 0.1, "instructions": 0.2}
    assert shown["tavily.web.crawl"]["tavily_rates"]["advanced_instructions"] == 0.6
    assert all("reported_charge" not in rows[eid]["cost"] for eid in rows)
    serialized = json.dumps(rows).lower()
    assert not any(term in serialized for term in (
        "research task", "account usage", "key management", "feedback endpoint", "export endpoint",
    ))


def test_olostep_surface_is_bounded_byok_and_platform_safe():
    cat = cs.load(refresh=True)
    rows = {ep["id"]: ep for ep in cat.for_provider("olostep")}
    assert set(rows) == {
        "olostep.web.scrape",
        "olostep.web.search",
        "olostep.web.answer",
        "olostep.web.map.search",
        "olostep.web.crawl",
        "olostep.web.crawl.status",
        "olostep.web.crawl.results",
    }
    assert all(ep["scope"] == "any_account" for ep in rows.values())
    assert all(cat.platform_eligible(ep) for ep in rows.values())
    assert not any("batch" in eid for eid in rows)
    assert cat.credit_rates["olostep"] == 0.002
    assert cat.cost_view(rows["olostep.web.scrape"]["cost"], "olostep")["usd"] == 0.002
    assert cat.cost_view(rows["olostep.web.search"]["cost"], "olostep")["usd"] == 0.01
    assert cat.cost_view(rows["olostep.web.answer"]["cost"], "olostep")["usd"] == 0.04
    assert rows["olostep.web.map.search"]["input"]["body"]["top_n"]["max"] == 1000
    crawl = rows["olostep.web.crawl"]
    assert crawl["input"]["body"]["max_pages"]["max"] == 100
    assert crawl["cost"]["settle"] == "usage"
    assert crawl["cost"]["usage"] == {"path": "credits_consumed", "unit": "credit"}
    status_owner = rows["olostep.web.crawl.status"]["resource_ownership"]["requires"]
    results_owner = rows["olostep.web.crawl.results"]["resource_ownership"]["requires"]
    assert status_owner["kind"] == "poll:olostep.web.crawl.status"
    assert results_owner["kind"] == "fetch:olostep.web.crawl.results"


def test_trestleiq_surface_is_three_direct_single_record_tools():
    cat = cs.load()
    rows = {ep["id"]: ep for ep in cat.for_provider("trestleiq")}
    assert set(rows) == {
        "trestleiq.people.phone.verify",
        "trestleiq.people.contact.verify",
        "trestleiq.people.address.verify",
    }
    assert all(ep["platform"] == "people" for ep in rows.values())
    assert all(ep["method"] == "GET" and ep["strict_query"] for ep in rows.values())
    assert all(cat.platform_eligible(ep) for ep in rows.values())
    assert cat.cost_view(rows["trestleiq.people.phone.verify"]["cost"], "trestleiq")["usd"] == 0.015
    assert cat.cost_view(rows["trestleiq.people.contact.verify"]["cost"], "trestleiq")["usd"] == 0.03
    assert cat.cost_view(rows["trestleiq.people.address.verify"]["cost"], "trestleiq")["usd"] == 0.01
    assert all(ep["cost"]["source"] == "observed" for ep in rows.values())
    assert all(ep["cost"]["confidence"] == "verified" for ep in rows.values())
    serialized = json.dumps(rows)
    assert "add_ons" not in serialized
    assert not any("bulk" in eid or "reverse" in eid for eid in rows)
    assert not any(eid.startswith("trestleiq.") for eid in cat.adapters)
    assert "treg.people.phone.verify" not in cat.by_id


def test_openmart_surface_separates_platform_reads_from_byok_lifecycles():
    cat = cs.load()
    rows = cat.for_provider("openmart")
    assert len(rows) == 9
    assert len({(e["method"], e["path"]) for e in rows}) == 9
    assert {e["id"] for e in rows if cat.platform_eligible(e)} == {
        "openmart.businesses.search",
        "openmart.businesses.lookup.openmart",
        "openmart.businesses.lookup.google-place",
        "openmart.companies.enrich",
        "openmart.companies.search",
    }
    assert all(e.get("verified") and e.get("example_file") for e in rows)
    assert cat.credit_rates["openmart"] == .0298
    assert "openmart.account.balance" not in cat.by_id


def test_openmart_pricing_and_account_boundaries_stay_visible():
    cat = cs.load()
    assert not any(
        eid.startswith("openmart.tasks.") or eid.endswith(".batch")
        for eid in cat.by_id if eid.startswith("openmart.")
    )
    fast = cat.by_id["openmart.businesses.search.ids"]
    assert fast["cost"]["value"] is None and fast["cost"]["confidence"] == "unknown"
    assert all(cat.by_id[key]["scope"] == "own_account" for key in (
        "openmart.deny-rules.create", "openmart.deny-rules.check",
        "openmart.deny-rules.delete",
    ))
    assert cat.by_id["openmart.deny-rules.delete"]["cache"] == "forbidden"


# ---- platform listing --------------------------------------------------------------------
async def test_platforms_lists_the_curated_shelves_busiest_first(clients: AsyncClient):
    r = await clients.get("/catalog/platforms")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["generated_from"] == "catalog"
    rows = {p["slug"]: p for p in body["platforms"]}
    assert {"tiktok", "web"} <= set(rows), rows.keys()

    tiktok = rows["tiktok"]
    assert tiktok["label"] == "TikTok"
    assert tiktok["endpoints"] >= 5
    assert 0 < tiktok["capabilities"] <= tiktok["endpoints"]
    assert 0 < tiktok["verified"] <= tiktok["endpoints"]
    assert {"tikhub", "justoneapi"} <= set(tiktok["providers"]), "the social overlap pair is the point"
    assert {"dataforseo", "moz"} <= set(rows["web"]["providers"]), "the SEO overlap pair is the point"

    counts = [p["endpoints"] for p in body["platforms"]]
    assert counts == sorted(counts, reverse=True)


async def test_platform_listing_hides_taxonomy_entries_nobody_implements(clients: AsyncClient):
    rows = (await clients.get("/catalog/platforms")).json()["platforms"]
    assert all(p["endpoints"] > 0 for p in rows)


# ---- platform detail ---------------------------------------------------------------------
async def test_platform_detail_groups_the_same_job_across_providers(clients: AsyncClient):
    """The capability is the join key: one row, both providers — that's what makes the catalog
    comparable rather than four separate provider docs."""
    r = await clients.get("/catalog/platforms/tiktok")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["platform"] == {"slug": "tiktok", "label": "TikTok", "category": "Social"}

    caps = {c["id"]: c for c in body["capabilities"]}
    assert [c["id"] for c in body["capabilities"]] == sorted(caps), "capabilities are sorted by id"
    profile = caps["tiktok.user.profile"]
    assert profile["description"]
    assert {e["provider"] for e in profile["endpoints"]} >= {"tikhub", "justoneapi"}

    ep = next(e for e in profile["endpoints"] if e["provider"] == "tikhub")
    assert set(ep) == {"id", "provider", "provider_display", "name", "summary", "method", "path",
                       "scope", "tier", "kind", "domain", "call_template", "cost", "verified", "docs_url",
                       "has_example", "input", "platform_eligible", "platform_blocked",
                       "test_request", "miss", "status", "status_note", "superseded_by", "async"}
    assert ep["kind"] == "data", "an endpoint with no explicit kind is data (the browse surface)"
    assert ep["provider_display"] == P.get("tikhub").display_name
    assert ep["method"] == "GET" and ep["path"].startswith("/")
    assert ep["tier"] == "core", "an endpoint with no explicit tier is core, not hidden"
    assert ep["verified"] and ep["has_example"] is True
    assert ep["cost"]["type"] == "per_success"


async def test_every_listed_endpoint_carries_its_platform(clients: AsyncClient):
    for slug in ("web", "google", "tiktok"):
        body = (await clients.get(f"/catalog/platforms/{slug}")).json()
        ids = {e["id"] for c in body["capabilities"] for e in c["endpoints"]}
        ids |= {e["id"] for e in body["extended"]}
        # the default view is the BROWSE surface — account/utility plumbing is served only under
        # ?include_hidden=1, so the listing equals the platform's data + action endpoints
        assert ids == {e["id"] for e in cs.load().for_platform(slug)
                       if e["kind"] not in cs.HIDDEN_KINDS}


# ---- platform detail: the domain ledger the dashboard renders -----------------------------
async def test_the_ledger_files_every_row_under_a_domain_with_other_last(clients: AsyncClient):
    """A platform page is ONE table sectioned by subject. `other` is the junk drawer, so it is the
    one section whose position carries meaning — it can never outrank a real subject, however big
    it gets. The rest run busiest-first, because the biggest section is what a visitor came for."""
    body = (await clients.get("/catalog/platforms/tiktok")).json()
    doms = body["domains"]
    assert [d["domain"] for d in doms].count("other") == 1
    assert doms[-1]["domain"] == "other", "the junk drawer sorts last no matter its size"
    sizes = [len(d["rows"]) for d in doms[:-1]]
    assert sizes == sorted(sizes, reverse=True)

    # every browse-surface endpoint appears exactly once, in exactly one section (the default
    # ledger drops account/utility plumbing — see the hidden-count test below)
    ids = [e["id"] for d in doms for r in d["rows"] for e in r["endpoints"]]
    assert sorted(ids) == sorted(e["id"] for e in cs.load().for_platform("tiktok")
                                 if e["kind"] not in cs.HIDDEN_KINDS)
    assert len(ids) == len(set(ids))


async def test_a_section_leads_with_the_jobs_several_providers_do(clients: AsyncClient):
    """Merged rows first: a comparable job is worth more than a lone route, and burying the
    comparison under fifty single endpoints is how the old page hid it."""
    body = (await clients.get("/catalog/platforms/tiktok")).json()
    for section in body["domains"]:
        kinds = [r["kind"] for r in section["rows"]]
        assert kinds == sorted(kinds, key=lambda k: k != "merged"), section["domain"]

    user = next(d for d in body["domains"] if d["domain"] == "user")
    merged = next(r for r in user["rows"] if r["capability"] == "tiktok.user.profile")
    assert merged["kind"] == "merged"
    assert merged["description"] == "Get a user's public profile", "the capability's plain-English job"
    assert len({e["provider"] for e in merged["endpoints"]}) > 1, "one provider is not a comparison"


async def test_a_single_row_is_led_by_the_endpoints_own_name(clients: AsyncClient):
    """A row only one provider serves is led by the endpoint's curated `name` ("Get Showcase Product
    List" says more than `tiktok.shop.showcase` ever could), falling back to its `summary` until a
    name is written. `summary` is documentation prose — some of it runs to a paragraph — so it is a
    fallback for the row and the description in the expansion, never the preferred title."""
    body = (await clients.get("/catalog/platforms/tiktok")).json()
    rows = [r for d in body["domains"] for r in d["rows"] if r["kind"] == "single"]
    assert rows
    for row in rows:
        # exactly one endpoint: folding a provider's two takes on one job into a single row would
        # show one route next to the OTHER one's price
        assert len(row["endpoints"]) == 1, row["capability"]
        ep = row["endpoints"][0]
        assert row["description"] == (ep["name"] or ep["summary"] or row["capability"])


def test_a_curated_name_beats_the_summary_on_a_row(tmp_path):
    """The founder's fix for DataForSEO rows rendering doc paragraphs as titles: where a `name:` is
    written, it wins, and the prose stays in the expansion."""
    (tmp_path / "capabilities.yaml").write_text("platforms: {web: Web}\ncapabilities: {}\n")
    (tmp_path / "dataforseo.extended.yaml").write_text(
        "provider: dataforseo\nendpoints:\n"
        "  - id: dataforseo.x.named\n    platform: web\n    tier: extended\n    method: POST\n"
        "    path: /v3/backlinks/anchors/live\n    name: Anchor text overview\n"
        "    summary: This endpoint will provide you with a detailed overview of anchors used when "
        "linking to the specified website with relevant backlink data for each of them.\n"
        "  - id: dataforseo.x.unnamed\n    platform: web\n    tier: extended\n    method: POST\n"
        "    path: /v3/backlinks/domain_pages/live\n    summary: Domain pages with backlink data\n")
    cat = cs.load(directory=tmp_path)
    pairs = [(e, cs.endpoint_view(e, e["provider"], cat)) for e in cat.endpoints]
    rows = {r["endpoints"][0]["id"]: r for s in cs.domain_rows(pairs, cat.capabilities) for r in s["rows"]}
    assert rows["dataforseo.x.named"]["description"] == "Anchor text overview"
    assert rows["dataforseo.x.unnamed"]["description"] == "Domain pages with backlink data"


async def test_every_endpoint_carries_a_domain_and_a_call_line(clients: AsyncClient):
    """The domain decides which section a row files under, and the `treg call` line is the reason
    the row exists — both ride on the row rather than costing a second request."""
    for slug in ("tiktok", "web"):
        body = (await clients.get(f"/catalog/platforms/{slug}")).json()
        eps = [e for c in body["capabilities"] for e in c["endpoints"]] + body["extended"]
        assert eps
        for e in eps:
            assert e["domain"] and e["domain"] == e["domain"].lower().strip()
            # The endpoint-ID form: callable with no registered tool (marketplace credential ladder).
            assert e["call_template"].startswith(f"treg call {e['id']}")


async def test_the_ledger_ships_the_provider_wide_facts_once(clients: AsyncClient):
    """Limits and the rate card live once at the top of a provider's yaml. An expanded row needs
    them, and copying them onto 2,000 endpoint rows to deliver them would be absurd."""
    body = (await clients.get("/catalog/platforms/tiktok")).json()
    assert set(body["providers"]) == {e["provider"] for e in cs.load().for_platform("tiktok")}
    tikhub = body["providers"]["tikhub"]
    assert tikhub["display_name"] == P.get("tikhub").display_name
    assert tikhub["docs"]
    assert body["providers"]["justoneapi"]["pricing_url"]


def test_the_domain_is_curation_first_then_the_taxonomy_then_a_guess(tmp_path):
    """Three sources, best first. An explicit `domain:` always wins; otherwise the capability id's
    middle segment, which the taxonomy already encodes; and only then a keyword read off the path,
    which is all an unmapped extended endpoint has to offer."""
    (tmp_path / "capabilities.yaml").write_text(
        "platforms: {tiktok: TikTok}\ncapabilities: {tiktok.video.comments: Comments}\n")
    (tmp_path / "tikhub.yaml").write_text(
        "provider: tikhub\nendpoints:\n"
        "  - id: tikhub.tiktok.video.comments\n    capability: tiktok.video.comments\n"
        "    platform: tiktok\n    method: GET\n    path: /a\n"
        "  - id: tikhub.tiktok.override\n    platform: tiktok\n    domain: shop\n"
        "    method: GET\n    path: /api/v1/tiktok/web/fetch_user_profile\n"
        "  - id: tikhub.tiktok.guessed\n    platform: tiktok\n    method: GET\n"
        "    path: /api/v1/tiktok/shop/web/fetch_product_detail\n    summary: Product detail\n"
        "  - id: tikhub.tiktok.nothing\n    platform: tiktok\n    method: GET\n    path: /x\n")
    by_id = cs.load(directory=tmp_path).by_id
    assert by_id["tikhub.tiktok.video.comments"]["domain"] == "video"
    assert by_id["tikhub.tiktok.override"]["domain"] == "shop", "curation beats the keyword guess"
    assert by_id["tikhub.tiktok.guessed"]["domain"] == "shop"
    assert by_id["tikhub.tiktok.nothing"]["domain"] == "other"


def test_a_delivery_mode_in_the_path_is_not_a_subject(tmp_path):
    """DataForSEO ends every synchronous route in `/live`, which read as a subject and filed 33
    unrelated SEO endpoints under a "live" heading. A mode, a version and a format segment all say
    how a route is CALLED — the grouping segment before them is what it is about."""
    (tmp_path / "capabilities.yaml").write_text("platforms: {web: Web}\ncapabilities: {}\n")
    (tmp_path / "dataforseo.extended.yaml").write_text(
        "provider: dataforseo\nendpoints:\n"
        "  - id: dataforseo.x.anchors\n    platform: web\n    tier: extended\n    method: POST\n"
        "    path: /v3/backlinks/anchors/live\n    summary: Live anchors overview\n"
        "  - id: dataforseo.x.lonely\n    platform: web\n    tier: extended\n    method: GET\n"
        "    path: /v3/status\n    summary: Live API status\n")
    by_id = cs.load(directory=tmp_path).by_id
    assert by_id["dataforseo.x.anchors"]["domain"] == "backlinks"
    # ...and a path with no grouping segment left is honest about it: the last segment is the
    # OPERATION, and a section per operation is not a section at all.
    assert by_id["dataforseo.x.lonely"]["domain"] == "other"


async def test_unknown_platform_is_404(clients: AsyncClient):
    r = await clients.get("/catalog/platforms/myspace")
    assert r.status_code == 404
    assert "myspace" in r.text


async def test_management_endpoints_are_hidden_from_counts_and_the_default_view(
        clients: AsyncClient, monkeypatch, tmp_path):
    """`kind: account`/`utility` are real inventory but PLUMBING (webhooks, saved lists, token
    helpers). They must never inflate the census counts, and the default platform view leaves them
    out — the browse surface is data + action. `?include_hidden=1` returns the whole surface, each
    endpoint carrying its `kind` so a client can file the plumbing behind its own expander."""
    (tmp_path / "capabilities.yaml").write_text(
        "platforms: {web: Web}\ncapabilities: {web.backlinks.summary: Backlinks}\n")
    (tmp_path / "moz.yaml").write_text(
        "provider: moz\nsource: {docs: https://moz.com}\nendpoints:\n"
        "  - id: moz.web.backlinks.summary\n    capability: web.backlinks.summary\n"
        "    platform: web\n    method: POST\n    path: /d\n    kind: data\n"
        "  - id: moz.web.post\n    platform: web\n    method: POST\n    path: /a\n    kind: action\n"
        "  - id: moz.web.list.create\n    platform: web\n    method: POST\n    path: /acc\n    kind: account\n"
        "  - id: moz.web.token\n    platform: web\n    method: GET\n    path: /util\n    kind: utility\n")
    cat = cs.load(directory=tmp_path)
    monkeypatch.setattr(cs, "load", lambda *a, **k: cat)

    # census: only data + action count — never the account/utility plumbing
    census = {p["slug"]: p for p in (await clients.get("/catalog/platforms")).json()["platforms"]}
    assert census["web"]["endpoints"] == 2, "management endpoints do not inflate the tile count"

    # default platform detail: management endpoints are absent from every shape it returns
    body = (await clients.get("/catalog/platforms/web")).json()
    ids = ({e["id"] for c in body["capabilities"] for e in c["endpoints"]}
           | {e["id"] for e in body["extended"]}
           | {e["id"] for d in body["domains"] for r in d["rows"] for e in r["endpoints"]})
    assert ids == {"moz.web.backlinks.summary", "moz.web.post"}
    assert body["hidden_count"] == 2, "but the page is told how many were set aside"

    # ?include_hidden=1: the whole surface comes back, each endpoint carrying its kind
    full = (await clients.get("/catalog/platforms/web?include_hidden=1")).json()
    shown = {e["id"]: e for d in full["domains"] for r in d["rows"] for e in r["endpoints"]}
    assert set(shown) == {"moz.web.backlinks.summary", "moz.web.post",
                          "moz.web.list.create", "moz.web.token"}
    assert shown["moz.web.list.create"]["kind"] == "account"
    assert shown["moz.web.token"]["kind"] == "utility"
    assert full["hidden_count"] == 2


# ---- search ------------------------------------------------------------------------------
async def test_search_finds_the_job_across_providers_best_first(clients: AsyncClient):
    """The discover half of the loop: an agent knows the JOB ("tiktok comments"), not the shelf it
    sits on. Both providers implementing it must come back, core+verified ahead of the long tail."""
    r = await clients.get("/catalog/search", params={"q": "tiktok comments"})
    assert r.status_code == 200, r.text
    body = r.json()
    rows = body["results"]
    assert body["count"] == len(rows) <= body["total"]

    top = [e["id"] for e in rows[:5]]
    assert top[0] == "treg.tiktok.video.comments", "the routed endpoint for the job comes first"
    assert set(top[1:]) == {
        "anyapi.tiktok.video.comments",
        "justoneapi.tiktok.video.comments",
        "tikhub.tiktok.video.comments",
        "scrapecreators.tiktok.video.comments",
    }
    # anyapi is the fourth seller of this job and ranks on price like the rest; it carries no
    # `verified` stamp yet because a vendor never stamps its own rows — the maintainers' own
    # verification run adds it, and this line goes back to a plain `all(...)` when it does.
    assert all(e["tier"] == "core" for e in rows[1:5])
    assert all(e["verified"] for e in rows[1:5] if not e["id"].startswith("anyapi."))
    # rank is total and stable: score desc, then core before extended WITHIN a score tie — tier
    # never outranks relevance, so a strong extended match may sit above a weak core one
    # …except that a capability with a routed row is shown as a GROUP (parent first, then its
    # children), so the tie-break is asserted over rows outside routed groups.
    routed_caps = {e["capability"] for e in rows if e.get("kind") == "routed"}
    loose = [e for e in rows if e["capability"] not in routed_caps]
    scores = [e["score"] for e in loose]
    assert scores == sorted(scores, reverse=True)
    for score in set(scores):
        group = [e["tier"] for e in loose if e["score"] == score]
        assert group == sorted(group, key=lambda t: t != "core")

    first = rows[1]  # the first CHILD; rows[0] is the generated treg.* row, whose provider is treg itself
    assert first["capability"] == "tiktok.video.comments" and first["capability_description"]
    assert first["platform"] == "tiktok" and first["platform_label"] == "TikTok"
    assert first["provider_display"] == P.get(first["provider"]).display_name
    assert first["cost"]["usd"] is not None, "a search row prices in one currency or comparison is fiction"
    assert any(h.startswith(f"treg catalog get {rows[0]['id']}") or h.startswith(f"treg catalog get {first['id']}") for h in body["hints"])


async def test_search_requires_every_token_to_match(clients: AsyncClient):
    """AND, not OR — a second word is a refinement, so it must be able to shrink the result set."""
    broad = (await clients.get("/catalog/search", params={"q": "tiktok", "limit": 100})).json()
    narrow = (await clients.get("/catalog/search", params={"q": "tiktok comments", "limit": 100})).json()
    assert 0 < narrow["total"] < broad["total"]
    for e in narrow["results"]:  # the second token really is a filter, not a scoring nudge
        if e.get("kind") == "routed":
            continue  # a routed parent rides in on a matched CHILD (see search); its own text need not match
        haystack = " ".join((e["id"], e["summary"], e["capability"], e["capability_description"])).lower()
        assert "comment" in haystack, e["id"]

    empty = (await clients.get("/catalog/search", params={"q": "tiktok flibbertigibbet"})).json()
    assert empty["total"] == 0 and empty["results"] == []
    assert empty["hints"], "a dead end still has to say what to try next"


async def test_search_respects_limit_and_an_empty_query(clients: AsyncClient):
    body = (await clients.get("/catalog/search", params={"q": "tiktok", "limit": 3})).json()
    assert len(body["results"]) == 3 and body["total"] > 3
    assert any("more matches" in h for h in body["hints"])
    assert (await clients.get("/catalog/search", params={"q": "  "})).json()["results"] == []


# ---- endpoint detail -----------------------------------------------------------------------
async def test_endpoint_detail_answers_everything_in_one_call(clients: AsyncClient):
    """The inspect half: params, price, the captured response and a paste-ready command — plus the
    OTHER providers doing the same job, so comparing costs never needs a second request."""
    r = await clients.get("/catalog/endpoints/justoneapi.tiktok.video.comments")
    assert r.status_code == 200, r.text
    body = r.json()
    e = body["endpoint"]
    assert e["id"] == "justoneapi.tiktok.video.comments"
    assert e["capability"] == "tiktok.video.comments" and e["platform_label"] == "TikTok"
    assert e["input"]["queryParams"], "the detail view's whole job is showing what to SEND"

    assert body["provider"]["service"] == "justoneapi"
    assert body["provider"]["pricing_url"], "provider-wide facts live once at the top of the yaml file"

    sibs = {s["id"] for s in body["siblings"]}
    assert "tikhub.tiktok.video.comments" in sibs
    assert e["id"] not in sibs, "an endpoint is not its own sibling"

    tmpl = body["call_template"]
    assert tmpl.startswith("treg call justoneapi.tiktok.video.comments")
    assert "--query awemeId=" in tmpl, "the verifier's proven request is what makes it paste-ready"
    assert isinstance(body["example_response"], (dict, list)), "inline, so parsing needs no second call"


async def test_instagram_message_send_is_try_ready_but_never_auto_verified(clients: AsyncClient):
    """App Review needs a deliberate UI send, so the catalog must explain the real recipient/body
    shape while keeping the write action out of automated verification."""
    r = await clients.get("/catalog/endpoints/instagram.instagram.message.send")
    assert r.status_code == 200, r.text
    ep = r.json()["endpoint"]
    assert ep["method"] == "POST" and ep["path"] == "/{ig_user_id}/messages"
    assert ep["authorization_methods"] == ["instagram-login", "facebook-page"]
    assert set(ep["input"]["pathParams"]) == {"ig_user_id", "page_id"}
    assert ep["authorization_paths"]["facebook-page"].startswith("/{page_id}/messages")
    assert ep["input"]["bodyType"] == "json"
    assert set(ep["input"]["body"]) == {"recipient", "message"}
    assert "IGSID" in ep["input"]["body"]["recipient"]["note"]
    loaded = cs.load().by_id[ep["id"]]
    assert loaded["verified"] is None and not loaded["test_request"]


async def test_instagram_conversations_keep_direct_and_page_routes(clients: AsyncClient):
    r = await clients.get("/catalog/endpoints/instagram.x.user-messages")
    assert r.status_code == 200, r.text
    ep = r.json()["endpoint"]
    assert ep["path"] == "/{ig_user_id}/conversations"
    assert set(ep["input"]["pathParams"]) == {"ig_user_id", "page_id"}
    assert ep["authorization_paths"]["facebook-page"].startswith("/{page_id}/conversations")
    assert "?" not in ep["authorization_paths"]["facebook-page"]
    assert ep["input"]["queryParams"]["platform"]["required"] is True
    assert ep["input"]["queryParams"]["platform"]["authorization_methods"] == ["facebook-page"]
    assert "Existing Facebook-backed connections" in ep["input"]["note"]
    assert ep["input"]["pathParams"]["ig_user_id"]["authorization_methods"] == ["instagram-login"]
    assert ep["input"]["pathParams"]["page_id"]["authorization_methods"] == ["facebook-page"]
    assert "--authorization-method" not in ep["call_template"]
    assert "page_id=" not in ep["call_template"]
    page_only = cs.load().by_id["instagram.x.hashtag-search"]
    assert "--authorization-method facebook-page" in cs.call_template(page_only)


def test_every_instagram_path_placeholder_has_a_declared_try_input():
    """The UI, CLI and agents all learn path values from `input.pathParams`; runtime parsing the
    braces independently is only the last guard, not a usable endpoint contract."""
    endpoints = [ep for ep in cs.load().by_id.values() if ep["provider"] == "instagram"]
    assert len(endpoints) == 32
    for ep in endpoints:
        paths = [ep.get("path") or "", *(ep.get("authorization_paths") or {}).values()]
        placeholders = {name for path in paths for name in re.findall(r"{([A-Za-z0-9_]+)}", path)}
        declared = set((((ep.get("input") or {}).get("pathParams")) or {}))
        assert placeholders <= declared, ep["id"]
    extended = [ep for ep in endpoints if ep["tier"] == "extended"]
    assert len(extended) == 22
    assert all(ep.get("input") for ep in extended)


def test_instagram_extended_actions_declare_every_required_write_value():
    cat = cs.load()
    expected = {
        "instagram.x.media-comment-create": ({"ig_media_id"}, {"message"}),
        "instagram.x.comment-reply-create": ({"ig_comment_id"}, {"message"}),
        "instagram.x.comment-hide": ({"ig_comment_id"}, {"hide"}),
        "instagram.x.comment-delete": ({"ig_comment_id"}, set()),
    }
    for endpoint_id, (path_names, query_names) in expected.items():
        inp = cat.by_id[endpoint_id]["input"]
        assert set(inp.get("pathParams") or {}) == path_names
        assert set(inp.get("queryParams") or {}) == query_names
        assert all(spec.get("required") for spec in (inp.get("pathParams") or {}).values())
        assert all(spec.get("required") for spec in (inp.get("queryParams") or {}).values())
    hide = cat.by_id["instagram.x.comment-hide"]
    assert hide["path"] == "/{ig_comment_id}"
    assert hide["input"]["queryParams"]["hide"]["type"] == "boolean"


def test_page_only_instagram_contracts_expose_required_meta_parameters():
    catalog = cs.load()
    product_search = catalog.by_id["instagram.x.catalog-product-search"]
    assert set(product_search["input"]["queryParams"]) >= {"catalog_id", "q"}
    assert product_search["input"]["queryParams"]["catalog_id"]["required"] is True

    appeal = catalog.by_id["instagram.x.user-product-appeal"]
    assert appeal["input"]["queryParams"]["product_id"]["required"] is True

    discovery = catalog.by_id["instagram.x.user-business-discovery"]
    assert discovery["path"] == "/{ig_user_id}"
    assert discovery["input"]["queryParams"]["fields"]["required"] is True
    assert "business_discovery.username(" in discovery["input"]["queryParams"]["fields"]["example"]
    assert "--query 'fields=business_discovery.username(" in cs.call_template(discovery)


def test_instagram_catalog_paths_do_not_embed_query_strings():
    """The relay sends caller params separately, so static query values belong in `input`."""
    endpoints = [ep for ep in cs.load().by_id.values() if ep["provider"] == "instagram"]
    for ep in endpoints:
        assert "?" not in ep["path"], ep["id"]
        assert all("?" not in path for path in (ep.get("authorization_paths") or {}).values()), ep["id"]


def test_instagram_publishing_notes_use_current_meta_quota():
    """Feedback #430: Content Publishing guide is 100 API-published posts / 24h, not 50.

    Meta's content_publishing_limit reference still samples quota_total: 50 in
    places; catalog prose follows the Content Publishing guide and tells agents
    to read remaining allowance live rather than hard-coding only one number.
    Settlement is unchanged.
    """
    cat = cs.load()
    create = cat.by_id["instagram.instagram.media.container.create"]
    publish = cat.by_id["instagram.instagram.post.publish"]
    limit = cat.by_id["instagram.x.user-content-publishing-limit"]
    quota_phrase = "100 API-published posts per 24-hour moving period"
    carousel = "carousels count as one"
    live_check = "GET /{ig_user_id}/content_publishing_limit"
    for note in (
        create["cost"]["note"],
        publish["input"]["note"],
        publish["cost"]["note"],
        limit["summary"],
    ):
        assert quota_phrase in note
        assert carousel in note
        assert "50" not in note
        assert "50-posts" not in note
        assert "50-per-24h" not in note
    assert live_check in publish["input"]["note"]
    assert "before a batch" in publish["input"]["note"]
    assert limit["path"] == "/{ig_user_id}/content_publishing_limit"


async def test_retired_rows_leave_discovery_but_keep_an_actionable_direct_lookup(clients: AsyncClient):
    """A cached endpoint id needs its migration story, while a new agent must never discover it."""
    retired = "tikhub.x.linkedin-web-search-jobs"
    successor = "tikhub.x.linkedin-web-v2-search-jobs"
    cat = cs.load()
    assert retired in cat.by_id
    assert retired not in {ep["id"] for ep in cat.endpoints}
    assert cat.by_id[retired]["status"] == "retired"
    assert cat.by_id[retired]["superseded_by"] == successor
    assert not cat.by_id[successor]["status"]

    detail = (await clients.get(f"/catalog/endpoints/{retired}")).json()["endpoint"]
    assert detail["status"] == "retired"
    assert "collapsed" in detail["status_note"]
    assert detail["superseded_by"] == successor
    search = (await clients.get("/catalog/search", params={"q": retired})).json()
    assert retired not in {row["id"] for row in search["results"]}


def test_lusha_decision_makers_is_a_tombstone_pointing_at_buying_group():
    """Lusha removed POST /v3/contacts/decision-makers on 2026-08-12 (changelog 2.9.0); the legacy
    handler still answered companies-only bodies but rejected `contactsLimit`, so the documented
    spend cap never applied. The id stays as a tombstone with its story; the successor is the only
    operation that honours the cap and is the row an agent may now discover and spend against."""
    cat = cs.load()
    retired, successor = "lusha.x.decision-makers", "lusha.x.buying-group"
    old, new = cat.by_id[retired], cat.by_id[successor]
    assert old["status"] == "retired"
    assert old["superseded_by"] == successor
    assert "contactsLimit" in old["status_note"] and "2026-08-12" in old["status_note"]
    assert "contactsLimit" not in old["input"].get("body", {}), (
        "the retired path must not advertise a cap it never honoured")
    assert "personas" not in old["input"].get("body", {})
    assert retired not in {ep["id"] for ep in cat.endpoints}
    assert not cat.platform_eligible(old), "a tombstone is never an offer"

    assert not new.get("status")
    assert new["path"] == "/v3/contacts/buying-group" and new["method"] == "POST"
    assert new["capability"] == old["capability"] == "people.decision_makers"
    assert new["cost"]["type"] == "per_result" and new["cost"]["value"] == 1
    assert new["cost"]["currency"] == "credit"
    assert new["test_request"]["body"] == {"companies": [{"domain": "lusha.com"}], "contactsLimit": 1}
    assert new["input"]["body"]["contactsLimit"]["type"] == "integer"
    assert new["input"]["body"]["personas"]["enum"] == [
        "decision_maker", "potential_champion", "end_user"]
    assert "60" in new["input"]["note"] and "contactsLimit" in new["input"]["note"]
    assert not new.get("verified") and not new.get("example_file"), "no live probe was run"
    assert cat.platform_eligible(new), "the successor must stay servable on treg's key"
    live = {ep["id"] for ep in cat.endpoints if ep.get("capability") == "people.decision_makers"}
    assert successor in live and retired not in live


def test_tikhub_drift_repair_preserves_markers_and_rescues_only_real_jobs():
    cat = cs.load()
    marked = [ep for ep in cat.by_id.values()
              if ep["provider"] == "tikhub" and ep.get("status")]
    assert len(marked) == 50
    assert {ep["status"] for ep in marked} == {"retired"}
    assert sum(bool(ep.get("superseded_by")) for ep in marked) == 9
    assert all(ep.get("status_note") for ep in marked)
    assert all(ep["superseded_by"] in cat.by_id and
               not cat.by_id[ep["superseded_by"]].get("status")
               for ep in marked if ep.get("superseded_by"))

    people = cat.by_id["justoneapi.x.linkedin-search-user-v1"]
    assert people["capability"] == "linkedin.search.people"
    comments = cat.by_id["tikhub.x.linkedin-web-v2-get-post-comments"]
    assert comments["path"] == "/api/v1/linkedin/web_v2/get_post_comments"
    assert comments["method"] == "GET"
    assert comments["capability"] == "linkedin.post.comments"


async def test_call_template_carries_method_and_body_for_a_post(clients: AsyncClient):
    body = (await clients.get("/catalog/endpoints/dataforseo.web.backlinks.summary")).json()
    tmpl = body["call_template"]
    assert tmpl.startswith("treg call dataforseo.web.backlinks.summary --method POST")
    assert "--data '[{\"target\":\"moz.com\"" in tmpl, "single-quoted JSON survives a shell paste"


async def test_a_credit_price_is_served_in_usd_per_provider(clients: AsyncClient):
    """A "credit" is a provider-scoped unit, not a currency: each provider converts at its OWN
    fx.yaml rate ($0.00188 on scrapecreators, $0.026 on apollo), and a provider with no rate
    stays native — "we don't know" must never surface as a dollar figure."""
    priced = (await clients.get("/catalog/endpoints/scrapecreators.x.v1-amazon-shop")).json()["endpoint"]["cost"]
    assert priced["currency"] == "credit" and priced["value"]
    assert priced["usd"] == round(priced["value"] * cs.load().credit_rates["scrapecreators"], 6)
    assert priced["usd"] > 0, "a credit rate exists, so the dashboard gets a comparable number"

    apollo = (await clients.get("/catalog/endpoints/apollo.people.enrich")).json()["endpoint"]["cost"]
    assert apollo["currency"] == "credit" and apollo["value"]
    assert apollo["usd"] == round(apollo["value"] * cs.load().credit_rates["apollo"], 6)
    assert apollo["usd"] != priced["usd"] or apollo["value"] != priced["value"], \
        "two providers' credits are unrelated units, priced by their own rates"

    unrated = cs.load().cost_view({"type": "per_call", "value": 3, "currency": "credit"}, "no-such-provider")
    assert unrated["usd"] is None, "no published rate: display credits, never a guessed dollar"


# ---- cost: units, provenance and platform eligibility ----------------------------------------
async def test_per_divides_so_a_cpm_price_serves_per_row():
    """A provider that quotes dollars per 1,000 rows must not serve as dollars per row. `per` is what
    makes "$2.00 per 1,000" and "$0.002 each" the same fact instead of a 1,000x error."""
    cat = cs.load()
    ep = cat.by_id["spyfu.google.domain.paid_keywords"]
    cost = cat.cost_view(ep["cost"], ep["provider"])
    assert (cost["value"], cost["per"], cost["unit"]) == (2.0, 1000, "row")
    assert cost["usd"] == 0.002, "usd is the price of ONE chargeable event, whatever the quote unit"


async def test_a_provider_meter_converts_per_meter_not_per_provider():
    """`currency: unit` is the provider's own meter, and a provider can spend several at once.
    Majestic's analysis units and index-item units are as unrelated as two providers' credits."""
    cat = cs.load()
    assert set(cat.unit_rates["majestic"]) == {"analysis_unit", "retrieval_unit", "index_item_unit"}
    listing = cat.by_id["majestic.web.backlinks.list"]["cost"]
    assert listing["currency"] == "unit" and listing["unit"] == "analysis_unit"
    assert listing["value"] == 5000
    # Each pool converts at ITS OWN researched rate (fx.yaml, 2026-07-31): 5,000 analysis units at
    # $0.000004 — not at the retrieval or index-item rate, which differ by orders of magnitude.
    assert cat.cost_view(listing, "majestic")["usd"] == 0.02  # 5,000 × $0.000004, rounded at 9 dp

    # Moz's row quota is a meter too. It used to carry no `currency` at all, defaulted to USD, and
    # served every Moz route at $1.00 per row — the exact failure `currency: unit` prevents.
    moz = cat.by_id["moz.web.url.metrics"]["cost"]
    assert moz["currency"] == "unit" and moz["unit"] == "quota_row"
    assert cat.cost_view(moz, "moz")["usd"] == 0.006667  # 1 row × the researched $0.006667/row
    # Semrush is the one that stays native: package prices are sales-gated, so no rate is derivable
    # and its `usd` must remain None rather than a guess.
    assert cat.unit_rates["semrush"]["api_unit"] is None


async def test_free_is_spelled_one_way_and_prices_at_zero():
    """661 endpoints wrote free three ways, which left `usd` null on most of them — downstream,
    indistinguishable from "price unknown", which is the one thing free must never look like."""
    cat = cs.load()
    frees = [e for e in cat.endpoints if (e["cost"] or {}).get("type") == "free"]
    assert len(frees) > 500
    for ep in frees:
        cost = cat.cost_view(ep["cost"], ep["provider"])
        assert (cost["value"], cost["currency"], cost["unit"]) == (0, "USD", "call"), ep["id"]
        assert cost["usd"] == 0, ep["id"]


async def test_an_observed_price_is_provenanced_without_a_url():
    """DataForSEO prices per API family, not per route, so its extended entries carry only the
    charge the provider REPORTED at verification. That is the strongest provenance there is — it is
    what was actually billed — and its evidence is the captured response, not a pricing page."""
    cat = cs.load()
    ep = cat.by_id["dataforseo.x.ai-optimization-llm-mentions-historical-live"]
    cost = cat.cost_view(ep["cost"], ep["provider"])
    assert cost["source"] == "observed" and cost["confidence"] == "verified"
    assert cost["checked"] == ep["verified"], "the price was confirmed the day the call was made"
    assert "source_url" not in cost and cost["usd"] == cost["value"]


async def test_platform_eligibility_refuses_everything_it_cannot_prove():
    """The predicate behind spending treg's own key. Asymmetric on purpose: an unknown price must
    read as "refuse", never as free — so every axis is checked independently."""
    cat = cs.load()
    ok = cat.by_id["tikhub.tiktok.user.profile"]
    assert cat.platform_eligible(ok), "priced from the provider's live rate card, and live-called"

    def with_cost(**changes):
        return {**ok, "cost": {**ok["cost"], **changes}}

    assert cat.platform_eligible(with_cost(confidence="documented")), \
        "2026-07-31 policy: a provider-published rate is billable"
    assert not cat.platform_eligible(with_cost(confidence="inferred")), "a guess is not a rate"
    assert not cat.platform_eligible(with_cost(value=None, confidence="unknown"))
    assert not cat.platform_eligible(with_cost(currency="credit")), "tikhub has no credit rate"
    assert not cat.platform_eligible({**ok, "scope": "own_account"})
    assert not cat.platform_eligible({**ok, "kind": "account"})
    assert cat.platform_eligible({**ok, "verified": None}), \
        "2026-07-31 policy: the live-called stamp is no longer required — a broken route fails unbilled"
    assert not cat.platform_eligible({**ok, "cost": None}), "no price block ⇒ refuse, not free"
    # `ok` is fully priced, so status is the ONLY axis left to explain a refusal here. A marked row
    # keeps its historical price — it is retained to explain a cached id, not to be sold — and an
    # eligible one would put treg's own key behind a route the provider has already removed.
    assert not cat.platform_eligible({**ok, "status": "retired"}), "a retired route is not an offer"
    assert not cat.platform_eligible({**ok, "status": "broken"}), "a broken route is not an offer"
    # A plan-gated route works upstream but treg's own subscription cannot serve it — a customer
    # discovered exactly this the hard way, via a run of 403s on akta's alternative-data family.
    # It stays discoverable (a team's OWN key on a bigger plan serves it) but is never an offer.
    assert not cat.platform_eligible({**ok, "platform_blocked": "plan gate"}), \
        "a plan-gated route is not an offer, however well priced"
    blocked = cat.by_id["akta.companies.headcount_trend"]
    assert blocked["platform_blocked"], "the akta alt-signals family carries its plan-gate reason"
    assert not cat.platform_eligible(blocked)


async def test_eligibility_rides_on_the_served_row(clients: AsyncClient):
    """A client deciding whether a call needs a credential must not have to re-derive the rule."""
    cat = cs.load()
    body = (await clients.get("/catalog/endpoints/tikhub.tiktok.user.profile")).json()
    assert body["endpoint"]["platform_eligible"] is True
    # Every launch provider has to have a usable surface, or tier 4 ships with nothing behind it.
    eligible = [e for e in cat.endpoints if cat.platform_eligible(e)]
    for provider in ("tikhub", "dataforseo", "scrapecreators"):
        assert sum(1 for e in eligible if e["provider"] == provider) > 20, provider


async def test_unknown_endpoint_is_404(clients: AsyncClient):
    r = await clients.get("/catalog/endpoints/tikhub.tiktok.nope")
    assert r.status_code == 404 and "tikhub.tiktok.nope" in r.text


async def test_unknown_endpoint_access_is_a_clean_404(clients: AsyncClient):
    r = await clients.get("/catalog/endpoints/not.a.real.endpoint/access")
    assert r.status_code == 404
    assert "unknown endpoint" in r.text


def test_hunter_multi_domain_search_uses_official_query_filters():
    """Hunter Multi-Domain Search (Beta) rejects a JSON `companies` array with
    `wrong_params` / `Unknown parameter: companies.` Official docs take company
    and email filters as query parameters on POST (feedback #183)."""
    cat = cs.load()
    ep = cat.by_id["hunter.x.multi-domain-search"]
    inp = ep.get("input") or {}
    body = inp.get("body") or {}
    query = inp.get("queryParams") or {}
    test = ep.get("test_request") or {}

    assert "companies" not in body
    assert "companies" not in query
    assert "companies" not in (test.get("body") or {})
    assert "companies" not in (test.get("queryParams") or {})
    assert "location" in query
    assert "department" in query
    assert "company_name" in query
    assert query["location"].get("example") == "US"
    assert test.get("queryParams", {}).get("location") == "US"
    assert test.get("queryParams", {}).get("department") == "executive"
    assert "body" not in test

    tmpl = cs.call_template(ep)
    assert tmpl.startswith("treg call hunter.x.multi-domain-search --method POST")
    assert "--data" not in tmpl
    assert "companies" not in tmpl
    argv = shlex.split(tmpl)
    queries = [argv[i + 1] for i, part in enumerate(argv) if part == "--query"]
    assert "location=US" in queries
    assert "department=executive" in queries


def test_serpstat_jsonrpc_id_is_required_in_call_template():
    """Serpstat rejects a JSON-RPC body without top-level `id`. `call_template` only
    includes required body fields via `_required_examples`, so `id` must be required
    on every Serpstat endpoint that declares it."""
    cat = cs.load()
    serpstat = [ep for ep in cat.endpoints if ep["provider"] == "serpstat"]
    assert len(serpstat) >= 12, "every curated Serpstat endpoint is in play"
    for ep in serpstat:
        field = ((ep.get("input") or {}).get("body") or {}).get("id")
        assert isinstance(field, dict), ep["id"]
        assert field.get("required") is True, ep["id"]
        assert field.get("example") == "1", ep["id"]

    tmpl = cs.call_template(cat.by_id["serpstat.web.backlinks.summary"])
    assert tmpl.startswith("treg call serpstat.web.backlinks.summary --method POST")
    argv = shlex.split(tmpl)
    data = json.loads(argv[argv.index("--data") + 1])
    assert data["id"] == "1"
    assert data["method"] == "SerpstatBacklinksProcedure.getSummaryV2"


def test_serpstat_call_template_nests_jsonrpc_params():
    """Dotted body keys (`params.domain`) must become a nested JSON-RPC params object
    in the paste-ready command. A flat payload is invalid JSON-RPC 2.0 and Serpstat
    rejects it (feedback #644 / #55)."""
    cat = cs.load()
    nested = []
    for ep in cat.endpoints:
        if ep["provider"] != "serpstat":
            continue
        body = (ep.get("input") or {}).get("body") or {}
        dotted = {
            k: v for k, v in body.items()
            if isinstance(k, str) and k.startswith("params.")
            and isinstance(v, dict) and v.get("required")
        }
        if dotted:
            nested.append((ep, dotted))
    assert nested, "Serpstat JSON-RPC endpoints declare params.* fields"
    assert any(ep["id"] == "serpstat.google.domain.competitors" for ep, _ in nested)

    for ep, dotted in nested:
        tmpl = cs.call_template(ep)
        argv = shlex.split(tmpl)
        data = json.loads(argv[argv.index("--data") + 1])
        assert "id" in data, ep["id"]
        assert "method" in data, ep["id"]
        assert isinstance(data.get("params"), dict), ep["id"]
        assert data["params"] != "<object>", ep["id"]
        assert all("." not in k for k in data), ep["id"]
        for dotted_key, spec in dotted.items():
            example = spec.get("example")
            if example in (None, ""):
                continue
            cursor = data
            for part in dotted_key.split("."):
                assert isinstance(cursor, dict), (ep["id"], dotted_key)
                assert part in cursor, (ep["id"], dotted_key)
                cursor = cursor[part]
            assert cursor == example, (ep["id"], dotted_key)

    competitors_argv = shlex.split(
        cs.call_template(cat.by_id["serpstat.google.domain.competitors"]))
    competitors = json.loads(competitors_argv[competitors_argv.index("--data") + 1])
    assert competitors == {
        "method": "SerpstatDomainProcedure.getOrganicCompetitorsPage",
        "id": "1",
        "params": {"domain": "serpstat.com", "se": "g_us"},
    }


def test_call_template_unflattens_dotted_body_keys_and_drops_parent_placeholder():
    """A synthetic JSON-RPC schema: parent `params` plus `params.*` children. The
    placeholder must not survive next to the nested object."""
    ep = {
        "id": "demo.web.rpc", "method": "POST",
        "input": {"body": {
            "method": {"type": "string", "required": True, "example": "Do.Thing"},
            "params": {"type": "object", "required": True},
            "id": {"type": "string", "required": True, "example": "1"},
            "params.domain": {"type": "string", "required": True, "example": "example.com"},
            "params.se": {"type": "string", "required": True, "example": "g_us"},
        }},
    }
    argv = shlex.split(cs.call_template(ep))
    data = json.loads(argv[argv.index("--data") + 1])
    assert data == {
        "method": "Do.Thing",
        "id": "1",
        "params": {"domain": "example.com", "se": "g_us"},
    }


def test_call_template_falls_back_to_documented_examples(tmp_path):
    """No test_request (an unverified endpoint) still yields a usable line: required params only,
    valued by their documented example, or a typed placeholder when even that is missing."""
    (tmp_path / "capabilities.yaml").write_text("platforms: {web: Web}\ncapabilities: {}\n")
    (tmp_path / "moz.yaml").write_text(
        "provider: moz\nendpoints:\n"
        "  - id: moz.web.thing\n    platform: web\n    method: GET\n    path: /v1/{site}/links\n"
        "    input:\n"
        "      pathParams: {site: {type: string, required: true, example: moz.com}}\n"
        "      queryParams:\n"
        "        limit: {type: integer, required: true}\n"
        "        offset: {type: integer, required: false}\n")
    ep = cs.load(directory=tmp_path).by_id["moz.web.thing"]
    # Path params ride as --query (the server folds them into the path), required query after.
    assert cs.call_template(ep) == "treg call moz.web.thing --query site=moz.com --query 'limit=<integer>'"


def test_call_templates_share_wire_encoding_and_quote_complete_query_arguments():
    """The detail command is paste-ready, including arrays, booleans and shell metacharacters."""
    import shlex

    cat = cs.load()
    meta = shlex.split(cs.call_template(cat.by_id["meta-ad-library.meta-ads.library.search"]))
    meta_query = [meta[i + 1] for i, part in enumerate(meta) if part == "--query"]
    assert 'ad_reached_countries=["US"]' in meta_query

    synthetic = {
        "id": "demo.web.query", "method": "GET",
        "input": {"queryParams": {
            "enabled": {"type": "boolean", "required": True, "example": True},
            "phrase": {"type": "string", "required": True, "example": "two words"},
        }},
    }
    line = cs.call_template(synthetic)
    assert shlex.split(line)[3:] == ["--query", "enabled=true", "--query", "phrase=two words"]
    assert "'phrase=two words'" in line


def test_gtm_catalog_paths_and_declared_parameters_are_the_same_contract():
    """Every GTM catalog command must ask for the atomic ids its path actually substitutes.

    Google Discovery describes these as one semantic parent/path resource, but passing
    ``accounts/…/containers/…`` through one treg placeholder encodes the hierarchy as ``%2F``.
    The curated and generated tiers therefore expose the flattened path segments instead.
    """
    cat = cs.load()
    endpoints = [ep for ep in cat.by_id.values() if ep["provider"] == "google-tag-manager"]
    assert endpoints
    for ep in endpoints:
        placeholders = set(re.findall(r"{([A-Za-z0-9_]+)}", ep.get("path") or ""))
        declared = set((((ep.get("input") or {}).get("pathParams")) or {}))
        assert placeholders == declared, ep["id"]

    core = [ep for ep in endpoints if ep["tier"] == "core"]
    for ep in core:
        for spec in (((ep.get("input") or {}).get("pathParams")) or {}).values():
            assert "/" not in str((spec or {}).get("example") or ""), ep["id"]

    line = cs.call_template(cat.by_id["google-tag-manager.workspaces"])
    assert "--query account_id=123456" in line
    assert "--query container_id=789" in line
    assert "parent=" not in line


def test_catalog_validator_rejects_an_unknown_endpoint_array_encoding(tmp_path, capsys):
    """The endpoint encoding declaration is schema, not free-form prose. Exercise the real
    validator so deleting its validation block cannot leave a falsely green test suite."""
    cv = _load_validator()
    real = cv.CATALOG
    for name in ("capabilities.yaml", "fx.yaml"):
        (tmp_path / name).write_text((real / name).read_text())
    meta = (real / "meta-ad-library.yaml").read_text()
    meta = meta.replace("queryArrayEncoding: json", "queryArrayEncoding: nonsense", 1)
    (tmp_path / "meta-ad-library.yaml").write_text(meta)
    original = cv.CATALOG
    try:
        cv.CATALOG = tmp_path
        result = cv.main(["meta-ad-library"])
    finally:
        cv.CATALOG = original
    output = capsys.readouterr().out
    assert result == 1
    assert "input.queryArrayEncoding must be one of" in output


# ---- example responses -------------------------------------------------------------------
async def test_example_route_serves_a_captured_response(clients: AsyncClient):
    r = await clients.get("/catalog/examples/tikhub.tiktok.user.profile")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    assert isinstance(r.json(), (dict, list))


async def test_example_route_404s_an_unknown_endpoint(clients: AsyncClient):
    assert (await clients.get("/catalog/examples/tikhub.tiktok.nope")).status_code == 404


async def test_example_route_refuses_path_traversal(clients: AsyncClient):
    """The id is resolved through the catalog before any path is built, so a traversal attempt is
    just a miss — it must never read a file, encoded or not."""
    for probe in ("../../api.py", "..%2f..%2fapi.py", "%2e%2e%2f%2e%2e%2fconfig.py",
                  "/etc/passwd", "tikhub.tiktok.user.profile/../../api.py"):
        r = await clients.get(f"/catalog/examples/{probe}")
        assert r.status_code == 404, (probe, r.status_code)
        assert "def " not in r.text, probe


# ---- loader ------------------------------------------------------------------------------
def test_an_extended_file_merges_into_the_same_provider(tmp_path):
    """Curation splits a provider across `<service>.yaml` and `<service>.extended.yaml`; both land
    under one provider, and the extended tier is what keeps the long tail out of the capability view."""
    (tmp_path / "capabilities.yaml").write_text(
        "platforms: {tiktok: TikTok}\ncapabilities: {tiktok.user.profile: A profile}\n")
    (tmp_path / "tikhub.yaml").write_text(
        "provider: tikhub\nendpoints:\n"
        "  - id: tikhub.tiktok.user.profile\n"
        "    capability: tiktok.user.profile\n"
        "    platform: tiktok\n    method: GET\n    path: /a\n")
    (tmp_path / "tikhub.extended.yaml").write_text(
        "provider: tikhub\nendpoints:\n"
        "  - id: tikhub.tiktok.user.mix\n"
        "    platform: tiktok\n    tier: extended\n    method: GET\n    path: /b\n"
        "  - id: tikhub.tiktok.user.stats\n"
        "    capability: tiktok.user.profile\n"
        "    platform: tiktok\n    tier: extended\n    method: GET\n    path: /c\n")

    cat = cs.load(directory=tmp_path)
    assert {e["id"] for e in cat.for_provider("tikhub")} == {
        "tikhub.tiktok.user.profile", "tikhub.tiktok.user.mix", "tikhub.tiktok.user.stats"}
    tiers = {e["id"]: e["tier"] for e in cat.endpoints}
    assert tiers["tikhub.tiktok.user.profile"] == "core"
    assert tiers["tikhub.tiktok.user.mix"] == "extended"


def test_async_defaults_apply_per_endpoint_and_an_endpoint_block_replaces_them_whole(tmp_path):
    (tmp_path / "capabilities.yaml").write_text(
        "platforms: {video-gen: Video}\ncapabilities: {video-gen.from_text: Generate}\n")
    (tmp_path / "demo.yaml").write_text(
        "provider: demo\n"
        "async:\n"
        "  id_from: task_id\n"
        "  poll: {endpoint: demo.video-gen.task.status, param: {in: pathParams, name: task_id}}\n"
        "  status: {path: status, success: [done], failure: [failed]}\n"
        "  result: {path: output.url, ttl_note: 1h}\n"
        "  interval: 10\n"
        "endpoints:\n"
        "  - id: demo.video-gen.from-text\n"
        "    capability: video-gen.from_text\n    platform: video-gen\n"
        "    method: POST\n    path: /generate\n"
        "    cost: {type: per_success, table: [{when: {body.model: a}, value: 1}], "
        "fallback: {value: 1, note: upper}, currency: USD}\n"
        "  - id: demo.video-gen.dynamic\n"
        "    capability: video-gen.from_text\n    platform: video-gen\n"
        "    method: POST\n    path: /dynamic\n"
        "    async:\n"
        "      id_from: id\n"
        "      poll: {url_from: urls.get, url_hosts: [api.example.com]}\n"
        "      status: {path: status, success: [succeeded], failure: [failed]}\n"
        "      result: {fetch: demo.video-gen.content, fetch_param: {in: pathParams, name: id, value_from: id}}\n"
        "      interval: 20\n"
        "  - id: demo.video-gen.task.status\n    platform: video-gen\n    tier: extended\n"
        "    method: GET\n    path: /tasks/{task_id}\n"
        "  - id: demo.video-gen.content\n    platform: video-gen\n    tier: extended\n"
        "    method: GET\n    path: /content/{id}\n")

    cat = cs.load(directory=tmp_path)
    first = cat.by_id["demo.video-gen.from-text"]
    assert first["async"]["status"] == {"path": "status", "success": ["done"], "failure": ["failed"]}
    assert first["async"]["interval"] == 10
    assert first["cost"]["table"][0]["when"] == {"body.model": "a"}
    # An endpoint block is the whole protocol: nothing from the provider default leaks into it.
    dynamic = cat.by_id["demo.video-gen.dynamic"]["async"]
    assert dynamic["poll"] == {"url_from": "urls.get", "url_hosts": ["api.example.com"]}
    assert dynamic["result"] == {
        "fetch": "demo.video-gen.content",
        "fetch_param": {"in": "pathParams", "name": "id", "value_from": "id"}}
    assert dynamic["status"] == {"path": "status", "success": ["succeeded"], "failure": ["failed"]}
    assert cs.effective_async_descriptor({"id_from": "a"}, False) is None
    assert cs.effective_async_descriptor({"id_from": "a"}, None) == {"id_from": "a"}
    assert cs.effective_async_descriptor(None, {"id_from": "b"}) == {"id_from": "b"}


def test_ai_generation_taxonomy_and_chinese_alias_tokens_are_loaded():
    cat = cs.load()
    assert cat.platforms["video-gen"]["category"] == "AI generation"
    assert cat.platforms["image-gen"]["category"] == "AI generation"
    assert cat.platforms["voice-gen"] == {
        "label": "Voice generation",
        "category": "AI generation",
        "summary": "Text-to-speech across voice models, with prices side by side.",
    }
    assert {"video-gen.from_text", "video-gen.from_image", "video-gen.task.status",
            "image-gen.from_text", "image-gen.edit", "voice-gen.from_text"} <= set(cat.capabilities)
    text_to_video_zh = "\u6587\u751f\u89c6\u9891"
    assert cat.aliases[text_to_video_zh] == ["text-to-video"]
    assert cs._tokens(f"{text_to_video_zh} text-to-video") == [
        text_to_video_zh, "text", "to", "video"]


async def test_ai_generation_pages_keep_comparisons_curated_and_coverage_in_models(
        clients: AsyncClient):
    video = (await clients.get("/catalog/platforms/video-gen")).json()
    # One ledger of standalone model rows. Generation models are not interchangeable, so the
    # job-level capabilities (video-gen.from_text/.from_image) hold NO endpoints - a merged row
    # comparing Hailuo with Wan or Seedance would be a false comparison. Curated core rows carry
    # per-model capabilities instead (single-provider, so they render as singles in the wall);
    # the job-level rows return only when specific models are hand-picked into them.
    assert {section["domain"] for section in video["domains"]} == {"models"}
    rows = video["domains"][0]["rows"]
    # reAPI and PiAPI share per-model join keys on purpose, so the same model over two routes is
    # the one merged row the wall is built for (a real comparison of price and filter policy).
    shared = {"video-gen.seedance-2-5.generate", "video-gen.seedance-2-5-unrestricted.generate"}
    assert {row["capability"] for row in rows if row["kind"] != "single"} == shared
    providers = {row["capability"]: {e["provider"] for e in row["endpoints"]} for row in rows}
    # the official OpenRouter route joins the default-filter row; only the resellers relax the filter
    assert providers["video-gen.seedance-2-5.generate"] == {"reapi", "piapi", "openrouter"}
    assert providers["video-gen.seedance-2-5-unrestricted.generate"] == {"reapi", "piapi"}
    caps = {row["capability"] for row in rows}
    assert "video-gen.from_text" not in caps and "video-gen.from_image" not in caps
    ids = {endpoint["id"] for row in rows for endpoint in row["endpoints"]}
    assert {"minimax.video-gen.from_text", "minimax.video-gen.from_image",
            "openrouter.video-gen.wan-3-0.from_text",
            "replicate.video-gen.seedance-1-lite",
            "reapi.video-gen.seedance-2-5.unrestricted",
            "piapi.video-gen.seedance-2-5.less-restriction"} <= ids

    image = (await clients.get("/catalog/platforms/image-gen")).json()
    assert {section["domain"] for section in image["domains"]} == {"models"}
    image_rows = [row for section in image["domains"] for row in section["rows"]]
    shared_images = {"image-gen.gpt-image-2-5.generate", "image-gen.gpt-image-2.generate",
                     "image-gen.gemini-3-pro-image.generate"}
    assert {row["capability"] for row in image_rows if row["kind"] != "single"} == shared_images
    # every image model row compares the two resellers with Replicate's official model
    assert all({e["provider"] for e in row["endpoints"]} == {"reapi", "piapi", "replicate"}
               for row in image_rows if row["capability"] in shared_images)
    assert "image-gen.from_text" not in {row["capability"] for row in image_rows}
    image_ids = {endpoint["id"] for row in image_rows for endpoint in row["endpoints"]}
    assert {"minimax.image-gen.from_text", "replicate.image-gen.flux-schnell",
            "reapi.image-gen.gemini-3-pro-image", "piapi.image-gen.gpt-image-2-5"} <= image_ids

    voice = (await clients.get("/catalog/platforms/voice-gen")).json()
    assert {section["domain"] for section in voice["domains"]} == {"models"}
    voice_rows = [row for section in voice["domains"] for row in section["rows"]]
    assert {row["capability"] for row in voice_rows} == {
        "voice-gen.speech-2-8-hd.generate",
        "voice-gen.speech-2-8-turbo.generate",
        "voice-gen.fishaudio.s2-1-pro.generate",
    }
    voice_endpoints = [endpoint for row in voice_rows for endpoint in row["endpoints"]]
    assert {endpoint["id"] for endpoint in voice_endpoints} == {
        "minimax.voice-gen.speech-2-8-hd",
        "minimax.voice-gen.speech-2-8-turbo",
        "fishaudio.tts.s2-1-pro",
    }
    assert {endpoint["provider"] for endpoint in voice_endpoints} == {"minimax", "fishaudio"}
    catalog = cs.load()
    assert all(catalog.by_id[endpoint["id"]]["cache"] == "forbidden"
               for endpoint in voice_endpoints)

    voice_full = (await clients.get(
        "/catalog/platforms/voice-gen?include_hidden=1")).json()
    assert voice_full["hidden_count"] == 6
    action_endpoints = {
        endpoint["id"]: endpoint
        for section in voice_full["domains"]
        for row in section["rows"]
        for endpoint in row["endpoints"]
        if endpoint["kind"] == "utility"
    }
    assert set(action_endpoints) == {
        "fishaudio.voices.discover", "minimax.voice-gen.voices.list",
    }
    account_endpoints = {
        endpoint["id"]
        for section in voice_full["domains"]
        for row in section["rows"]
        for endpoint in row["endpoints"]
        if endpoint["kind"] == "account"
    }
    assert account_endpoints == {
        "fishaudio.voices.create", "fishaudio.voices.list",
        "fishaudio.voices.update", "fishaudio.voices.delete",
    }
    assert catalog.by_id["minimax.voice-gen.voices.list"]["platform_request"] == {
        "body.voice_type": "system"
    }


def test_a_missing_catalog_directory_is_an_empty_catalog_not_a_crash(tmp_path):
    cat = cs.load(directory=tmp_path / "nope")
    assert cat.endpoints == [] and cat.platforms == {}
    # a half-written dir (no taxonomy, malformed provider file) must degrade the same way
    (tmp_path / "capabilities.yaml").write_text("platforms: {tiktok: TikTok}\n")
    (tmp_path / "broken.yaml").write_text("provider: broken\nendpoints: [{no_id: true}]\n")
    assert cs.load(directory=tmp_path).endpoints == []


def test_a_proposed_capability_still_gets_a_description(tmp_path):
    (tmp_path / "capabilities.yaml").write_text("platforms: {web: Web}\ncapabilities: {}\n")
    (tmp_path / "moz.yaml").write_text(
        "provider: moz\nproposed_capabilities: {web.thing.new: A new job}\nendpoints:\n"
        "  - id: moz.web.thing.new\n    capability: web.thing.new\n    platform: web\n"
        "    method: POST\n    path: /x\n")
    assert cs.load(directory=tmp_path).capabilities["web.thing.new"] == "A new job"


# ---- stamping ----------------------------------------------------------------------------
async def test_connecting_a_key_stamps_the_catalogs_verified_endpoints(clients: AsyncClient, monkeypatch):
    """A fresh connection should arrive knowing what it can call — the catalog's verified core
    endpoints ride onto the provisioned tool's examples, capped so the list stays scannable."""
    monkeypatch.setitem(P.REGISTRY, "tikhub", dataclasses.replace(
        P.REGISTRY["tikhub"], base_url="http://upstream", probe_url="", probe_path="/whoami",
        token_verify_field=""))
    r = await clients.post("/connections/token", json={"provider": "tikhub", "token": "tk-key"})
    assert r.status_code == 200, r.text

    tool = next(t for t in (await clients.get("/tools")).json() if t["name"] == "tikhub")
    examples = tool["examples"]
    assert 0 < len(examples) <= 12
    assert all(set(e) == {"method", "path", "note"} for e in examples)
    assert len({(e["method"], e["path"]) for e in examples}) == len(examples), "no duplicate paths"

    profile = next(e for e in examples if e["path"] == "/api/v1/tiktok/web/fetch_user_profile")
    assert "tiktok.user.profile" in profile["note"], "the capability travels with the example"
    assert "uniqueId" in profile["note"], "the input hint is the part method+path can't show"

    stamped = {(e["method"], e["path"]) for e in examples}
    unverified = [e for e in cs.load().for_provider("tikhub") if not e["verified"]]
    assert unverified, "fixture assumption: tikhub has at least one unverified endpoint"
    assert not stamped & {(e["method"], e["path"]) for e in unverified}, "documented is not verified"


async def test_stamping_keeps_the_registrys_own_examples_first(clients: AsyncClient, monkeypatch):
    """The hand-written registry examples are the curated ones; the catalog appends, never displaces."""
    provider = dataclasses.replace(
        P.REGISTRY["tikhub"], base_url="http://upstream", probe_url="", probe_path="/whoami",
        token_verify_field="",
        examples=({"method": "GET", "path": "/hand/written", "note": "from the registry"},))
    monkeypatch.setitem(P.REGISTRY, "tikhub", provider)
    assert (await clients.post("/connections/token",
                               json={"provider": "tikhub", "token": "tk-key"})).status_code == 200

    tool = next(t for t in (await clients.get("/tools")).json() if t["name"] == "tikhub")
    assert tool["examples"][0]["path"] == "/hand/written"
    assert len(tool["examples"]) == 12


def test_a_provider_with_no_catalog_entry_stamps_nothing():
    assert cs.tool_examples("slack") == []
    assert json.dumps(cs.tool_examples("tikhub")), "the shape must be JSON-serializable for the Tool column"


# ---- "free" is a price, not a missing one ----------------------------------------------------
def test_a_free_route_needs_no_price_provenance():
    """`confidence` says how much we trust a NUMBER we are about to charge. A free route has no
    number, so demanding provenance refused 61 endpoints across 8 providers — 28 of Hunter's 35 —
    treating "costs nothing" as if it meant "we don't know", which is the one distinction the cost
    model is otherwise careful to keep apart."""
    from treg.domain.catalog import store as cs
    cat = cs.load()
    free = {"type": "free", "value": 0, "currency": "USD", "unit": "call"}
    ep = {"cost": free, "provider": "hunter", "scope": "any_account", "kind": "data"}
    assert cat.platform_eligible(ep)


def test_a_PAID_route_still_needs_provenance():
    """The relaxation must not leak: a route with a real price and no provenance stays refused, or
    we would bill a team using a number nobody checked."""
    from treg.domain.catalog import store as cs
    cat = cs.load()
    unchecked = {"type": "per_call", "value": 0.05, "currency": "USD", "unit": "call"}
    ep = {"cost": unchecked, "provider": "hunter", "scope": "any_account", "kind": "data"}
    assert not cat.platform_eligible(ep)

    documented = dict(unchecked, confidence="documented")
    assert cat.platform_eligible({**ep, "cost": documented})


def test_an_unpriced_route_is_still_refused():
    """The original rule, unchanged: no usd → refuse, so treg never pays a provider and charges $0."""
    from treg.domain.catalog import store as cs
    cat = cs.load()
    ep = {"cost": {"type": "per_call", "currency": "credit", "unit": "credit"},
          "provider": "pdl", "scope": "any_account", "kind": "data"}
    assert not cat.platform_eligible(ep)


# ---- shared-plan rates: a price treg SET must say so, everywhere ----------------------------

def _load_validator():
    """The actual validator module, so these tests exercise the real check rather than a copy that
    can drift from it."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "catalog_validate", Path(__file__).parent.parent / "scripts" / "catalog_validate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_shared_plan_rate_prints_its_fee_and_break_even():
    """A `kind: treg_shared_plan` entry is the catalog asserting OUR OWN price for a flat-fee
    provider. The honesty of that lives in the basis string, because the basis is what every surface
    shows as provenance: it must say whose price it is, the vendor fee, and the break-even volume.
    This runs the validator's own check against the real fx.yaml."""
    cv = _load_validator()
    errors: list[str] = []
    cv.check_fx(errors)
    assert errors == [], "\n".join(errors)


def test_the_shared_plan_check_actually_BITES(tmp_path):
    """Each dishonest shape must produce an error — checked by running the real check against bad
    entries, not by trusting that it would. (This session alone produced two tests that passed with
    their subject deleted; the lesson stuck.)"""
    cv = _load_validator()
    bad = """
credit_rates_usd:
  no_price:   {usd: null, kind: treg_shared_plan, basis: "treg shared-plan rate. $10/mo. break-even at 1,000 calls/mo", source: "x", checked: "2026-08-14"}
  no_fee:     {usd: 0.001, kind: treg_shared_plan, basis: "treg shared-plan rate. break-even at 1,000 calls/mo", source: "x", checked: "2026-08-14"}
  no_breakeven: {usd: 0.001, kind: treg_shared_plan, basis: "treg shared-plan rate. Vendor sells flat $10/mo", source: "x", checked: "2026-08-14"}
  wrong_start: {usd: 0.001, kind: treg_shared_plan, basis: "Vendor sells flat $10/mo; break-even at 1,000 calls/mo", source: "x", checked: "2026-08-14"}
  typo_kind:  {usd: 0.001, kind: treg_shared_pla, basis: "whatever", source: "x", checked: "2026-08-14"}
  prose_only: {usd: 0.001, basis: "treg shared-plan rate without the marker", source: "x", checked: "2026-08-14"}
  fine_vendor: {usd: 0.02, basis: "Starter $49/mo / 2,000 credits", source: "x", checked: "2026-08-14"}
"""
    (tmp_path / "fx.yaml").write_text(bad)
    original = cv.CATALOG
    try:
        cv.CATALOG = tmp_path
        errors: list[str] = []
        cv.check_fx(errors)
    finally:
        cv.CATALOG = original
    blob = "\n".join(errors)
    for name in ("no_price", "no_fee", "no_breakeven", "wrong_start", "typo_kind", "prose_only"):
        assert name in blob, f"{name} should have been refused:\n{blob}"
    assert "fine_vendor" not in blob, "an ordinary vendor rate must pass untouched"


def test_a_shared_plan_rate_converts_like_any_credit():
    """The whole design: a flat-fee provider is modelled as a credit provider whose credit is one
    call on treg's shared plan. cost_view needs ZERO changes — this asserts the pilot entry flows
    through the existing conversion."""
    from treg.domain.catalog import store as cs

    c = cs.load()
    out = c.cost_view({"type": "per_call", "value": 1, "currency": "credit"}, "alphavantage")
    assert out["usd"] == 0.001


def test_the_trial_kind_check_actually_BITES(tmp_path):
    """Every dishonest treg_trial shape refused by the REAL check: a non-zero 'trial', a zero with
    no allowance (the congestion-control gap), a basis that does not say whose $0 it is, and prose
    claiming a trial without the marker."""
    cv = _load_validator()
    bad = """
credit_rates_usd:
  nonzero:  {usd: 0.001, kind: treg_trial, trial_calls_per_team_day: 20, basis: "treg trial rate. x", source: "x", checked: "2026-08-15"}
  no_cap:   {usd: 0, kind: treg_trial, basis: "treg trial rate. x", source: "x", checked: "2026-08-15"}
  bad_cap:  {usd: 0, kind: treg_trial, trial_calls_per_team_day: 0, basis: "treg trial rate. x", source: "x", checked: "2026-08-15"}
  wrong_basis: {usd: 0, kind: treg_trial, trial_calls_per_team_day: 20, basis: "free on our key", source: "x", checked: "2026-08-15"}
  fine_trial: {usd: 0, kind: treg_trial, trial_calls_per_team_day: 20, basis: "treg trial rate. Served at $0 on treg's free key", source: "x", checked: "2026-08-15"}
"""
    (tmp_path / "fx.yaml").write_text(bad)
    original = cv.CATALOG
    try:
        cv.CATALOG = tmp_path
        errors: list[str] = []
        cv.check_fx(errors)
    finally:
        cv.CATALOG = original
    blob = "\n".join(errors)
    for name in ("nonzero", "no_cap", "bad_cap", "wrong_basis"):
        assert name in blob, f"{name} should have been refused:\n{blob}"
    assert "fine_trial" not in blob, "a compliant trial entry must pass"


def test_trial_pools_flow_from_fx_to_eligibility_and_display():
    """The real file's three pools, end to end through the loader: a $0 price that is platform
    eligible AND carries its allowance wherever the cost is shown — a bare $0.00 would read as
    unlimited."""
    from treg.domain.catalog import store as cs

    c = cs.load()
    assert c.trial_pools == {"finnhub": 50, "twelvedata": 20, "tiingo": 20,
                             "getleadsio": 5}
    ep = c.by_id["finnhub.quote"]
    assert c.platform_eligible(ep)
    cost = c.cost_view(ep["cost"], "finnhub")
    assert cost["usd"] == 0.0 and cost["trial_calls_per_team_day"] == 50
    # and the two NON-trial own-key providers stay ineligible — the license boundary holds
    assert not c.platform_eligible(c.by_id["polygon.prev-close"])
    assert not c.platform_eligible(c.by_id["eodhd.eod"])


# ---- method metadata: an endpoint treg enforces the wrong verb on cannot be called at all -------
def test_no_tikhub_ads_route_is_recorded_as_a_GET():
    """treg enforces the recorded method, so a wrong one is not a cosmetic error — it makes the
    endpoint uncallable from every direction at once: POST is refused here ("… is GET"), and GET is
    refused upstream (405). All twelve `/api/v1/tiktok/ads/*` routes shipped that way, because the
    ingester's OPTIONS probe answers with a list and its preference order picked GET."""
    cat = cs.load()
    ads = [e for e in cat.endpoints if e["path"].startswith("/api/v1/tiktok/ads/")]
    assert len(ads) >= 12
    assert [e["id"] for e in ads if e["method"] != "POST"] == []


def test_an_endpoint_whose_input_is_a_body_does_not_advertise_query_params():
    """The verb and the parameter position are one decision. Correcting the method and leaving the
    params under `queryParams` would hand callers a POST with its arguments in the wrong half of the
    request — still uncallable, just for a new reason."""
    cat = cs.load()
    ep = cat.by_id["tikhub.x.tiktok-ads-search-ads"]
    assert ep["method"] == "POST"
    assert "queryParams" not in (ep.get("input") or {})
    assert "keyword" in ((ep.get("input") or {}).get("body") or {})


# ---- an id that misses -------------------------------------------------------------------------
async def test_an_unknown_id_names_the_ids_it_nearly_matched(clients: AsyncClient):
    """An id is not free text. An agent holding one that misses by a segment has hit a dead end
    mid-plan, and the usual next move is to invent another and fail again."""
    r = await clients.get("/catalog/endpoints/lusha.companies-signals")
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["did_you_mean"] == ["lusha.x.companies-signals"]
    assert "lusha.x.companies-signals" in detail["hint"]


async def test_an_id_that_resembles_nothing_is_sent_to_search(clients: AsyncClient):
    """No near miss must not become a WRONG suggestion — a confidently wrong id is worse than none."""
    r = await clients.get("/catalog/endpoints/acme.does-not-exist")
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["did_you_mean"] == []
    assert "catalog search" in detail["hint"]


# ---- the GENERATOR, not just its output ---------------------------------------------------------
def test_instagram_ingester_omits_page_token_messaging_routes():
    """The next re-ingest must not restore the invalid Instagram-id messaging routes."""
    import sys
    sys.path.insert(0, "scripts")
    from catalog_ingest import INSTAGRAM_EDGES

    routes = {(method, path) for _, method, path, _, _ in INSTAGRAM_EDGES}
    assert ("GET", "/{ig_user_id}/conversations") not in routes
    assert ("POST", "/{ig_user_id}/messages") not in routes


def test_the_ingester_puts_a_POST_routes_arguments_in_the_BODY():
    """The checked-in YAML is machine-generated, so a fix that lives only in the file is undone by
    the next `catalog_ingest.py` run. These assert the GENERATOR: the tests above inspect the
    corrected YAML and would pass with the ingester reverted.

    The two decisions have to come from one document. TikHub's Apifox docs list every TikTok-Ads
    parameter under `parameters.query` while its OpenAPI declares the same route POST-with-a-JSON
    body, so taking the verb from one and the position from the other produced a POST carrying its
    arguments in the query string — uncallable, just differently."""
    import sys
    sys.path.insert(0, "scripts")
    from catalog_ingest import tikhub_input_and_test

    doc_op = {"parameters": {"query": [
        {"name": "keyword", "type": "string", "required": True, "sampleValue": "shoes"},
        {"name": "limit", "type": "integer", "required": False, "sampleValue": 20},
    ]}}
    spec_op = {"post": {"requestBody": {"content": {"application/json": {"schema": {}}}}},
               "get": {}}

    inp, test, reason = tikhub_input_and_test(doc_op, {}, method="POST", spec_op=spec_op)
    assert not reason
    assert "queryParams" not in inp and "keyword" in inp["body"], inp
    assert inp["bodyType"] == "json"
    assert "queryParams" not in test and test["body"]["keyword"] == "shoes", test

    # a GET route is untouched — the rule keys off the spec's own body declaration, not the verb alone
    as_get, get_test, _ = tikhub_input_and_test(doc_op, {}, method="GET", spec_op=spec_op)
    assert "queryParams" in as_get and "body" not in as_get
    assert "queryParams" in get_test

    # …and a POST whose spec declares NO json body keeps its query string
    no_body, _, _ = tikhub_input_and_test(doc_op, {}, method="POST", spec_op={"post": {}})
    assert "queryParams" in no_body and "body" not in no_body


def test_gtm_ingestion_expands_semantic_resource_names_into_atomic_path_ids():
    """The checked-in extended YAML must stay fixed after the next Discovery re-ingest."""
    import sys
    sys.path.insert(0, "scripts")
    from catalog_ingest import google_flat_path_params

    entry = {
        "path": "/tagmanager/v2/accounts/{accountsId}/containers/{containersId}/workspaces",
        "input": {
            "pathParams": {
                "parent": {"type": "string", "required": True, "note": "container resource path"},
            },
            "queryParams": {"pageToken": {"type": "string", "required": False}},
        },
    }
    assert google_flat_path_params(entry) is entry
    params = entry["input"]["pathParams"]
    assert list(params) == ["accountsId", "containersId"]
    assert all(spec["required"] for spec in params.values())
    assert "pageToken" in entry["input"]["queryParams"]


def test_a_published_spec_outranks_the_OPTIONS_probe():
    """The probe infers a verb from a preflight; the spec is the provider's own contract. When the
    spec names exactly one method the spec wins, so a re-ingest inherits an upstream verb change
    instead of re-deriving a stale guess."""
    import sys
    sys.path.insert(0, "scripts")
    from catalog_ingest import resolve_method

    # the case that matters: spec says POST, the probe came back GET
    assert resolve_method(spec_op={"post": {}}, probed="GET", documented="GET") == "POST"
    # ambiguous spec (two verbs) → the probe is still the tiebreaker it always was
    assert resolve_method(spec_op={"get": {}, "post": {}}, probed="POST") == "POST"
    # no spec at all → probe, then docs, then GET
    assert resolve_method(spec_op={}, probed="POST") == "POST"
    assert resolve_method(spec_op={}, probed=None, documented="delete") == "DELETE"
    assert resolve_method(spec_op={}, probed=None) == "GET"


def test_a_stored_EMPTY_json_body_survives_into_the_call_template():
    """`--data '{}'` is not noise: these are POSTs that take no arguments but still require a JSON
    body, and `if body:` dropped it — printing a command that differs from the one that was tested,
    against handlers that reject an empty body outright."""
    cat = cs.load()
    empties = [e for e in cat.endpoints if (e.get("test_request") or {}).get("body") == {}]
    assert empties, "the fixture for this rule is the catalog itself; it must not be empty"
    for ep in empties:
        line = cs.call_template(ep)
        assert "--data '{}'" in line, f"{ep['id']}: {line}"
    # …and a GET is not handed a body it never had
    gets = [e for e in cat.endpoints if e["method"] == "GET"]
    assert not any("--data" in cs.call_template(e) for e in gets)


async def test_search_pulls_the_routed_parent_in_when_a_child_matches(clients: AsyncClient):
    """`leadsforge email` matches leadsforge.* on the provider's NAME; the routed row for that job
    carries no such word, yet it is the row to show first — so a matched child brings its parent
    along. And `find leads` lands on the lead-search job itself (people.search says "leads")."""
    rows = (await clients.get("/catalog/search", params={"q": "leadsforge email"})).json()["results"]
    ids = [r["id"] for r in rows]
    assert "treg.people.email.find" in ids, ids
    assert ids.index("treg.people.email.find") < ids.index("leadsforge.people.email.find")
    rows = (await clients.get("/catalog/search", params={"q": "find leads"})).json()["results"]
    assert rows[0]["id"] == "treg.people.search", [r["id"] for r in rows[:3]]


async def test_search_caps_a_routed_group_at_a_few_children(clients: AsyncClient):
    """A search page is a list of JOBS: one capability's two dozen providers must not eat the
    budget. The parent says how many were cut; `catalog get` ranks them all."""
    rows = (await clients.get("/catalog/search", params={"q": "find leads"})).json()["results"]
    parent = next(r for r in rows if r["id"] == "treg.people.search")
    kids = [r for r in rows if r["capability"] == "people.search" and r.get("kind") != "routed"]
    assert len(kids) <= 5 and parent["children_hidden"] >= 1
    assert "treg.people.email.find" in {r["id"] for r in rows}, "the next job fits on the page now"


async def test_enrichment_catalog_prices_and_routed_child_rates(clients):
    from treg.domain.catalog import store
    cat = store.load()
    endpoints = [e for e in cat.by_id.values() if e['provider'] == 'quickenrich']
    assert len(endpoints) == 11
    assert sum(e['cost']['type'] == 'free' for e in endpoints) == 6
    for ep in endpoints:
        cost = cat.cost_view(ep['cost'], ep['provider'])
        assert cost['usd'] == (0 if cost['type'] == 'free' else 0.004834)
    for cap in ('people.email.find', 'people.phone.find', 'people.enrich', 'people.search', 'companies.search'):
        response = await clients.get('/catalog/endpoints/treg.' + cap)
        assert response.status_code == 200
        doc = response.json()
        children = [c for c in doc['routing']['plan'] if c['endpoint_id'].startswith('quickenrich.')]
        assert children
        for child in children:
            ep = cat.by_id[child['endpoint_id']]
            assert child['usd'] == cat.cost_view(ep['cost'], ep['provider'])['usd']


def test_generic_display_prices_match_web_and_cli():
    from treg.domain.catalog import store
    from treg.routers.web import _price_label
    from treg.cli import _cost_usd, _cost_label
    cat = store.load()
    for quantity, rate in [(25, 2.0), (100, 1.0)]:
        raw = {'type': 'per_result', 'currency': 'USD', 'value': rate, 'per': quantity,
               'display': {'unit': 'records', 'grouped': True, 'round_up': True}}
        cost = cat.cost_view(raw, 'any-provider')
        expected = f'${rate:g}/started {quantity} records'
        assert cost['usd'] == rate / quantity
        assert _price_label(cost) == _cost_usd(cost) == _cost_label(cost) == expected
    cost = cat.cost_view({'type': 'per_result', 'currency': 'USD', 'value': 2,
                         'display': {'unit': 'item', 'variable': True}}, 'another-provider')
    assert _price_label(cost) == _cost_usd(cost) == _cost_label(cost) == '$2+/item'
    maximum = cat.cost_view({'type': 'per_call', 'currency': 'USD', 'value': 0.064,
                             'display': {'unit': 'call', 'maximum': True}}, 'another-provider')
    assert _price_label(maximum) == _cost_usd(maximum) == _cost_label(maximum) == 'up to $0.064/call'


def test_hunter_domain_search_advertises_one_search_credit():
    """Feedback #201: live Domain Search bills 1 SEARCH credit (~$0.0245) even for one email.

    `value`/`per`/`note` stay 1 credit per 10 emails — `usd` is still that linear slice so
    reserve can scale with `limit`. catalog_get / usd_per_call must quote the whole credit,
    which is what `display.grouped` + `advertised_usd` do. Settlement is unchanged.
    """
    cat = cs.load()
    raw = cat.by_id["hunter.companies.emails"]["cost"]
    assert (raw["value"], raw["per"], raw["unit"]) == (1, 10, "record")
    cost = cat.cost_view(raw, "hunter")
    assert cost["usd"] == 0.00245
    assert cost["display_usd"] == 0.0245
    assert cost["display_unit"] == "started 10 emails"
    assert cat.advertised_usd(cost) == 0.0245
    # Sibling Finder and Multi-Domain reveal already quote one full search credit.
    find = cat.cost_view(cat.by_id["hunter.people.email.find"]["cost"], "hunter")
    assert find["usd"] == 0.0245 and cat.advertised_usd(find) == 0.0245
    reveal = cat.cost_view(cat.by_id["hunter.x.multi-domain-search-reveal"]["cost"], "hunter")
    assert reveal["usd"] == 0.0245 and cat.advertised_usd(reveal) == 0.0245


def test_dataforseo_related_keywords_does_not_advertise_order_by():
    """Feedback #54 / #439: live related_keywords/live rejects order_by and
    filters with 40501.

    Vendor docs still list both fields; the live API does not. catalog_get must
    not offer them on this id. ranked_keywords (a sibling Labs route) still
    sorts and filters.
    """
    cat = cs.load()
    ideas = cat.by_id["dataforseo.google.keywords.ideas"]
    assert ideas["path"] == "/dataforseo_labs/google/related_keywords/live"
    body = ideas["input"]["body"]
    assert "order_by" not in body
    assert "filters" not in body
    note = ideas["input"]["note"]
    assert "order_by" in note
    assert "filters" in note
    ranked = cat.by_id["dataforseo.google.domain.ranked_keywords"]
    assert "order_by" in ranked["input"]["body"]
    assert "filters" in ranked["input"]["body"]
    for task in ideas["test_request"]["body"]:
        assert "order_by" not in task
        assert "filters" not in task


async def test_catalog_get_dataforseo_related_keywords_omits_order_by(clients: AsyncClient):
    body = (await clients.get("/catalog/endpoints/dataforseo.google.keywords.ideas")).json()
    assert "order_by" not in body["endpoint"]["input"]["body"]
    assert "filters" not in body["endpoint"]["input"]["body"]
    note = body["endpoint"]["input"]["note"]
    assert "order_by" in note
    assert "filters" in note


RANKED_KEYWORDS_ID = "dataforseo.google.domain.ranked_keywords"


def test_dataforseo_ranked_keywords_names_labs_location_language_pairs():
    """Feedback #300: ranked_keywords location+language must be a Labs pair.

    catalog_get used to say only `2840 = United States` / `one of language_code
    | language_name` with example `en`, so agents sent `location_code: 2076`
    (Brazil, often misread as Morocco) with `language_code: fr` and got
    Invalid Field language_code. Settlement is unchanged.
    """
    cat = cs.load()
    ep = cat.by_id[RANKED_KEYWORDS_ID]
    assert ep["path"] == "/dataforseo_labs/google/ranked_keywords/live"
    loc = ep["input"]["body"]["location_code"]
    lang = ep["input"]["body"]["language_code"]
    loc_note = loc["note"].lower()
    lang_note = lang["note"].lower()
    input_note = ep["input"]["note"].lower()
    assert "2840" in loc_note and "united states" in loc_note
    assert "2076" in loc_note and "brazil" in loc_note
    assert "morocco" in loc_note and "2504" in loc_note
    assert "locations_and_languages" in loc_note
    assert loc["example"] == 2840
    assert "invalid field" in lang_note
    assert "2076" in lang_note and "fr" in lang_note
    assert "brazil" in lang_note and "pt" in lang_note
    assert "2504" in lang_note and "morocco" in lang_note
    assert "locations_and_languages" in lang_note
    assert lang["example"] == "en"
    assert "locations_and_languages" in input_note
    assert "https://docs.dataforseo.com/v3/dataforseo_labs/locations_and_languages/" in ep["input"]["note"]
    task = ep["test_request"]["body"][0]
    assert task["location_code"] == 2840
    assert task["language_code"] == "en"


async def test_catalog_get_dataforseo_ranked_keywords_names_labs_location_language_pairs(
        clients: AsyncClient):
    """Feedback #300: catalog_get must name Brazil 2076 vs Morocco 2504 pairs."""
    body = (await clients.get(f"/catalog/endpoints/{RANKED_KEYWORDS_ID}")).json()
    loc = body["endpoint"]["input"]["body"]["location_code"]
    lang = body["endpoint"]["input"]["body"]["language_code"]
    loc_note = loc["note"].lower()
    lang_note = lang["note"].lower()
    input_note = body["endpoint"]["input"]["note"]
    assert "2840" in loc_note and "united states" in loc_note
    assert "2076" in loc_note and "brazil" in loc_note
    assert "2504" in loc_note and "morocco" in loc_note
    assert "locations_and_languages" in loc_note
    assert loc["example"] == 2840
    assert "invalid field" in lang_note
    assert "2076" in lang_note and "fr" in lang_note
    assert "2504" in lang_note
    assert "locations_and_languages" in lang_note
    assert lang["example"] == "en"
    assert "https://docs.dataforseo.com/v3/dataforseo_labs/locations_and_languages/" in input_note


async def test_catalog_get_dataforseo_maps_live_omits_location_name(clients: AsyncClient):
    body = (await clients.get(
        "/catalog/endpoints/dataforseo.x.serp-google-maps-live-advanced"
    )).json()
    fields = body["endpoint"]["input"]["body"]
    assert "location_name" not in fields
    assert "location_code" in fields
    assert "location_coordinate" in fields
    note = body["endpoint"]["input"]["note"]
    assert "location_name" in note
    assert "40501" in note


def test_dataforseo_backlinks_summary_is_single_task():
    """Feedback #102 / #103: backlinks/summary/live accepts exactly one task.

    catalog_get used to reuse the generic "array of task objects" wording (and
    the provider-level "up to 100 tasks" limit), so agents batched domains and
    got per-task 40000 "You can set only one task at a time" on the rest.
    Vendor docs: each Live API call can contain only one task. Multi-target
    work is dataforseo.web.url.metrics (bulk_ranks/live, many targets / one task).
    """
    cat = cs.load()
    ep = cat.by_id["dataforseo.web.backlinks.summary"]
    assert ep["path"] == "/backlinks/summary/live"
    note = ep["input"]["note"]
    assert "exactly one task" in note
    assert "40000" in note
    assert "dataforseo.web.url.metrics" in note
    tasks = ep["test_request"]["body"]
    assert isinstance(tasks, list) and len(tasks) == 1
    limits = cat.provider_meta["dataforseo"]["limits"]
    assert "exactly one task" in limits
    assert "up to 100 tasks per POST array" not in limits


async def test_catalog_get_dataforseo_backlinks_summary_names_the_single_task_limit(
        clients: AsyncClient):
    body = (await clients.get("/catalog/endpoints/dataforseo.web.backlinks.summary")).json()
    note = body["endpoint"]["input"]["note"]
    assert "exactly one task" in note
    assert "40000" in note
    assert "dataforseo.web.url.metrics" in note
    assert "up to 100 tasks per POST array" not in body["provider"]["limits"]
    assert "exactly one task" in body["provider"]["limits"]
    tmpl = body["call_template"]
    assert tmpl.startswith("treg call dataforseo.web.backlinks.summary --method POST")
    assert "--data '[{\"target\":\"moz.com\"" in tmpl


async def test_catalog_get_dataforseo_ai_mode_live_names_the_single_task_limit(
        clients: AsyncClient):
    """Feedback #94: catalog_get must not advertise multi-task batching on this Live route."""
    body = (await clients.get(
        "/catalog/endpoints/dataforseo.x.serp-google-ai-mode-live-advanced")).json()
    note = body["endpoint"]["input"]["note"]
    assert "exactly one task" in note.lower() or "exactly 1 task" in note.lower()
    assert "40000" in note
    assert "one object per task" not in note.lower()
    tmpl = body["call_template"]
    assert tmpl.startswith(
        "treg call dataforseo.x.serp-google-ai-mode-live-advanced --method POST")


async def test_catalog_get_dataforseo_claude_llm_responses_live_names_working_model(
        clients: AsyncClient):
    """Feedback #358: catalog_get must not advertise claude-opus-4-0 or multi-task batching."""
    body = (await clients.get(
        "/catalog/endpoints/dataforseo.x.ai-optimization-claude-llm-responses-live")).json()
    fields = body["endpoint"]["input"]["body"]
    assert fields["model_name"]["example"] == "claude-sonnet-4-5"
    model_note = fields["model_name"]["note"]
    assert "40501" in model_note or "llm_responses/models" in model_note
    note = body["endpoint"]["input"]["note"]
    assert "exactly one task" in note.lower() or "exactly 1 task" in note.lower()
    assert "40000" in note
    assert "one object per task" not in note.lower()
    example = body.get("example_response")
    if isinstance(example, dict):
        tasks = example.get("tasks") or []
        assert not tasks or tasks[0].get("status_code") != 40501, (
            "catalog_get must not advertise the 40501 Invalid Field failure as the example"
        )


async def test_catalog_get_dataforseo_page_audit_names_browser_preset_dependency(
        clients: AsyncClient):
    """Feedback #234 / #235: catalog_get must not advertise browser_preset alone."""
    body = (await clients.get("/catalog/endpoints/dataforseo.web.page.audit")).json()
    fields = body["endpoint"]["input"]["body"]
    assert "enable_browser_rendering=true" in fields["browser_preset"]["note"]
    assert "40501" in fields["browser_preset"]["note"]
    assert "browser_preset" in fields["enable_browser_rendering"]["note"]
    assert "browser_preset" in body["endpoint"]["input"]["note"]
    assert "enable_browser_rendering" in body["endpoint"]["input"]["note"]


async def test_catalog_get_hunter_domain_search_quotes_the_credit(clients: AsyncClient):
    body = (await clients.get("/catalog/endpoints/hunter.companies.emails")).json()
    cost = body["endpoint"]["cost"]
    assert cost["usd"] == 0.00245, "reserve unit stays the per-record slice"
    assert cost["display_usd"] == 0.0245
    assert cost["display_unit"] == "started 10 emails"
    search = (await clients.get("/catalog/search", params={"q": "hunter domain search emails", "limit": 50})).json()
    row = next(r for r in search["results"] if r["id"] == "hunter.companies.emails")
    assert row["cost"]["display_usd"] == 0.0245
    assert row["cost"]["usd"] == 0.00245


INSTAGRAM_REELS_SEARCH_ID = "scrapecreators.x.v2-instagram-reels-search"
INSTAGRAM_REELS_DATE_POSTED = ["last-week", "last-month", "last-year"]
LINKEDIN_SEARCH_POSTS_ID = "scrapecreators.x.v1-linkedin-search-posts"
LINKEDIN_SEARCH_POSTS_DATE_POSTED = [
    "last-hour", "last-day", "last-week", "last-month", "last-year",
]


def test_scrapecreators_instagram_reels_search_date_posted_enum():
    """Feedback #381: GET /v2/instagram/reels/search only accepts week/month/year windows.

    catalog_get used to advertise example last-hour (Google's generic date_posted
    set). Upstream OpenAPI enum is last-week | last-month | last-year; hour/day
    windows are unsupported because Google does not index Instagram reels
    reliably there. Sibling scrapecreators date_posted fields keep their own
    windows. Settlement is unchanged.

    Ref: https://docs.scrapecreators.com/openapi.json
    """
    cat = cs.load()
    ep = cat.by_id[INSTAGRAM_REELS_SEARCH_ID]
    assert ep["path"] == "/v2/instagram/reels/search"
    field = ep["input"]["queryParams"]["date_posted"]
    assert field["enum"] == INSTAGRAM_REELS_DATE_POSTED
    assert field["example"] == "last-week"
    note = field["note"].lower()
    assert "hour" in note and "day" in note
    assert "not supported" in note or "unsupported" in note

    google = cat.by_id["scrapecreators.x.v1-google-search"]["input"]["queryParams"]["date_posted"]
    assert google.get("enum") is None
    assert google["example"] == "last-hour"
    linkedin = cat.by_id[LINKEDIN_SEARCH_POSTS_ID]["input"]["queryParams"]["date_posted"]
    assert linkedin["enum"] == LINKEDIN_SEARCH_POSTS_DATE_POSTED
    assert linkedin["enum"] != INSTAGRAM_REELS_DATE_POSTED


async def test_catalog_get_scrapecreators_instagram_reels_search_date_posted(
        clients: AsyncClient):
    """Feedback #381: catalog_get must not advertise last-hour on this reels search."""
    body = (await clients.get(f"/catalog/endpoints/{INSTAGRAM_REELS_SEARCH_ID}")).json()
    field = body["endpoint"]["input"]["queryParams"]["date_posted"]
    assert field["enum"] == INSTAGRAM_REELS_DATE_POSTED
    assert field["example"] == "last-week"
    note = field["note"].lower()
    assert "hour" in note and "day" in note
    assert "not supported" in note or "unsupported" in note


def test_scrapecreators_linkedin_search_posts_date_posted_enum():
    """Feedback #121: GET /v1/linkedin/search/posts only accepts last-* windows.

    catalog_get used to advertise date_posted as a free string (example last-week)
    with no enum, so agents sent past-week / past-day (Google-style) and the
    provider rejected them. Upstream OpenAPI enum is last-hour | last-day |
    last-week | last-month | last-year. Instagram reels search keeps its own
    three-value window. Settlement is unchanged.

    Ref: https://docs.scrapecreators.com/openapi.json
    """
    cat = cs.load()
    ep = cat.by_id[LINKEDIN_SEARCH_POSTS_ID]
    assert ep["path"] == "/v1/linkedin/search/posts"
    field = ep["input"]["queryParams"]["date_posted"]
    assert field["enum"] == LINKEDIN_SEARCH_POSTS_DATE_POSTED
    assert field["example"] == "last-week"
    note = field["note"].lower()
    assert "last-hour" in note and "last-day" in note
    assert "last-week" in note and "last-month" in note and "last-year" in note
    assert "past-week" in note and "past-day" in note
    assert "not accepted" in note
    assert ep["cost"]["value"] == 1
    assert ep["cost"]["currency"] == "credit"

    reels = cat.by_id[INSTAGRAM_REELS_SEARCH_ID]["input"]["queryParams"]["date_posted"]
    assert reels["enum"] == INSTAGRAM_REELS_DATE_POSTED


async def test_catalog_get_scrapecreators_linkedin_search_posts_date_posted(
        clients: AsyncClient):
    """Feedback #121: catalog_get must name last-* and warn against past-*."""
    body = (await clients.get(f"/catalog/endpoints/{LINKEDIN_SEARCH_POSTS_ID}")).json()
    field = body["endpoint"]["input"]["queryParams"]["date_posted"]
    assert field["enum"] == LINKEDIN_SEARCH_POSTS_DATE_POSTED
    assert field["example"] == "last-week"
    note = field["note"].lower()
    assert "last-hour" in note and "last-week" in note
    assert "past-week" in note and "past-day" in note
    assert "not accepted" in note


TIKTOK_SEARCH_VIDEOS_ID = "scrapecreators.tiktok.search.videos"
TIKTOK_SEARCH_VIDEOS_DATE_POSTED = [
    "yesterday", "this-week", "this-month", "last-3-months", "last-6-months", "all-time",
]
TIKTOK_SEARCH_VIDEOS_SORT_BY = ["relevance", "most-liked", "date-posted"]


def test_scrapecreators_tiktok_search_videos_query_params_match_openapi():
    """Feedback #430: GET /v1/tiktok/search/keyword exposes the current OpenAPI params.

    catalog_get used to advertise only query + date_posted (no enum). Upstream
    OpenAPI also has sort_by, region (proxy placement, not a region filter),
    cursor, and trim. Settlement, path, capability, and adapters are unchanged.

    Ref: https://docs.scrapecreators.com/openapi.json
    """
    cat = cs.load()
    ep = cat.by_id[TIKTOK_SEARCH_VIDEOS_ID]
    assert ep["path"] == "/v1/tiktok/search/keyword"
    params = ep["input"]["queryParams"]
    assert set(params) == {
        "query", "date_posted", "sort_by", "region", "cursor", "trim",
    }
    assert params["query"]["required"] is True
    date_posted = params["date_posted"]
    assert date_posted["required"] is False
    assert date_posted["enum"] == TIKTOK_SEARCH_VIDEOS_DATE_POSTED
    assert date_posted["example"] == "all-time"
    sort_by = params["sort_by"]
    assert sort_by["required"] is False
    assert sort_by["enum"] == TIKTOK_SEARCH_VIDEOS_SORT_BY
    assert sort_by["example"] == "relevance"
    assert sort_by["note"].lower() == "sort by"
    region_note = params["region"]["note"].lower()
    assert "does not filter" in region_note or "doesn't filter" in region_note
    assert "proxy" in region_note
    assert params["cursor"]["type"] == "number"
    assert params["cursor"]["example"] == 10
    assert "cursor" in params["cursor"]["note"].lower()
    assert params["trim"]["type"] == "boolean"
    assert "trim" in params["trim"]["note"].lower()
    assert ep["cost"]["value"] == 1
    assert ep["cost"]["currency"] == "credit"


async def test_catalog_get_scrapecreators_tiktok_search_videos_query_params(
        clients: AsyncClient):
    """Feedback #430: catalog_get must name keyword-search sort/region/cursor/trim."""
    body = (await clients.get(f"/catalog/endpoints/{TIKTOK_SEARCH_VIDEOS_ID}")).json()
    params = body["endpoint"]["input"]["queryParams"]
    assert params["date_posted"]["enum"] == TIKTOK_SEARCH_VIDEOS_DATE_POSTED
    assert params["sort_by"]["enum"] == TIKTOK_SEARCH_VIDEOS_SORT_BY
    assert "proxy" in params["region"]["note"].lower()
    assert params["cursor"]["type"] == "number"
    assert params["trim"]["type"] == "boolean"


FACEBOOK_ADLIBRARY_SEARCH_ADS_ID = "scrapecreators.x.v1-facebook-adlibrary-search-ads"
FACEBOOK_ADLIBRARY_AD_ID = "scrapecreators.x.v1-facebook-adlibrary-ad"
FACEBOOK_ADLIBRARY_SEARCH_ADS_SORT_BY = [
    "total_impressions", "relevancy_monthly_grouped",
]


def test_scrapecreators_facebook_adlibrary_search_ads_sort_by_order_only():
    """Feedback #658: sort_by ranks results; it is not verified performance.

    catalog_get used to say "Sort by impressions (high to low)" with example
    total_impressions, so agents read spend / impressions_text /
    impressions_index as measurable ranking. Observed Meta Ad Library
    commercial search rows often have null spend, null impressions_text, and
    impressions_index=-1. Search collation_count can be null on the sibling
    detail endpoint. Catalog-only: sort_by.note is order-only and names the
    OpenAPI enum; input.note warns that collation_count is not a
    creative-variant or budget count. Settlement is unchanged.

    Ref: https://docs.scrapecreators.com/openapi.json
    """
    cat = cs.load()
    ep = cat.by_id[FACEBOOK_ADLIBRARY_SEARCH_ADS_ID]
    assert ep["path"] == "/v1/facebook/adLibrary/search/ads"
    field = ep["input"]["queryParams"]["sort_by"]
    assert field["enum"] == FACEBOOK_ADLIBRARY_SEARCH_ADS_SORT_BY
    assert field["example"] == "total_impressions"
    note = field["note"].lower()
    assert "total_impressions" in note and "relevancy_monthly_grouped" in note
    assert "order" in note
    assert "spend" in note and "impressions_text" in note
    assert "impressions_index" in note
    assert "null" in note and "-1" in note
    assert "verified" in note or "performance" in note
    input_note = ep["input"]["note"].lower()
    assert "order-only" in input_note or "order only" in input_note
    assert "collation_count" in input_note
    assert FACEBOOK_ADLIBRARY_AD_ID in ep["input"]["note"]
    assert "search" in input_note and "detail" in input_note
    assert "creative" in input_note or "budget" in input_note
    assert ep["cost"]["value"] == 1
    assert ep["cost"]["currency"] == "credit"


async def test_catalog_get_scrapecreators_facebook_adlibrary_search_ads_sort_by(
        clients: AsyncClient):
    """Feedback #658: catalog_get must warn that sort_by is order-only."""
    body = (await clients.get(
        f"/catalog/endpoints/{FACEBOOK_ADLIBRARY_SEARCH_ADS_ID}")).json()
    field = body["endpoint"]["input"]["queryParams"]["sort_by"]
    assert field["enum"] == FACEBOOK_ADLIBRARY_SEARCH_ADS_SORT_BY
    assert field["example"] == "total_impressions"
    note = field["note"].lower()
    assert "order" in note
    assert "spend" in note and "impressions_text" in note
    assert "impressions_index" in note
    assert "null" in note
    input_note = body["endpoint"]["input"]["note"].lower()
    assert "collation_count" in input_note
    assert FACEBOOK_ADLIBRARY_AD_ID in body["endpoint"]["input"]["note"]
    assert "search" in input_note and "detail" in input_note


REDDIT_SEARCH_POSTS_ID = "scrapecreators.reddit.search.posts"
REDDIT_SEARCH_POSTS_SORT = ["relevance", "new", "top", "comment_count"]
TIKHUB_REDDIT_SEARCH_ID = "tikhub.x.reddit-app-fetch-dynamic-search"


def test_scrapecreators_reddit_search_posts_sort_enum():
    """Feedback #507 / #461: GET /v1/reddit/search sort=new is chronological, not 'about X'.

    catalog_get used to advertise sort as a free string (example relevance)
    with note 'Sort by', so agents sent sort=new expecting recent posts about
    the query and got newest sitewide posts weakly related or unrelated.
    Sibling #461: query=Betterment + sort=new matched colloquial 'better'.
    Upstream OpenAPI enum is relevance | new | top | comment_count. Catalog-only:
    sort names that enum and warns to prefer relevance; input.note repeats the
    caveat. Settlement is unchanged.

    Ref: https://docs.scrapecreators.com/openapi.json
    """
    cat = cs.load()
    ep = cat.by_id[REDDIT_SEARCH_POSTS_ID]
    assert ep["path"] == "/v1/reddit/search"
    field = ep["input"]["queryParams"]["sort"]
    assert field["enum"] == REDDIT_SEARCH_POSTS_SORT
    assert field["example"] == "relevance"
    note = field["note"].lower()
    assert "relevance" in note
    assert "new" in note
    assert "chronological" in note or "newest-first" in note or "newest first" in note
    assert "weak" in note or "unrelated" in note
    input_note = ep["input"]["note"].lower()
    assert "relevance" in input_note
    assert "new" in input_note
    assert "chronological" in input_note or "newest" in input_note
    assert "weak" in input_note or "unrelated" in input_note
    params = ep["input"]["queryParams"]
    assert params["filter"]["enum"] == ["posts", "comments"]
    assert params["timeframe"]["enum"] == ["all", "day", "week", "month", "year"]
    assert "after" in params
    assert params["trim"]["type"] == "boolean"
    assert (ep.get("test_request") or {}).get("queryParams", {}).get("sort") == "relevance"
    assert ep["cost"]["value"] == 1
    assert ep["cost"]["currency"] == "credit"

    tikhub = cat.by_id[TIKHUB_REDDIT_SEARCH_ID]
    tikhub_sort = tikhub["input"]["queryParams"]["sort"]["note"]
    assert "RELEVANCE" in tikhub_sort and "NEW" in tikhub_sort
    assert "HOT" in tikhub_sort and "TOP" in tikhub_sort and "COMMENTS" in tikhub_sort
    tikhub_note = tikhub_sort.lower()
    assert "chronological" in tikhub_note or "newest" in tikhub_note
    assert "weak" in tikhub_note or "unrelated" in tikhub_note
    tikhub_input = tikhub["input"]["note"]
    assert "RELEVANCE" in tikhub_input and "NEW" in tikhub_input


async def test_catalog_get_reddit_keyword_search_sort_new_weak_relevance(
        clients: AsyncClient):
    """Feedback #507 / #461: catalog_get must warn that chronological/new is weakly related."""
    for endpoint_id in (REDDIT_SEARCH_POSTS_ID, TIKHUB_REDDIT_SEARCH_ID):
        body = (await clients.get(f"/catalog/endpoints/{endpoint_id}")).json()
        field = body["endpoint"]["input"]["queryParams"]["sort"]
        note = field["note"].lower()
        input_note = body["endpoint"]["input"]["note"].lower()
        blob = f"{note} {input_note}"
        assert "relevance" in blob
        assert "chronological" in blob or "newest" in blob
        assert "weak" in blob or "unrelated" in blob
        assert "new" in blob
        if endpoint_id == REDDIT_SEARCH_POSTS_ID:
            assert field["enum"] == REDDIT_SEARCH_POSTS_SORT
            assert field["example"] == "relevance"
        else:
            raw = field["note"]
            assert "NEW" in raw and "RELEVANCE" in raw


TWITTER_TWEET_TRANSCRIPT_ID = "scrapecreators.x.v1-twitter-tweet-transcript"
TWITTER_TWEET_DETAIL_ID = "scrapecreators.x.v1-twitter-tweet"


def test_scrapecreators_twitter_tweet_transcript_article_null():
    """Feedback #633: native video tweet transcript; Articles may return transcript: null.

    catalog_get used to advertise a tweet URL with no URL-shape caveat, so agents
    treated HTTP success + transcript: null as a successful empty caption while
    still paying the per-call credit. Observed on X Articles whose media is only
    article-embedded video. Catalog-only: input.note names native video tweet
    URLs, treats null as unsupported / no transcript, and points at tweet detail
    scrapecreators.x.v1-twitter-tweet for embedded video URLs. Settlement is
    unchanged.

    Ref: https://docs.scrapecreators.com/openapi.json
    """
    cat = cs.load()
    ep = cat.by_id[TWITTER_TWEET_TRANSCRIPT_ID]
    assert ep["path"] == "/v1/twitter/tweet/transcript"
    url_note = ep["input"]["queryParams"]["url"]["note"].lower()
    assert "video" in url_note
    assert "article" in url_note
    input_note = ep["input"]["note"].lower()
    assert "native" in input_note and "video tweet" in input_note
    assert "article" in input_note
    assert "transcript" in input_note and "null" in input_note
    assert "credit" in input_note
    assert "unsupported" in input_note
    assert "no transcript" in input_note
    assert "empty" in input_note
    assert TWITTER_TWEET_DETAIL_ID in ep["input"]["note"]
    assert "embedded video" in input_note
    assert ep["cost"]["value"] == 1
    assert ep["cost"]["currency"] == "credit"


async def test_catalog_get_scrapecreators_twitter_tweet_transcript_article_null(
        clients: AsyncClient):
    """Feedback #633: catalog_get must warn that Article-embedded video can return transcript: null."""
    body = (await clients.get(
        f"/catalog/endpoints/{TWITTER_TWEET_TRANSCRIPT_ID}")).json()
    url_note = body["endpoint"]["input"]["queryParams"]["url"]["note"].lower()
    assert "video" in url_note
    assert "article" in url_note
    input_note = body["endpoint"]["input"]["note"].lower()
    assert "native" in input_note and "video tweet" in input_note
    assert "article" in input_note
    assert "transcript" in input_note and "null" in input_note
    assert "credit" in input_note
    assert "unsupported" in input_note
    assert "no transcript" in input_note
    assert TWITTER_TWEET_DETAIL_ID in body["endpoint"]["input"]["note"]


LLM_MENTIONS_HISTORICAL_ID = "dataforseo.x.ai-optimization-llm-mentions-historical-live"
LLM_MENTIONS_MULTI_TARGET_ID = (
    "dataforseo.x.ai-optimization-llm-mentions-multi-target-metrics-live"
)


def test_dataforseo_llm_mentions_historical_target_is_and_combined():
    """Feedback #218: historical-live `target` is one AND-combined filter.

    catalog_get used to advertise "up to 10 entities" without AND semantics, so
    agents sent many brands in one call expecting multiple series. Upstream
    AND-combines include/exclude entities into one metrics series. Brand
    comparison is multi-target-metrics-live (`targets` with keys) or one call
    per brand. The wikipedia+bmw example stays a filter combo.
    """
    cat = cs.load()
    ep = cat.by_id[LLM_MENTIONS_HISTORICAL_ID]
    assert ep["path"] == "/ai_optimization/llm_mentions/historical/live"
    note = ep["input"]["body"]["target"]["note"]
    assert "AND-combined" in note
    assert "one metrics series" in note or "one series" in note
    assert LLM_MENTIONS_MULTI_TARGET_ID in note
    example = ep["input"]["body"]["target"]["example"]
    assert example[0]["domain"] == "en.wikipedia.org"
    assert example[0]["search_filter"] == "exclude"
    assert example[1]["keyword"] == "bmw"
    tasks = ep["test_request"]["body"]
    assert isinstance(tasks, list) and len(tasks) == 1
    assert tasks[0]["target"][0]["domain"] == "en.wikipedia.org"
    assert tasks[0]["target"][1]["keyword"] == "bmw"
    multi = cat.by_id[LLM_MENTIONS_MULTI_TARGET_ID]
    assert "targets" in multi["input"]["body"]
    assert "target" not in multi["input"]["body"]


async def test_catalog_get_dataforseo_llm_mentions_historical_names_and_semantics(
        clients: AsyncClient):
    """Feedback #218: catalog_get must not advertise multi-series `target`."""
    body = (await clients.get(f"/catalog/endpoints/{LLM_MENTIONS_HISTORICAL_ID}")).json()
    note = body["endpoint"]["input"]["body"]["target"]["note"]
    assert "AND-combined" in note
    assert "one metrics series" in note or "one series" in note
    assert LLM_MENTIONS_MULTI_TARGET_ID in note
    tmpl = body["call_template"]
    assert tmpl.startswith(
        f"treg call {LLM_MENTIONS_HISTORICAL_ID} --method POST")
    assert "en.wikipedia.org" in tmpl
    assert "exclude" in tmpl
    assert "bmw" in tmpl


async def test_catalog_get_dataforseo_llm_mentions_multi_target_names_targets_bound(
        clients: AsyncClient):
    """Feedback #490: catalog_get must name the 2-10 / 40501 targets bound."""
    body = (await clients.get(f"/catalog/endpoints/{LLM_MENTIONS_MULTI_TARGET_ID}")).json()
    field = body["endpoint"]["input"]["body"]["targets"]
    assert field.get("required") is False
    note = field["note"]
    lower = note.lower()
    assert "required" in lower
    assert "2" in note and "10" in note
    assert "40501" in note
    assert LLM_MENTIONS_HISTORICAL_ID in note
    example = field["example"]
    assert len(example) == 4
    assert [item["key"] for item in example] == [
        "chat_gpt", "claude", "gemini", "perplexity"
    ]
    tmpl = body["call_template"]
    assert tmpl.startswith(
        f"treg call {LLM_MENTIONS_MULTI_TARGET_ID} --method POST")
    assert "chat_gpt" in tmpl
    assert "perplexity" in tmpl


def test_dataforseo_llm_mentions_platform_omitted_is_google_only():
    """Feedback #489: historical + multi-target `platform` omit is google only.

    catalog_get used to advertise both default google and "returned for both
    platforms". Live omit matches platform=google; chat_gpt is a different
    series. Settlement is unchanged.
    """
    cat = cs.load()
    for endpoint_id in (LLM_MENTIONS_HISTORICAL_ID, LLM_MENTIONS_MULTI_TARGET_ID):
        ep = cat.by_id[endpoint_id]
        field = ep["input"]["body"]["platform"]
        assert field.get("required") is False
        note = field["note"].lower()
        assert "optional" in note
        assert "chat_gpt" in note and "google" in note
        assert "defaults to google" in note
        assert "not both platforms" in note
        assert "returned for both" not in note
        assert "united states" in note and "english" in note
        assert field["example"] == "google"


LLM_MENTIONS_TOP_DOMAINS_ID = (
    "dataforseo.x.ai-optimization-llm-mentions-top-mentioned-domains-live"
)


def test_dataforseo_llm_mentions_chat_gpt_location_is_us_only():
    """Feedback #359: historical + top-domains chat_gpt location is US-only.

    catalog_get used to list location_code without the chat_gpt 2840 / 40501
    caveat, so agents sent country codes (e.g. 2036) and read envelope Ok as
    success. Settlement is unchanged.
    """
    cat = cs.load()
    for endpoint_id in (LLM_MENTIONS_HISTORICAL_ID, LLM_MENTIONS_TOP_DOMAINS_ID):
        ep = cat.by_id[endpoint_id]
        loc = ep["input"]["body"]["location_code"]
        name = ep["input"]["body"]["location_name"]
        loc_note = loc["note"].lower()
        assert "chat_gpt" in loc_note
        assert "2840" in loc_note
        assert "united states" in loc_note
        assert "40501" in loc_note
        assert "tasks[]" in loc_note or "tasks[" in loc_note
        assert loc["example"] == 2840
        name_note = name["note"].lower()
        assert "chat_gpt" in name_note
        assert "united states" in name_note
        assert "40501" in name_note


async def test_catalog_get_dataforseo_llm_mentions_chat_gpt_location_is_us_only(
        clients: AsyncClient):
    """Feedback #359: catalog_get must name chat_gpt US-only location / 40501."""
    for endpoint_id in (LLM_MENTIONS_HISTORICAL_ID, LLM_MENTIONS_TOP_DOMAINS_ID):
        body = (await clients.get(f"/catalog/endpoints/{endpoint_id}")).json()
        loc = body["endpoint"]["input"]["body"]["location_code"]
        note = loc["note"].lower()
        assert "chat_gpt" in note
        assert "2840" in note
        assert "40501" in note
        assert "tasks[]" in note or "tasks[" in note
        assert loc["example"] == 2840


async def test_catalog_get_dataforseo_llm_mentions_platform_omitted_is_google_only(
        clients: AsyncClient):
    """Feedback #489: catalog_get must not say omit returns both platforms."""
    for endpoint_id in (LLM_MENTIONS_HISTORICAL_ID, LLM_MENTIONS_MULTI_TARGET_ID):
        body = (await clients.get(f"/catalog/endpoints/{endpoint_id}")).json()
        field = body["endpoint"]["input"]["body"]["platform"]
        note = field["note"].lower()
        assert "defaults to google" in note
        assert "not both platforms" in note
        assert "returned for both" not in note
        assert field["example"] == "google"


GOOGLE_TRENDS_ID = "serpapi.x.google-trends"


def test_serpapi_google_trends_data_type_names_geo_map_cardinality():
    """Feedback #440: GEO_MAP is compared regional breakdown (multiple queries);
    GEO_MAP_0 is interest by region (single query).

    catalog_get used to list TIMESERIES | GEO_MAP | GEO_MAP_0 | RELATED_TOPICS |
    RELATED_QUERIES with no cardinality, so agents sent GEO_MAP with one keyword
    and got HTTP 400. Settlement is unchanged.

    Ref: https://serpapi.com/google-trends-api
    """
    cat = cs.load()
    ep = cat.by_id[GOOGLE_TRENDS_ID]
    assert ep["path"] == "/search"
    note = ep["input"]["queryParams"]["data_type"]["note"].lower()
    assert "geo_map" in note
    assert "multiple" in note
    assert "compar" in note
    assert "geo_map_0" in note
    assert "single" in note
    assert "timeseries" in note
    assert "related_topics" in note
    assert "related_queries" in note


async def test_catalog_get_serpapi_google_trends_data_type_cardinality(
        clients: AsyncClient):
    """Feedback #440: catalog_get must warn GEO_MAP needs multiple queries."""
    body = (await clients.get(f"/catalog/endpoints/{GOOGLE_TRENDS_ID}")).json()
    note = body["endpoint"]["input"]["queryParams"]["data_type"]["note"].lower()
    assert "geo_map" in note
    assert "multiple" in note
    assert "compar" in note
    assert "geo_map_0" in note
    assert "single" in note
    assert "timeseries" in note
    assert "related_topics" in note
    assert "related_queries" in note


GOOGLE_MAPS_ID = "serpapi.x.google-maps"


def test_serpapi_google_maps_documents_place_id():
    """Feedback #525: Google place_id NAP lookup was undocumented.

    catalog_get listed only engine/type/q/ll/start, so a place_id-only call
    returned Treg 400 requiring type and q. Upstream accepts place_id without
    other optional params (https://serpapi.com/google-maps-api); Treg schema
    validation still requires type and q. Catalog-only: document optional
    place_id and keep type/q required. Settlement is unchanged.
    """
    cat = cs.load()
    ep = cat.by_id[GOOGLE_MAPS_ID]
    assert ep["path"] == "/search"
    params = ep["input"]["queryParams"]
    assert params["engine"]["required"] is True
    assert params["type"]["required"] is True
    assert params["q"]["required"] is True
    place = params["place_id"]
    assert place["type"] == "string"
    assert place.get("required") is False
    note = place["note"].lower()
    assert "place_id" in note
    assert "nap" in note or ("name" in note and "address" in note and "phone" in note)
    assert "type=place" in note
    assert "https://serpapi.com/google-maps-api" in place["note"]
    assert "type" in note and "q" in note
    type_note = params["type"]["note"].lower()
    assert "place_id" in type_note
    assert "search" in type_note and "place" in type_note
    q_note = params["q"]["note"].lower()
    assert "place_id" in q_note
    assert "search" in q_note
    input_note = ep["input"]["note"].lower()
    assert "place_results" in input_note
    assert "local_results" in input_note
    assert "place_id" in input_note
    test = ep["test_request"]["queryParams"]
    assert test == {
        "engine": "google_maps",
        "type": "search",
        "q": "pizza",
        "ll": "@40.7455096,-74.0083012,14z",
    }


async def test_catalog_get_serpapi_google_maps_place_id(clients: AsyncClient):
    """Feedback #525: catalog_get must document place_id for NAP lookup."""
    body = (await clients.get(f"/catalog/endpoints/{GOOGLE_MAPS_ID}")).json()
    params = body["endpoint"]["input"]["queryParams"]
    assert params["type"]["required"] is True
    assert params["q"]["required"] is True
    place = params["place_id"]
    assert place["type"] == "string"
    assert place.get("required") is False
    note = place["note"].lower()
    assert "place_id" in note
    assert "type=place" in note
    assert "https://serpapi.com/google-maps-api" in place["note"]
    tmpl = body["call_template"]
    assert tmpl.startswith(f"treg call {GOOGLE_MAPS_ID}")
    assert "type=search" in tmpl
    assert "q=pizza" in tmpl
    assert "place_id=" not in tmpl


TIKTOK_ADS_SEARCH_ID = "tikhub.x.tiktok-ads-search-ads"
TIKTOK_ADS_SEARCH_PERIOD = "7 | 30 | 120 | 180"


def test_tikhub_tiktok_ads_search_ads_period_and_limit_notes():
    """Feedback #561 / #459: period is 7|30|120|180; live limit max is ~20.

    catalog_get used to advertise period as a free integer (example 180) and
    limit as "Items per page" (example 20). Live Creative Center / TikHub
    nested validation rejects other period values and rejects limit=30/50 even
    though some OpenAPI text says max 50. Catalog-only: name the period oneof
    and prefer limit ≤ 20. Settlement is unchanged.
    """
    cat = cs.load()
    ep = cat.by_id[TIKTOK_ADS_SEARCH_ID]
    assert ep["path"] == "/api/v1/tiktok/ads/search_ads"
    body = ep["input"]["body"]

    period = body["period"]
    assert period["type"] == "integer"
    assert period["example"] == 180
    assert TIKTOK_ADS_SEARCH_PERIOD in period["note"]

    limit = body["limit"]
    assert limit["type"] == "integer"
    assert limit["example"] == 20
    limit_note = limit["note"].lower()
    assert "default 20" in limit_note
    assert "≤ 20" in limit["note"]
    assert "50" in limit_note
    assert (ep.get("test_request") or {}).get("body", {}).get("limit") == 5

    sibling = cat.by_id["tikhub.x.tiktok-ads-get-top-ads-spotlight"]
    assert sibling["input"]["body"]["limit"]["note"] == "Items per page"


async def test_catalog_get_tikhub_tiktok_ads_search_ads_period_and_limit(
        clients: AsyncClient):
    """Feedback #561 / #459: catalog_get must name period oneof and limit ≤ 20."""
    body = (await clients.get(f"/catalog/endpoints/{TIKTOK_ADS_SEARCH_ID}")).json()
    fields = body["endpoint"]["input"]["body"]
    assert TIKTOK_ADS_SEARCH_PERIOD in fields["period"]["note"]
    assert fields["period"]["type"] == "integer"
    assert fields["period"]["example"] == 180
    limit_note = fields["limit"]["note"].lower()
    assert "default 20" in limit_note
    assert "≤ 20" in fields["limit"]["note"]
    assert fields["limit"]["example"] == 20


TIKTOK_ADS_TRENDS_HASHTAG_LIST_ID = "tikhub.x.tiktok-ads-get-trends-hashtag-list"
TIKTOK_ADS_TRENDS_HASHTAG_TIME_RANGE = "7 | 30 | 90"


def test_tikhub_tiktok_ads_trends_hashtag_list_limit_is_preview_capped():
    """Feedback #606: body limit is often ignored; this is a tiny public preview.

    catalog_get used to advertise limit as "Items per page" with example 20,
    and test_request / call templates use limit 5+. A live paid call requesting
    30 hashtags (Spain / 7 days) returned only 3 items with data.pagination
    {hasMore:false, limit:3, page:1, totalCount:3} — the captured
    example_response already shows that shape. Catalog-only: limit.note and
    input.note warn that the public trends list is a small preview (~3 items),
    the requested limit is frequently ignored or capped, and agents must trust
    data.pagination over the request body. Do not invent a larger national
    ranking. time_range.note names 7 | 30 | 90 without changing types.
    Settlement, routing and credentials are unchanged. Sibling #424 (opaque
    400 validation) stays on its own ticket.
    """
    cat = cs.load()
    ep = cat.by_id[TIKTOK_ADS_TRENDS_HASHTAG_LIST_ID]
    assert ep["path"] == "/api/v1/tiktok/ads/get_trends_hashtag_list"
    assert ep["method"] == "POST"
    body = ep["input"]["body"]

    limit = body["limit"]
    assert limit["type"] == "integer"
    assert limit["example"] == 20
    note = limit["note"].lower()
    assert "preview" in note
    assert "~3" in note or "tiny" in note
    assert "ignored" in note or "capped" in note
    assert "data.pagination" in note
    assert "limit" in note and "totalcount" in note and "hasmore" in note
    assert "national" in note or "ranking" in note

    input_note = ep["input"]["note"].lower()
    assert "preview" in input_note
    assert "ranking" in input_note or "dump" in input_note
    assert "pagination" in input_note

    time_range = body["time_range"]
    assert time_range["type"] == "integer"
    assert time_range["example"] == 7
    assert TIKTOK_ADS_TRENDS_HASHTAG_TIME_RANGE in time_range["note"]

    assert (ep.get("test_request") or {}).get("body", {}).get("limit") == 5
    assert ep["cost"]["type"] == "per_success"
    assert ep["cost"]["value"] == 0.001
    assert ep["cost"]["currency"] == "USD"


async def test_catalog_get_tikhub_tiktok_ads_trends_hashtag_list_limit_preview(
        clients: AsyncClient):
    """Feedback #606: catalog_get must warn that limit is a preview cap."""
    body = (await clients.get(
        f"/catalog/endpoints/{TIKTOK_ADS_TRENDS_HASHTAG_LIST_ID}")).json()
    fields = body["endpoint"]["input"]["body"]
    note = fields["limit"]["note"].lower()
    assert "preview" in note
    assert "ignored" in note or "capped" in note
    assert "data.pagination" in note
    assert "totalcount" in note and "hasmore" in note
    input_note = body["endpoint"]["input"]["note"].lower()
    assert "preview" in input_note
    assert "pagination" in input_note
    assert TIKTOK_ADS_TRENDS_HASHTAG_TIME_RANGE in fields["time_range"]["note"]
    assert fields["limit"]["example"] == 20
    assert fields["time_range"]["example"] == 7


YOUTUBE_SEARCH_ID = "scrapecreators.x.v1-youtube-search"
YOUTUBE_SEARCH_SORTBY = ["relevance", "popular"]
YOUTUBE_SEARCH_UPLOAD_DATE = ["today", "this_week", "this_month", "this_year"]
YOUTUBE_SEARCH_TYPE = ["videos", "shorts", "channels", "playlists"]
YOUTUBE_SEARCH_DURATION = ["under_3_min", "between_3_and_20_min", "over_20_min"]


def test_scrapecreators_youtube_search_filter_enums():
    """Feedback #117 / #370: GET /v1/youtube/search only accepts OpenAPI enums.

    catalog_get used to advertise sortBy as a free string (example relevance)
    with no enum, so agents sent view_count from sibling YouTube search APIs
    (justoneapi / tikhub) and got HTTP 400. Upstream OpenAPI enum is
    relevance | popular only. uploadDate / type / duration have their own
    enums; type uses plural forms (not video/channel). call_template stays
    sortBy=relevance. Settlement is unchanged.

    Ref: https://docs.scrapecreators.com/openapi.json
    """
    cat = cs.load()
    ep = cat.by_id[YOUTUBE_SEARCH_ID]
    assert ep["path"] == "/v1/youtube/search"
    params = ep["input"]["queryParams"]

    sort_by = params["sortBy"]
    assert sort_by["enum"] == YOUTUBE_SEARCH_SORTBY
    assert sort_by["example"] == "relevance"
    sort_note = sort_by["note"].lower()
    assert "relevance" in sort_note and "popular" in sort_note
    assert "view_count" in sort_note
    assert "upload_date" in sort_note
    assert "rating" in sort_note
    assert "400" in sort_note
    assert "not accepted" in sort_note

    upload = params["uploadDate"]
    assert upload["enum"] == YOUTUBE_SEARCH_UPLOAD_DATE
    assert "today" in upload["note"]
    assert "this_week" in upload["note"]
    assert "this_month" in upload["note"]
    assert "this_year" in upload["note"]

    type_field = params["type"]
    assert type_field["enum"] == YOUTUBE_SEARCH_TYPE
    assert type_field["example"] == "videos"
    type_note = type_field["note"].lower()
    assert "plural" in type_note
    assert "not video/channel" in type_note

    duration = params["duration"]
    assert duration["enum"] == YOUTUBE_SEARCH_DURATION
    assert duration["example"] == "under_3_min"
    duration_note = duration["note"].lower()
    assert "not shorts" in duration_note

    assert (ep.get("test_request") or {}).get("queryParams", {}).get("sortBy") == "relevance"
    assert "sortBy=relevance" in cs.call_template(ep)
    assert ep["cost"]["value"] == 1
    assert ep["cost"]["currency"] == "credit"

    sibling = cat.by_id["justoneapi.x.youtube-search-v1"]["input"]["queryParams"]["sortBy"]
    assert "view_count" in sibling["enum"]


async def test_catalog_get_scrapecreators_youtube_search_filter_enums(
        clients: AsyncClient):
    """Feedback #117 / #370: catalog_get must name relevance|popular, not view_count."""
    body = (await clients.get(f"/catalog/endpoints/{YOUTUBE_SEARCH_ID}")).json()
    params = body["endpoint"]["input"]["queryParams"]
    assert params["sortBy"]["enum"] == YOUTUBE_SEARCH_SORTBY
    assert params["sortBy"]["example"] == "relevance"
    note = params["sortBy"]["note"].lower()
    assert "relevance" in note and "popular" in note
    assert "view_count" in note
    assert "not accepted" in note
    assert "400" in note
    assert params["uploadDate"]["enum"] == YOUTUBE_SEARCH_UPLOAD_DATE
    assert params["type"]["enum"] == YOUTUBE_SEARCH_TYPE
    assert params["duration"]["enum"] == YOUTUBE_SEARCH_DURATION
    tmpl = body["call_template"]
    assert tmpl.startswith(f"treg call {YOUTUBE_SEARCH_ID}")
    assert "sortBy=relevance" in tmpl
    assert "view_count" not in tmpl


SPEECH_28_IDS = (
    "minimax.voice-gen.speech-2-8-hd",
    "minimax.voice-gen.speech-2-8-turbo",
)
SPEECH_28_LANGUAGE_BOOST = [
    "Chinese", "Chinese,Yue", "English", "Arabic", "Russian", "Spanish",
    "French", "Portuguese", "German", "Turkish", "Dutch", "Ukrainian",
    "Vietnamese", "Indonesian", "Japanese", "Italian", "Korean", "Thai",
    "Polish", "Romanian", "Greek", "Czech", "Finnish", "Hindi", "Bulgarian",
    "Danish", "Hebrew", "Malay", "Persian", "Slovak", "Swedish", "Croatian",
    "Filipino", "Hungarian", "Norwegian", "Slovenian", "Catalan", "Nynorsk",
    "Tamil", "Afrikaans", "auto",
]
SPEECH_28_EMOTIONS = [
    "happy", "sad", "angry", "fearful", "disgusted", "surprised", "calm",
]
SPEECH_28_BITRATES = [32000, 64000, 128000, 256000]
SPEECH_28_SAMPLE_RATES = [8000, 16000, 22050, 24000, 32000, 44100]


def _assert_speech_28_input_enums(body: dict) -> None:
    """Feedback #598 / #599 / #602: speech-2.8 catalog fields match MiniMax OpenAPI."""
    language = body["language_boost"]
    assert language["enum"] == SPEECH_28_LANGUAGE_BOOST
    assert language["example"] == "auto"
    lang_note = language["note"].lower()
    assert "english(uk)" in lang_note
    assert "en-gb" in lang_note
    assert "english" in lang_note
    assert "2013" in language["note"]

    emotion = body["voice_setting"]["properties"]["emotion"]
    assert emotion["enum"] == SPEECH_28_EMOTIONS
    emotion_note = emotion["note"].lower()
    assert "whisper" in emotion_note
    assert "fluent" in emotion_note
    assert "2.6" in emotion_note
    assert "2013" in emotion["note"]
    voice_note = body["voice_setting"]["note"].lower()
    assert "whisper" in voice_note and "fluent" in voice_note

    audio = body["audio_setting"]
    assert audio["example"]["bitrate"] == 128000
    bitrate = audio["properties"]["bitrate"]
    assert bitrate["enum"] == SPEECH_28_BITRATES
    assert bitrate["example"] == 128000
    bitrate_note = bitrate["note"].lower()
    assert "mp3" in bitrate_note
    assert "192000" in bitrate["note"]
    assert "2013" in bitrate["note"]
    sample_rate = audio["properties"]["sample_rate"]
    assert sample_rate["enum"] == SPEECH_28_SAMPLE_RATES
    audio_note = audio["note"].lower()
    assert "192000" in audio["note"]
    assert "mp3" in audio_note and "wav" in audio_note and "flac" in audio_note


def test_minimax_speech_28_language_emotion_audio_enums():
    """Feedback #598 / #599 / #602: Speech 2.8 HD+Turbo catalog enums.

    catalog_get used to advertise language_boost as a free string (example auto),
    voice_setting.emotion only as an unnamed optional control, and audio_setting
    bitrate only via the 128000 example. Live MiniMax returns status 2013 for
    English(UK), emotion=whisper, and bitrate=192000. Catalog-only: name the
    OpenAPI enums and the 2.8 emotion subset. Settlement is unchanged.
    Ref: https://platform.minimax.io/docs/api-reference/speech-t2a-http
    """
    cat = cs.load()
    for endpoint_id in SPEECH_28_IDS:
        ep = cat.by_id[endpoint_id]
        assert ep["path"] == "/v1/t2a_v2"
        _assert_speech_28_input_enums(ep["input"]["body"])
        audio = (ep.get("test_request") or {}).get("body", {}).get("audio_setting") or {}
        assert audio.get("bitrate") == 128000
        assert ep["cost"]["currency"] == "USD"


async def test_catalog_get_minimax_speech_28_language_emotion_audio_enums(
        clients: AsyncClient):
    """Feedback #598 / #599 / #602: catalog_get must name speech-2.8 enums."""
    for endpoint_id in SPEECH_28_IDS:
        body = (await clients.get(f"/catalog/endpoints/{endpoint_id}")).json()
        _assert_speech_28_input_enums(body["endpoint"]["input"]["body"])
        tmpl = body["call_template"]
        assert tmpl.startswith(f"treg call {endpoint_id}")
        assert "128000" in tmpl
        assert "192000" not in tmpl
        assert "English(UK)" not in tmpl
        assert "whisper" not in tmpl


IMAGE_01_ID = "minimax.image-gen.from_text"


def test_minimax_image_01_platform_request_pins_model():
    """Feedback #634: image-01 must declare platform_request like Speech 2.8.

    body.model was optional with default image-01 and only pinned via cost.table
    when: {body.model: image-01}. _enforce_platform_request treats a singleton-enum
    table selector as a required exact match, so omitting the documented default
    returned catalog_parameter_invalid for body.model. Catalog-only: required
    singleton enum + platform_request body.model: image-01.
    """
    ep = cs.load().by_id[IMAGE_01_ID]
    assert ep["platform_request"] == {"body.model": "image-01"}
    model = ep["input"]["body"]["model"]
    assert model["required"] is True
    assert model["enum"] == ["image-01"]
    assert model["example"] == "image-01"
    assert "default" not in model
    assert (ep.get("test_request") or {}).get("body", {}).get("model") == "image-01"


async def test_catalog_get_minimax_image_01_platform_request(clients: AsyncClient):
    """Feedback #634: catalog_get must require model image-01 on the documented call."""
    body = (await clients.get(f"/catalog/endpoints/{IMAGE_01_ID}")).json()
    model = body["endpoint"]["input"]["body"]["model"]
    assert model["required"] is True
    assert model["enum"] == ["image-01"]
    assert model["example"] == "image-01"
    assert (body["endpoint"].get("test_request") or {}).get("body", {}).get("model") == "image-01"
    tmpl = body["call_template"]
    assert tmpl.startswith(f"treg call {IMAGE_01_ID}")
    assert "image-01" in tmpl


HEYGEN_AVATAR_IV_ID = "openrouter.x.heygen-avatar-iv"
HEYGEN_AVATAR_IV_PASSTHROUGH = (
    "voice_id", "voice_settings", "motion_prompt", "expressiveness",
    "fit", "remove_background", "background", "caption", "title",
)


def _assert_heygen_avatar_iv_input(body: dict) -> None:
    """Feedback #594: Avatar IV must expose photo, optional audio, and HeyGen passthrough."""
    prompt = body["prompt"]
    assert "paper boat" not in prompt["example"].lower()
    assert "tts" in prompt["note"].lower() or "script" in prompt["note"].lower()

    audio_flag = body["generate_audio"]
    assert audio_flag["default"] is False
    flag_note = audio_flag["note"].lower()
    assert "generate_audio: false" in audio_flag["note"] or "generate_audio: false" in flag_note
    assert "input_references" in flag_note

    refs = body["input_references"]
    assert refs["required"] is True
    ref_note = refs["note"].lower()
    assert "frame_images" in ref_note
    assert "image_url" in ref_note
    assert "audio_url" in ref_note
    example = refs["example"]
    assert example[0]["type"] == "image_url"
    assert example[0]["image_url"]["url"].startswith("https://")

    provider = body["provider"]
    assert provider["required"] is False
    provider_note = provider["note"]
    assert "provider.options.heygen.parameters" in provider_note
    for key in HEYGEN_AVATAR_IV_PASSTHROUGH:
        assert key in provider_note
    passthrough = provider["properties"]["options"]["properties"]["heygen"]["properties"]["parameters"]["properties"]
    assert set(passthrough) == set(HEYGEN_AVATAR_IV_PASSTHROUGH)
    assert passthrough["voice_id"]["note"]
    example_voice = provider["example"]["options"]["heygen"]["parameters"]["voice_id"]
    assert example_voice


def test_openrouter_heygen_avatar_iv_documents_photo_audio_and_passthrough():
    """Feedback #594: Avatar IV catalog named only generic video fields.

    The live rate card has supported_frame_images: null and generate_audio: false.
    Photo and optional audio ride input_references; HeyGen controls ride
    provider.options.heygen.parameters. Settlement is unchanged.
    Ref: https://openrouter.ai/heygen/avatar-iv
    """
    ep = cs.load().by_id[HEYGEN_AVATAR_IV_ID]
    assert ep["path"] == "/videos"
    _assert_heygen_avatar_iv_input(ep["input"]["body"])
    assert ep["cost"]["table"][0]["value"] == 0.05


async def test_catalog_get_openrouter_heygen_avatar_iv_photo_script(
        clients: AsyncClient):
    """Feedback #594: catalog_get must show a photo+script call, not a scenic prompt."""
    body = (await clients.get(f"/catalog/endpoints/{HEYGEN_AVATAR_IV_ID}")).json()
    _assert_heygen_avatar_iv_input(body["endpoint"]["input"]["body"])
    tmpl = body["call_template"]
    assert tmpl.startswith(f"treg call {HEYGEN_AVATAR_IV_ID}")
    assert "input_references" in tmpl
    assert "image_url" in tmpl
    assert "paper boat" not in tmpl
    assert "Welcome to our product tour" in tmpl


CRUSTDATA_COMPANIES_SEARCH_ID = "crustdata.companies.search"
CRUSTDATA_PEOPLE_SEARCH_ID = "crustdata.people.search"


def _assert_crustdata_ranked_search(body: dict, *, ranked_drops_cursor: bool) -> None:
    search = body["search"]
    assert search["type"] == "object"
    assert search.get("required") is False
    note = search["note"]
    assert "{query, mode?}" in note
    assert "hybrid" in note and "lexical" in note and "semantic" in note
    assert "default hybrid" in note
    sorts_note = body["sorts"]["note"]
    assert "{field" in sorts_note and "column" not in sorts_note
    assert "not supported with ranked search" in sorts_note.lower()
    if ranked_drops_cursor:
        assert "not supported with ranked search" in body["cursor"]["note"].lower()
        assert "1000" in body["limit"]["note"] and "100" in body["limit"]["note"]
        assert "hard constraints" in body["filters"]["note"]
    else:
        assert "not supported with ranked search" not in body["cursor"]["note"].lower()


def test_crustdata_company_and_person_search_document_ranked_nl_search():
    """Feedback #631: catalog_get omitted Crustdata's official `search` body field.

    CompanySearchRequest accepts filters or search. `search` is {query, mode?}
    with mode hybrid|lexical|semantic (default hybrid). Ranked company search
    does not support cursor/sorts and caps limit at 100. Person search documents
    the same search object; sorts use {field, order} not {column, order}.
    """
    cat = cs.load()
    companies = cat.by_id[CRUSTDATA_COMPANIES_SEARCH_ID]
    people = cat.by_id[CRUSTDATA_PEOPLE_SEARCH_ID]
    assert companies["path"] == "/company/search"
    assert people["path"] == "/person/search"
    _assert_crustdata_ranked_search(companies["input"]["body"], ranked_drops_cursor=True)
    _assert_crustdata_ranked_search(people["input"]["body"], ranked_drops_cursor=False)
    assert companies["input"]["note"] == "supply at least one of filters or search"
    assert people["input"]["note"] == "supply at least one of filters or search"
    assert "search" not in companies["test_request"]["body"]
    assert companies["cost"]["value"] == 0.03
    assert people["cost"]["value"] == 0.03


async def test_catalog_get_crustdata_companies_search_lists_search_object(
        clients: AsyncClient):
    """Feedback #631: catalog_get must list CompanySemanticSearch {query, mode?}."""
    body = (await clients.get(f"/catalog/endpoints/{CRUSTDATA_COMPANIES_SEARCH_ID}")).json()
    _assert_crustdata_ranked_search(body["endpoint"]["input"]["body"], ranked_drops_cursor=True)
    assert body["endpoint"]["input"]["note"] == "supply at least one of filters or search"
