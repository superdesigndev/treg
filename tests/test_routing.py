"""Capability routing — first-party routed endpoints.
`treg.people.email.find` picks a child and runs it through the ordinary call use case."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlmodel import select

from treg import audit
from treg.domain import money as ledger
from treg.domain.catalog.schema import is_v2
from treg.application.call import overflow as call_overflow
from treg.application.call import resolve as call_resolve
from treg.application.call import route as call_route
from treg.application.call import service as call_service
from treg.application import catalog_stats
from treg.application.call.types import ResolutionFailed, UpstreamResponse
from treg.config import Settings, get_settings
from treg.infra.db import session_maker
from treg.domain.catalog import store as catalog_store
from treg.domain.catalog.routing import paths as P
from treg.domain.catalog.routing.contracts import canonical_identity, parse_contracts
from treg.domain.catalog.routing.plan import UNMEASURED_HIT_PRIOR, Candidate, cost_at, rank, sampled_provider_allowed
from treg.infra.catalog_observations import CachedEndpointObservationReader
from treg.infra.upstream import relay as relay_module
from treg.models import CallRecord, Hold, LedgerEntry, OverflowRoute
from treg.timeutil import utcnow_naive

from test_marketplace_call import _balance, firecrawl_platform_on, platform_on  # noqa: F401

ROUTED = "treg.people.email.find"


@pytest.fixture
def enrichment_on(monkeypatch, platform_on):
    for p in ("HUNTER", "TOMBA", "LEADMAGIC", "LEADSFORGE", "FINDYMAIL", "AVIATO", "FIBER_AI"):
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{p}", f"PLATFORM-{p}-KEY")
    monkeypatch.setenv("TREG_PLATFORM_KEY_TOMBA_SECRET", "PLATFORM-TOMBA-SECRET")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "hunter,tomba,leadmagic,leadsforge,findymail,aviato,fiber-ai")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def enrichment_with_miss_declarers_on(monkeypatch, enrichment_on):
    """enrichment_on plus the two providers whose miss is a 4xx with a body (prospeo 400 NO_MATCH)
    or a plain 4xx (limadata 404) — without them in TREG_PLATFORM_PROVIDERS a prospeo/limadata
    test never runs the child and passes vacuously."""
    for p in ("PROSPEO", "LIMADATA"):
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{p}", f"PLATFORM-{p}-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "hunter,tomba,leadmagic,leadsforge,findymail,aviato,fiber-ai,prospeo,limadata")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def enrichment_with_quickenrich_on(monkeypatch, platform_on):
    """Like enrichment_on but includes QuickEnrich - the cheapest provider for phone/email lookups."""
    for p in ("HUNTER", "TOMBA", "LEADMAGIC", "LEADSFORGE", "FINDYMAIL", "AVIATO", "FIBER_AI", "QUICKENRICH"):
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{p}", f"PLATFORM-{p}-KEY")
    monkeypatch.setenv("TREG_PLATFORM_KEY_TOMBA_SECRET", "PLATFORM-TOMBA-SECRET")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "hunter,tomba,leadmagic,leadsforge,findymail,aviato,fiber-ai,quickenrich")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _relay_by_provider(answers: dict[str, list[tuple[int, dict]]], seen: list):
    """A fake upstream keyed by the vendor host: each provider answers its scripted list in order."""
    async def _relay(request, upstream_url, tool, secrets, client, drop_params=None, force_identity=False):
        provider = next((p for p in answers if p != "*" and p in upstream_url), "*")  # "*": any other vendor
        if (on_send := relay_module.ON_SEND.get()) is not None:
            on_send()  # as the real relay does once its local checks pass
        body = b""
        async for chunk in request.body_stream():
            body += chunk
        seen.append((provider, request.method, dict(request.query_items), json.loads(body) if body else None))
        status, doc = answers[provider].pop(0)
        payload = json.dumps(doc).encode()
        async def _s():
            yield payload
        async def _c():
            return None
        return UpstreamResponse(status, ((b"content-type", b"application/json"),), _s(), _c)
    return _relay



def test_limit_exceeded_note_names_adapter_shape():
    """why distinguishes a child that never maps limit from one that floors it."""
    from types import SimpleNamespace
    bare = SimpleNamespace(in_map={"q": "queryParams.q"}, in_expr={})
    floored = SimpleNamespace(in_map={"q": "body.objective"},
                              in_expr={"body.match_limit": "at_least(at_most(limit, 100), 5)"})
    assert call_route._adapter_uses_limit(bare) is False
    assert call_route._adapter_uses_limit(floored) is True
    assert call_route._limit_exceeded_note(
        asked=2, returned=5, served_by="parallel.people.search", merged=False, adapter=floored
    ) == {"asked": 2, "returned": 5, "why": "parallel.people.search returned more rows than asked"}
    assert call_route._limit_exceeded_note(
        asked=3, returned=10, served_by="crawl4ai.web.search", merged=False, adapter=bare
    )["why"] == "crawl4ai.web.search does not accept limit"
    assert call_route._limit_exceeded_note(
        asked=2, returned=3, served_by="icypeas.people.search", merged=True, adapter=floored
    )["why"] == "merged answers exceeded the asked limit"


# ---- pure ------------------------------------------------------------------------------------

def test_expression_language():
    doc = {"data": {"email": "a@x.io", "score": 80, "verification": {"status": "valid"}}, "emails": [{"email": "e", "type": "work"}], "none": []}
    assert P.evaluate("data.email", doc) == "a@x.io"
    assert P.evaluate("data.score / 100", doc) == 0.8
    assert P.evaluate("data.verification.status == 'valid'", doc) is True
    assert P.evaluate("data.email == null", doc) is False and P.evaluate("data.missing == null", doc) is True
    assert P.evaluate("none == []", doc) is True and P.evaluate("emails == []", doc) is False
    assert P.evaluate("emails[0].email", doc) == "e" and P.evaluate("emails[3].email", doc) is None
    assert P.evaluate("coalesce(data.missing, data.email)", doc) == "a@x.io"
    assert P.evaluate("coalesce(none, [])", doc) == [] and P.evaluate("coalesce(data.missing, [])", doc) == []
    assert P.evaluate("coalesce(none, []) == []", doc) is True and P.evaluate("coalesce(emails, []) == []", doc) is False
    assert P.evaluate("coalesce(data.missing, none)", doc) == []
    assert P.evaluate("split_first(data.name)", {"data": {"name": "Patrick Collison"}}) == "Patrick"
    assert P.evaluate("split_last(data.name)", {"data": {"name": "Patrick"}}) is None
    assert P.evaluate("join(a, b)", {"a": "Patrick", "b": "Collison"}) == "Patrick Collison"
    jobs = {"jobs": [{"n": "a", "now": True}, {"n": "b", "hit": True}]}
    assert P.evaluate("get(flagged(jobs, 'hit', 'now'), 'n')", jobs) == "b"
    assert P.evaluate("get(flagged(jobs, 'missing', 'now'), 'n')", jobs) == "a"
    assert P.evaluate("one_place(location)", {"location": " Dublin "}) == "Dublin"
    assert P.evaluate("one_place(location)", {"location": "London, United Kingdom"}) is None
    assert P.evaluate("choose(unlocked, email, null)", {"unlocked": False, "email": "j***@x.io"}) is None
    assert P.evaluate("at_most(limit, 10)", {"limit": 3}) == 3
    assert P.evaluate("at_most(limit, 10)", {"limit": 25}) == 10
    assert P.evaluate("at_least(limit, 10)", {"limit": 3}) == 10
    assert P.evaluate("at_least(limit, 10)", {"limit": 25}) == 25
    with pytest.raises(ValueError):
        P.evaluate("nope(a)", doc)


def test_a_route_false_adapter_is_never_routed():
    from treg.domain.catalog.routing import contracts as C, plan, synthetic

    contract = C.Contract(capability="x.find", summary="", identity=(("q",),), identity_types={"q": "str"},
                          derive={}, filters={}, output={"rows": {"required": True}}, miss="rows == []")
    adapters = {eid: C.Adapter(eid, (("q",),), {"q": "body.q"}, {"rows": "rows"}, "rows == []",
                               route=eid != "c.find", verified=True) for eid in ("a.find", "b.find", "c.find")}
    endpoints = [{"id": eid, "provider": eid.split(".")[0]} for eid in adapters]
    routed = plan.candidates_for(contract, endpoints, adapters, {"q": "x"})[0]
    assert [ep["id"] for ep, _, _ in routed] == ["a.find", "b.find"]
    row = synthetic.routed_endpoint(contract, endpoints, adapters, lambda cost, provider: None)
    assert row["routed_children"] == ["a.find", "b.find"]
    assert C.parse_adapters({"adapters": {"c.find": {"route": False}}})["c.find"].route is False


def test_a_row_map_puts_every_provider_row_in_the_contract_shape():
    """`row:` reads each provider row into the contract's keys, so an agent reads the same fields
    whoever answered; a row the map cannot read stays the provider's own, never dropped."""
    from treg.domain.catalog.routing import contracts as C

    doc = {"contracts": {"x.search": {
        "identity": [{"q": "str"}], "output": {"people": {"type": "list", "required": True}},
        "miss": "people == []",
        "row": {"of": "people", "needs": ["name"], "fields": {"name": "str", "location": "str", "summary": "str"}}}}}
    contract = C.parse_contracts(doc)["x.search"]
    ad = C.parse_adapters({"adapters": {"p.search": {
        "accepts": [["q"]], "in": {"q": "body.q"}, "out": {"people": "items"}, "miss": "items == []",
        "row": {"name": "join(first, last)", "location": "place(city, country, country)", "summary": "texts(lines)"}}}})["p.search"]
    ad = C.Adapter(**{**ad.__dict__, "_row_of": contract.row_of, "_row_needs": contract.row_needs})
    long = "word " * 200
    rows = ad.from_upstream({"items": [
        {"first": "Ada", "last": "Lovelace", "city": "London", "country": "UK", "lines": ["one", long], "id": 7},
        {"handle": "@someone"}, "not a row"]})["people"]
    assert rows[0]["name"] == "Ada Lovelace" and rows[0]["location"] == "London, UK" and "id" not in rows[0]
    assert len(rows[0]["summary"]) == C.ROW_TEXT_MAX, "text is capped"
    assert rows[1] == {"handle": "@someone"}, "unreadable row kept as the provider's"
    assert rows[2] == "not a row" and len(rows) == 3, "a non-object item is kept too"
    with pytest.raises(ValueError, match="row"):
        C.parse_contracts({"contracts": {"y": {**doc["contracts"]["x.search"], "row": {"of": "nope", "needs": ["name"], "fields": {"name": "str"}}}}})
    with pytest.raises(ValueError, match="needs"):
        C.parse_contracts({"contracts": {"y": {**doc["contracts"]["x.search"], "row": {"of": "people", "needs": ["age"], "fields": {"name": "str"}}}}})
    stray = C.Adapter(**{**ad.__dict__, "row_map": {**ad.row_map, "age": "age"}})
    endpoint = {"test_request": {"body": {"q": "x"}}}
    assert C.verify(stray, contract, endpoint, {"items": [{"first": "A"}]}) == (False, "row: fields outside the contract's row: ['age']")
    assert C.verify(ad, contract, endpoint, {"items": [{"first": "A"}]})[0] is True


def test_an_instagram_search_row_keeps_its_field_names_and_drops_picture_urls():
    """instagram.search.users rows: an agent picks accounts by audience size and bio. The provider's
    picture URL and bio-link metadata were most of each row's tokens and told it nothing."""
    ad = catalog_store.load().adapters["scrapecreators.x.v1-instagram-search-profiles"]
    assert ad.verified, ad.verify_note
    row = ad.from_upstream({"profiles": [{
        "username": "ada", "full_name": "Ada", "url": "https://www.instagram.com/ada/", "follower_count": 12000,
        "media_count": 300, "biography": "coffee roaster", "is_private": False, "is_professional_account": True,
        "profile_pic_url": "https://scontent.cdninstagram.com/" + "x" * 400,
        "bio_links": [{"url": "https://linktr.ee/ada", "lynx_url": "https://l.instagram.com/" + "y" * 300}]}]})["users"][0]
    assert row == {"username": "ada", "full_name": "Ada", "url": "https://www.instagram.com/ada/", "follower_count": 12000,
                   "media_count": 300, "biography": "coffee roaster", "is_private": False,
                   "is_professional_account": True}, "the provider's own names, so existing readers still work"
    # a link is never cut: a 305-character URL cut to 300 points at another resource
    long_url = "https://example.com/" + "a" * 275 + "?id=target"
    row2 = ad.from_upstream({"profiles": [{"username": "bo", "url": "https://www.instagram.com/bo/",
                                           "external_url": long_url, "biography": "b " * 400}]})["users"][0]
    assert row2["external_url"] == long_url and len(long_url) > 300
    assert len(row2["biography"]) == 300, "text is still cut"


def test_a_row_field_may_keep_more_text_than_the_default_cut():
    """people.search's `summary` is the evidence an agent checks a brief against: it keeps up to its
    own `max` while every other text field is still cut to ROW_TEXT_MAX."""
    from treg.domain.catalog.routing import contracts as C

    cat = catalog_store.load()
    cap = cat.contracts["people.search"].row_fields["summary"]["max"]
    assert cap > C.ROW_TEXT_MAX
    ad = cat.adapters["parallel.people.search"]
    long = "evidence " * (cap // 4)
    row = ad.from_upstream({"entities": [{"name": "Ada " + "x" * 400, "url": "https://linkedin.com/in/ada",
                                          "description": long}]})["people"][0]
    assert len(row["summary"]) == cap and len(row["name"]) == C.ROW_TEXT_MAX
    doc = {"identity": [{"q": "str"}], "output": {"people": {"type": "list", "required": True}},
           "row": {"of": "people", "needs": ["name"], "fields": {"name": {"type": "str"}}}}
    for bad in (0, -5, True, "1000", 1.5):
        with pytest.raises(ValueError, match="max"):
            C.parse_contracts({"contracts": {"y": {**doc, "row": {**doc["row"], "fields": {"name": {"type": "str", "max": bad}}}}}})
    with pytest.raises(ValueError, match="max"):
        C.parse_contracts({"contracts": {"y": {**doc, "row": {**doc["row"], "fields": {"name": {"type": "url", "max": 10}}}}}})


def test_identity_notes_say_what_to_put_in_each_key_and_only_email_finds_mention_domain_match():
    """A routed endpoint's identity keys read "identity" plus the contract's note, so an agent knows
    what a free-text `q` is for; the `domain_match` sentence belongs to email finds only."""
    from treg.domain.catalog.routing import contracts as C

    cat = catalog_store.load()
    body = cat.by_id["treg.people.search"]["input"]["body"]
    assert body["q"]["note"].startswith("identity; describe the people in words")
    assert body["company_domain"]["note"].startswith("identity; the employer's website domain")
    assert "domain_match" not in cat.by_id["treg.people.search"]["input"]["note"]
    assert "domain_match" in cat.by_id["treg.people.email.find"]["input"]["note"]
    doc = {"identity": [{"q": "str"}], "output": {"a": "str"}}
    assert C.parse_contracts({"contracts": {"y": {**doc, "identity_notes": {"q": " words "}}}})["y"].identity_notes == {"q": "words"}
    for bad in ({"nope": "x"}, {"q": ""}, {"q": 3}, {}, ["q"]):
        with pytest.raises(ValueError, match="identity_notes"):
            C.parse_contracts({"contracts": {"y": {**doc, "identity_notes": bad}}})


def test_a_filter_range_is_a_whole_min_and_optional_max_on_an_int_filter_and_shows_on_the_routed_row():
    from treg.domain.catalog.routing import contracts as C

    cat = catalog_store.load()
    for cap in ("web.map", "people.search"):
        limit = cat.by_id[f"treg.{cap}"]["input"]["body"]["limit"]
        assert limit["min"] == 1 and "max" not in limit
    doc = {"identity": [{"q": "str"}], "output": {"a": "str"}}
    for ranged in ({"type": "int", "default": 10, "min": 1}, {"type": "int", "default": 10, "min": 1, "max": 50}):
        assert C.parse_contracts({"contracts": {"y": {**doc, "filters": {"limit": ranged}}}})["y"].filters["limit"] == ranged
    for bad in ({"type": "int", "max": 50}, {"type": "str", "min": 1}, {"type": "str", "min": 1, "max": 50},
                {"type": "int", "min": 0}, {"type": "int", "min": 0, "max": 50}, {"type": "int", "min": 5, "max": 4},
                {"type": "int", "min": 1.0}, {"type": "int", "min": 1, "max": 50.0},
                {"type": "int", "min": True}, {"type": "int", "min": 1, "max": True}):
        with pytest.raises(ValueError, match="range"):
            C.parse_contracts({"contracts": {"y": {**doc, "filters": {"limit": bad}}}})


def test_a_stand_in_q_lets_a_free_text_provider_answer_a_title_request():
    """people.search builds `q` from the caller's title, company and place, but only for a provider
    that accepts none of the caller's own fields: exa joins a {title} or {company_domain, title}
    request, and a provider that takes {title} still gets exactly the title."""
    from treg.domain.catalog.routing.contracts import adapter_accepts
    from treg.domain.catalog.routing.plan import unscoped

    cat = catalog_store.load()
    contract = cat.contracts["people.search"]
    ident, _ = canonical_identity(contract, {"title": "Product Manager", "location": "Seattle, United States",
                                             "keywords": ["developer tools"]})
    assert "q" not in ident, "the stand-in never enters the request's identity"
    exa = cat.adapters["exa.people.search"]
    assert adapter_accepts(exa, ident) == ("q",) and not unscoped(exa, contract, ident)
    _, body = exa.to_upstream(ident, ("q",))
    assert body["query"] == "Product Manager in Seattle, United States developer tools"
    icy = cat.adapters["icypeas.people.search"]
    variant = adapter_accepts(icy, ident)
    assert variant == ("title",)
    assert icy.to_upstream(ident, variant)[1]["query"] == {
        "currentJobTitle": {"include": ["Product Manager"]}, "location": {"include": ["Seattle, United States"]},
        "keyword": {"include": ["developer tools"]}}, "a provider that takes the title gets it unchanged"
    scoped, _ = canonical_identity(contract, {"title": "CTO", "company_domain": "stripe.com"})
    assert not unscoped(exa, contract, scoped), "the stand-in carries the company"
    assert exa.to_upstream(scoped, adapter_accepts(exa, scoped))[1]["query"] == "CTO at stripe.com"
    with pytest.raises(ValueError, match="stand_in"):
        from treg.domain.catalog.routing import contracts as C
        C.parse_contracts({"contracts": {"y": {"identity": [{"q": "str"}], "output": {"a": "str"},
                                               "miss": "a == null", "stand_in": {"nope": "q"}}}})


def test_a_company_folded_into_the_callers_q_scopes_a_free_text_provider():
    """exa and parallel write the company into the text of a `q` the caller sent, so a
    {company_domain, q} request ("executives" at openai.com) reaches them; a {company_domain, title}
    request reaches them through the stand-in, which carries the company itself."""
    from treg.domain.catalog.routing.contracts import adapter_accepts
    from treg.domain.catalog.routing.plan import unscoped

    cat = catalog_store.load()
    contract = cat.contracts["people.search"]
    asked, _ = canonical_identity(contract, {"q": "executives", "company_domain": "openai.com"})
    for eid, field in (("exa.people.search", "query"), ("parallel.people.search", "objective")):
        ad = cat.adapters[eid]
        assert adapter_accepts(ad, asked) == ("q",) and not unscoped(ad, contract, asked)
        assert ad.to_upstream(asked, ("q",))[1][field] == "executives openai.com"
        scoped, _ = canonical_identity(contract, {"title": "CTO", "company_domain": "stripe.com"})
        assert not unscoped(ad, contract, scoped)
        assert ad.to_upstream(scoped, ("q",))[1][field] == "CTO at stripe.com", "the domain is said once"
    # A provider that answers this shape by company alone drops what q asked; one with the title
    # in its variant still answers {q, title}
    for eid in ("crawl4ai.people.search", "icypeas.people.search"):
        assert unscoped(cat.adapters[eid], contract, asked) == ("q",)
    titled_q, _ = canonical_identity(contract, {"q": "backend engineer", "title": "Backend Engineer"})
    assert not unscoped(cat.adapters["icypeas.people.search"], contract, titled_q)
    # Only what this call's upstream request carries: enrichlayer sends the name and the domain of
    # {q, company_domain, full_name} but not q, and a name is not what q asked for
    named, _ = canonical_identity(contract, {"q": "speaks fluent Japanese", "company_domain": "example.com",
                                             "full_name": "Alice Cooper"})
    assert unscoped(cat.adapters["enrichlayer.people.search"], contract, named) == ("q",)
    # and an adapter's mapped field outside the variant it matched is not sent: prospeo answers
    # {full_name, company_domain} by the company alone
    person, _ = canonical_identity(contract, {"full_name": "Alice Cooper", "company_domain": "example.com"})
    assert unscoped(cat.adapters["prospeo.people.search"], contract, person) == ("full_name",)
    assert not unscoped(cat.adapters["crawl4ai.people.search"], contract, person)
    assert cat.adapters["crawl4ai.people.search"].to_upstream(person, adapter_accepts(
        cat.adapters["crawl4ai.people.search"], person))[1]["where"] == {
        "company": {"domain": "example.com"}, "person": {"name": "Alice Cooper"}}
    # A text join skips a list, so a domain sent as one is not carried
    listed, _ = canonical_identity(contract, {"q": "executives", "company_domain": ["openai.com"]})
    assert unscoped(cat.adapters["exa.people.search"], contract, listed) == ("company_domain",)
    # Only providers that send both remain, so the shape fills to the caller's limit like a bare q
    assert contract.fill_target({"q": "executives", "company_domain": "openai.com", "limit": 20}) == 20
    assert contract.fill_target({"q": "   ", "limit": 5}) is None, "a blank q is not sent"
    assert contract.fill_target({"q": "executives", "limit": 5}) == 5
    # An adapter that sends its stand-in only through an expression still sends the keys inside it
    import dataclasses
    expr_only = dataclasses.replace(cat.adapters["exa.people.search"], in_map={})
    titled, _ = canonical_identity(contract, {"title": "CTO"})
    assert expr_only.to_upstream(titled, ("q",))[1]["query"] == "CTO"
    assert not unscoped(expr_only, contract, titled)


@pytest.mark.parametrize("sent, text", [
    ({"title": "Head of Tax"}, "Head of Tax"),
    ({"full_name": "Alice Cooper"}, "Alice Cooper"),
    ({"company_domain": "stripe.com"}, "people at stripe.com"),
    ({"company_domain": "stripe.com", "title": "Head of Tax"}, "Head of Tax at stripe.com"),
    ({"company_domain": "stripe.com", "title": "Head of Tax", "country": "ie"}, "Head of Tax at stripe.com in Ireland"),
    ({"company_domain": "stripe.com", "title": "Head of Tax", "country": "ie", "location": "Dublin, Ireland"},
     "Head of Tax at stripe.com in Dublin, Ireland"),
    ({"title": "CTO", "location": "Cambridge", "country": "us"}, "CTO in Cambridge, United States"),
    ({"company_domain": "stripe.com", "full_name": "Alice Cooper"}, "Alice Cooper at stripe.com"),
    ({"full_name": "Alice Cooper", "title": "CTO", "country": "de"}, "Alice Cooper, CTO in Germany"),
    ({"company_domain": "stripe.com", "title": "CFO, Head of Finance or Controller"},
     "CFO, Head of Finance or Controller at stripe.com"),
    ({"company_domain": ["stripe.com"], "title": "CTO"}, "CTO"),
])
def test_the_people_search_stand_in_says_who_at_which_company_where(sent, text):
    """The stand-in `q` is a sentence a profile search reads: who, at the company, in the place."""
    from treg.domain.catalog.routing.contracts import adapter_accepts
    cat = catalog_store.load()
    ident, _ = canonical_identity(cat.contracts["people.search"], sent)
    exa = cat.adapters["exa.people.search"]
    assert exa.with_stand_in(ident)["q"] == text
    assert adapter_accepts(exa, ident) == ("q",)


def test_stand_in_carries_names_only_keys_its_expression_reads():
    from treg.domain.catalog.routing import contracts as C
    doc = {"identity": [{"q": "str"}, {"title": "str"}, {"company_domain": "str"}], "output": {"a": "str"},
           "miss": "a == null", "filters": {"country": "str"}, "stand_in": {"q": "join(title, country)"}}
    assert C.parse_contracts({"contracts": {"y": {**doc, "stand_in_carries": {"q": ["title", "country"]}}}})[
        "y"].stand_in_carries == {"q": ("title", "country")}
    for bad in ({"q": ["company_domain"]}, {"title": ["q"]}, {"q": []}, {"q": ["nope"]}, {"q": ["q"]}, ["q"]):
        with pytest.raises(ValueError, match="stand_in_carries"):
            C.parse_contracts({"contracts": {"y": {**doc, "stand_in_carries": bad}}})


def test_a_stand_in_carries_the_country_so_it_is_not_an_ignored_filter():
    """parallel has no country field of its own; the stand-in sentence says the country, so it
    answers the question asked. A `q` the caller wrote gets no stand-in, so there it is ignored."""
    from treg.domain.catalog.routing.plan import ignored_filters
    cat = catalog_store.load()
    contract = cat.contracts["people.search"]
    parallel = cat.adapters["parallel.people.search"]
    built, _ = canonical_identity(contract, {"company_domain": "stripe.com", "title": "CTO", "country": "ie"})
    assert ignored_filters(parallel, contract, built, {"company_domain", "title", "country"}) == ()
    written, _ = canonical_identity(contract, {"q": "CTO at stripe.com", "country": "ie"})
    assert ignored_filters(parallel, contract, written, {"q", "country"}) == ("country",)
    # Carried only when the value reaches the text: beside a location too, but not as a code with no name
    placed, _ = canonical_identity(contract, {"title": "CTO", "location": "Cambridge", "country": "us"})
    assert ignored_filters(parallel, contract, placed, {"title", "location", "country"}) == ()
    unnamed, _ = canonical_identity(contract, {"title": "CTO", "country": "zz"})
    assert ignored_filters(parallel, contract, unnamed, {"title", "country"}) == ("country",)
    named, _ = canonical_identity(contract, {"title": "CTO", "location": "Dublin, Ireland", "country": "ie"})
    assert parallel.to_upstream(named, ("q",))[1]["objective"] == "CTO in Dublin, Ireland"
    assert ignored_filters(parallel, contract, named, {"title", "location", "country"}) == (), \
        "a country the location already says is still said"


@pytest.mark.parametrize("identity, kept", [
    ({"title": "CTO", "location": "Dublin, Ireland", "country": "ie"}, True),
    ({"title": "CTO", "location": "Cambridge", "country": "us"}, True),
    ({"title": "CTO", "location": "Dublin, Ireland"}, True),
    ({"title": "CTO", "country": "zz"}, False),
])
async def test_strict_filters_keep_a_stand_in_provider_whose_text_says_the_filters(
        clients, all_people_search_on, identity, kept):
    cat = catalog_store.load()
    options = call_route.RouteOptions.from_headers(
        lambda key: "true" if key == call_route.STRICT_FILTERS_HEADER else None)
    assert options.strict_filters
    caller = SimpleNamespace(org_id=1, org=SimpleNamespace(id=1, platform_overflow_disabled=False))
    plan = await call_route.build_plan(cat.by_id["treg.people.search"], identity, caller, options)
    assert ("parallel.people.search" in [c.endpoint["id"] for c in plan.candidates]) is kept


def test_a_domain_is_said_only_as_the_whole_host():
    """A caller's `q` naming another host keeps the asked domain in the text, so the company scope
    is not silently lost."""
    from treg.domain.catalog.routing import paths as P
    assert P.evaluate("phrase(q, d)", {"q": "CEOs at example.com", "d": "example.com"}) == "CEOs at example.com"
    assert P.evaluate("phrase(q, d)", {"q": "CEOs at Example.com.", "d": "example.com"}) == "CEOs at Example.com."
    for q in ("CEOs at example.com.au", "CEOs at sub.example.com", "CEOs at my-example.com"):
        assert P.evaluate("phrase(q, d)", {"q": q, "d": "example.com"}) == f"{q} example.com"
    cat = catalog_store.load()
    asked, _ = canonical_identity(cat.contracts["people.search"],
                                  {"q": "CEOs at example.com.au", "company_domain": "example.com"})
    assert cat.adapters["exa.people.search"].to_upstream(asked, ("q",))[1]["query"] == "CEOs at example.com.au example.com"
    assert cat.adapters["parallel.people.search"].to_upstream(asked, ("q",))[1]["objective"] == \
        "CEOs at example.com.au example.com"


@pytest.fixture
def all_people_search_on(monkeypatch, platform_on):
    provs = sorted({e["provider"] for e in catalog_store.load().for_capability("people.search")})
    for p in provs:
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{p.upper().replace('-', '_')}", f"PLATFORM-{p}-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", ",".join(provs))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("identity", [
    {"company_domain": "example.com", "title": "Head of Tax", "country": "gb"},
    {"company_domain": "example.com", "title": "Head of Tax"},
    {"company_domain": "example.com"},
])
async def test_a_company_scoped_request_offers_the_free_text_providers_last(
        clients, all_people_search_on, monkeypatch, identity):
    """The stand-in makes exa and parallel candidates for a company-scoped request without moving
    anyone: every other provider keeps its order, and the two follow them, since a built `q` covers
    none of the caller's keys. Only a prefer rule or route rule moves them up."""
    import dataclasses
    cat = catalog_store.load()
    options = call_route.RouteOptions.from_headers(lambda key: None)
    caller = SimpleNamespace(org_id=1, org=SimpleNamespace(id=1, platform_overflow_disabled=False))
    ep = cat.by_id["treg.people.search"]
    plan = await call_route.build_plan(ep, identity, caller, options)
    order = [c.endpoint["id"] for c in plan.candidates]
    q_only = ["parallel.people.search", "exa.people.search"]
    assert order[-2:] == q_only and "exa.people.search" not in {d["endpoint_id"] for d in plan.dropped}
    exa = next(c for c in plan.candidates if c.endpoint["id"] == "exa.people.search")
    assert exa.variant == ("q",) and exa.ignored == ()
    _, body = exa.adapter.to_upstream(plan.identity, exa.variant)
    assert body["query"].startswith(identity.get("title", "people") + " at example.com")
    preferred = await call_route.build_plan(ep, identity, caller, call_route.RouteOptions.from_headers(
        lambda key: "exa" if key == call_route.PREFER_HEADER else None))
    assert preferred.candidates[0].endpoint["id"] == "exa.people.search", "a prefer moves it up"
    for eid in q_only:
        monkeypatch.setitem(cat.adapters, eid, dataclasses.replace(cat.adapters[eid], _stand_in={}))
    without = await call_route.build_plan(ep, identity, caller, options)
    assert [c.endpoint["id"] for c in without.candidates] == order[:-2], "nobody else moved"


@pytest.fixture
def people_search_on(monkeypatch, platform_on):
    for p in ("CRAWL4AI", "EXA", "PARALLEL", "ICYPEAS"):
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{p}", f"PLATFORM-{p}-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "crawl4ai,exa,parallel,icypeas")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _first_providers(ep_id: str, identity: dict, header: str | None = None, n: int = 2,
                           org_id: int = 1) -> list[str]:
    options = call_route.RouteOptions.from_headers(
        lambda key: header if header and key == call_route.PREFER_HEADER else None)
    caller = SimpleNamespace(org_id=org_id, org=SimpleNamespace(id=org_id, platform_overflow_disabled=False))
    plan = await call_route.build_plan(catalog_store.load().by_id[ep_id], identity, caller, options)
    return [c.endpoint["provider"] for c in plan.candidates][:n]


def test_phrase_leaves_out_what_the_text_already_says():
    from treg.domain.catalog.routing import paths as P
    assert P.evaluate("phrase(q, title)", {"q": "head of tax in phoenix", "title": "Head of Tax"}) == "head of tax in phoenix"
    assert P.evaluate("phrase(q, title)", {"q": "speaks Japanese", "title": "CTO"}) == "speaks Japanese CTO"
    assert P.evaluate("phrase(q, title)", {"q": "semiconductor startups", "title": "CTO"}) == "semiconductor startups CTO"
    assert P.evaluate("phrase(q, title)", {"title": "CTO"}) == "CTO"
    assert P.evaluate("phrase(q, title)", {"q": " ", "title": None}) is None


def test_current_role_is_the_open_role_not_the_first_listed():
    from treg.domain.catalog.routing import paths as P
    ended = {"title": "Developer Evangelist", "dates": {"from": "2017-02-01", "to": "2025-12-01"}}
    now = {"title": "Video Engineer", "dates": {"from": "2026-01-01", "to": None}}
    advisor = {"title": "Advisor", "dates": {"from": "2019-05-01", "to": None}}
    assert P.current_role([ended, now, advisor]) is now, "the latest-started role with no end date"
    assert P.current_role([ended, {"title": "Intern", "dates": {"from": "2012", "to": "2013"}}]) is ended
    assert P.current_role([{"title": "A"}, {"title": "B"}])["title"] == "A", "no dates keeps list order"
    assert P.current_role([{"title": "A"}, {"title": "B", "dates": {"from": "2010"}}])["title"] == "B", "a dated start wins"
    assert P.current_role([]) is None and P.current_role(None) is None


def test_past_titles_are_the_ended_roles_latest_first():
    from treg.domain.catalog.routing import paths as P
    hist = [{"title": "Video Engineer", "dates": {"from": "2026-01-01", "to": None}},
            {"title": "Developer Evangelist", "dates": {"from": "2017-02-01", "to": "2025-12-01"}},
            {"title": "Intern", "dates": {"from": "2012", "to": "2013"}},
            {"title": "Developer Evangelist", "dates": {"from": "2014", "to": "2016"}}]
    assert P.past_titles(hist) == ["Developer Evangelist", "Intern"], "ended roles only, each once"
    assert P.past_titles([hist[0]]) is None and P.past_titles(None) is None


def test_rows_rank_holders_of_the_asked_role_now_ahead_of_past_holders():
    """`row.rank_by_role`: a semantic search matches the whole history, so a former holder of the
    role came back beside current ones; the router puts the role held now first and flags each."""
    c = catalog_store.load().contracts["people.search"]
    cfg = c.row_rank
    now = {"name": "A", cfg["now"]: "Senior Developer Evangelist", cfg["since"]: "2023-01"}
    left = {"name": "B", cfg["now"]: "Support Engineer", cfg["past"]: ["Developer Evangelist"]}
    other = {"name": "C", cfg["now"]: "Developer Advocate"}
    retired = {"name": "D", cfg["now"]: "Consultant", cfg["ended"]: "2024-01"}
    out = c.rank_rows({cfg["asked"]: "Developer Evangelists"}, [left, retired, other, now])
    assert [r["name"] for r in out] == ["A", "C", "B", "D"], "now, then unknown, then past, provider order within"
    assert out[0][cfg["flag"]] == "current" and out[2][cfg["flag"]] == "past" and cfg["flag"] not in out[1]
    held_then_left = {"name": "E", cfg["now"]: "Developer Evangelist", cfg["ended"]: "2025-12"}
    assert c.rank_rows({cfg["asked"]: "developer evangelist"}, [held_then_left])[0][cfg["flag"]] == "past"
    rows = [left, now]
    assert c.rank_rows({"q": "developer evangelists"}, rows) is rows, "no asked role: the provider's order"
    headline = {"name": "F", cfg["now"]: "Former Developer Evangelist; now advisor"}
    out = c.rank_rows({cfg["asked"]: "Developer Evangelist"}, [left, headline])
    assert out[0] is headline and out[1][cfg["flag"]] == "past", \
        "a title with no work history may be a headline: unmarked, not current"
    dated = lambda t: {"name": t, cfg["now"]: t, cfg["since"]: "2024"}  # noqa: E731
    marked = lambda asked, *ts: [r.get(cfg["flag"]) for r in c.rank_rows({cfg["asked"]: asked}, [dated(t) for t in ts])]  # noqa: E731
    assert marked("C++ Developer", "C# Developer", "C Developer", "Senior C++ Developer") == ["current", None, None], \
        "C++, C# and C are different roles"
    assert marked("AI 产品经理", "AI 产品经理", "AI 工程师") == ["current", None]
    assert marked("产品经理", "产品经理", "高级产品经理") == ["current", None], "a word without spaces holds only itself"


def test_a_malformed_rank_by_role_fails_the_load():
    from treg.domain.catalog.routing.contracts import parse_contracts
    base = {"identity": [{"title": "str"}], "output": {"people": {"type": "list"}},
            "row": {"of": "people", "needs": ["name"], "fields": {"name": {"type": "str"}, "title": {"type": "str"},
                                                                   "past": {"type": "list"}, "until": {"type": "str"}, "m": {"type": "str"}}}}
    ok = {"asked": "title", "now": "title", "since": "title", "past": "past", "ended": "until", "flag": "m"}
    assert parse_contracts({"contracts": {"y": {**base, "row": {**base["row"], "rank_by_role": ok}}}})["y"].row_rank == ok
    for bad in ({**ok, "asked": "q"}, {**ok, "flag": "nope"}, {"asked": "title"}):
        with pytest.raises(ValueError, match="rank_by_role"):
            parse_contracts({"contracts": {"y": {**base, "row": {**base["row"], "rank_by_role": bad}}}})


@pytest.mark.parametrize("domain, first", [
    ("stripe.fr", ["hunter", "findymail"]),   # the domain suffix goes ahead of prefer
    ("stripe.com", ["findymail"]),
])
async def test_a_domain_suffix_rule_goes_ahead_of_prefer(clients, enrichment_on, monkeypatch, domain, first):
    import dataclasses
    cat = catalog_store.load()
    monkeypatch.setitem(cat.contracts, "people.email.find", dataclasses.replace(
        cat.contracts["people.email.find"], prefer_by_domain_suffix={"fr": ("hunter",)}, prefer=("findymail",)))
    got = await _first_providers(ROUTED, {"full_name": "Patrick Collison", "domain": domain}, n=len(first))
    assert got == first


def test_prefer_when_is_retired_and_the_validator_refuses_it(tmp_path, monkeypatch):
    """An order for one kind of request is a published route rule, reviewed and observed without a
    deploy; a contract field for it would bypass that loop. The loader skips the retired key, so an
    older catalog still loads with its orders ignored, and the catalog validator (CI) refuses it."""
    import importlib.util
    import shutil
    from pathlib import Path
    import yaml
    from treg.domain.catalog.routing.contracts import parse_contracts
    doc = {"contracts": {"y": {"identity": [{"q": "str"}], "output": {"a": "str"},
                               "prefer_when": [{"sent": ["q"], "prefer": ["exa"]}]}}}
    assert parse_contracts(doc)["y"].prefer == ()
    assert all(not hasattr(c, "prefer_when") for c in catalog_store.load().contracts.values())

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("catalog_validate_retired", root / "scripts" / "catalog_validate.py")
    validate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validate)
    shipped = root / "src" / "treg" / "catalog"
    shutil.copy(shipped / "adapters.yaml", tmp_path / "adapters.yaml")
    contracts = yaml.safe_load((shipped / "contracts.yaml").read_text())
    (tmp_path / "contracts.yaml").write_text(yaml.safe_dump(contracts))
    monkeypatch.setattr(validate, "CATALOG", tmp_path)
    errors: list[str] = []
    validate.check_routing(errors)
    assert errors == [], "the shipped contracts pass"
    contracts["contracts"]["people.search"]["prefer_when"] = [{"sent": ["q"], "prefer": ["exa"]}]
    (tmp_path / "contracts.yaml").write_text(yaml.safe_dump(contracts))
    validate.check_routing(errors)
    assert errors == ["contracts.yaml: contract people.search: prefer_when is retired; "
                      "publish a route rule for that order"]



def test_every_shipped_adapter_round_trips_its_fixture():
    cat = catalog_store.load()
    bad = {eid: a.verify_note for eid, a in cat.adapters.items() if not a.verified}
    assert bad == {}, bad
    ep = cat.by_id[ROUTED]
    assert ep["kind"] == "routed" and ep["provider"] == "treg" and len(ep["routed_children"]) >= 8
    assert cat.platform_eligible(ep) and ep["cost_range_usd"][0] < ep["cost_range_usd"][1]
    # children bill misses, so no single quote: a $0 floor read as "free"
    people = cat.by_id["treg.people.search"]
    assert people["miss_billed_by"] and cat.advertised_usd(cat.cost_view(people["cost"], "treg")) is None
    # a hand-verified round trip on the plan's worked example
    ad = cat.adapters["leadsforge.people.email.find"]
    q, b = ad.to_upstream({"first_name": "Patrick", "last_name": "Collison", "domain": "stripe.com", "full_name": "Patrick Collison"})
    assert b == {"firstName": "Patrick", "lastName": "Collison", "companyDomain": "stripe.com"} and q == {}
    assert ad.from_upstream({"email": "p@stripe.com", "status": "succeeded"}) == {"email": "p@stripe.com"}
    assert ad.is_miss({"email": None}) and not ad.is_miss({"email": "x"})


def test_every_adapter_const_passes_its_child_body_allowlist():
    cat = catalog_store.load()
    failures = {}
    for endpoint_id, adapter in cat.adapters.items():
        ep = cat.by_id[endpoint_id]
        body_consts = {path: value for path, value in adapter.const.items()
                       if path.startswith("body.")}
        if not body_consts or not (ep.get("body_allowlist") or ep.get("strict_body")):
            continue
        body = deepcopy((ep.get("test_request") or {}).get("body"))
        assert isinstance(body, dict), endpoint_id
        request = {"body": body}
        for path, value in body_consts.items():
            P.set_path(request, path, value)
        try:
            call_resolve._enforce_catalog_body(ep, json.dumps(body).encode())
        except ResolutionFailed as exc:
            failures[endpoint_id] = exc.detail
    assert failures == {}, failures


def test_every_routed_adapter_sends_its_rows_platform_request_pins():
    """A shared-key pin (`platform_request`) is enforced, not injected: a routed child whose adapter
    does not send the pinned value is a 400 on treg's key, and the router reads a 400 as the
    caller's fault and fails the whole routed call instead of trying the next child."""
    cat = catalog_store.load()
    missing = {}
    for endpoint_id, adapter in cat.adapters.items():
        pins = {path: value for path, value in (cat.by_id[endpoint_id].get("platform_request") or {}).items()
                if path.startswith(("body.", "queryParams."))}
        if not adapter.route or not pins:
            continue
        sent: dict = {}
        for path, value in adapter.const.items():
            P.set_path(sent, path, value)
        # a query string carries text: the relay reads a query pin back as the pinned value's type
        wrong = {path: P.get_path(sent, path) for path, value in pins.items()
                 if (str(P.get_path(sent, path)) != str(value) if path.startswith("queryParams.")
                     else P.get_path(sent, path) != value)}
        if wrong:
            missing[endpoint_id] = wrong
    assert missing == {}, missing


def test_firecrawl_web_adapters_are_verified_routed_children():
    cat = catalog_store.load()
    for parent, child in (
        ("treg.web.search", "firecrawl.web.search"),
        ("treg.web.extract", "firecrawl.web.scrape"),
        ("treg.web.map", "firecrawl.web.map"),
    ):
        adapter = cat.adapters[child]
        assert adapter.verified, (child, adapter.verify_note)
        assert not adapter.verify_note, child
        assert child in cat.by_id[parent]["routed_children"], (parent, child)


def test_you_web_adapters_are_verified_routed_children():
    cat = catalog_store.load()
    for parent, child in (
        ("treg.web.search", "you.web.search"),
        ("treg.web.extract", "you.web.contents"),
    ):
        adapter = cat.adapters[child]
        assert adapter.verified, (child, adapter.verify_note)
        assert not adapter.verify_note, child
        assert child in cat.by_id[parent]["routed_children"], (parent, child)


def test_parallel_adapters_are_routed_and_an_excerpt_only_page_is_a_hit():
    cat = catalog_store.load()
    for parent, child in (
        ("treg.web.search", "parallel.web.search"),
        ("treg.web.extract", "parallel.web.extract"),
        ("treg.people.search", "parallel.people.search"),
        ("treg.companies.search", "parallel.companies.search"),
    ):
        assert cat.adapters[child].verified, (child, cat.adapters[child].verify_note)
        assert child in cat.by_id[parent]["routed_children"], (parent, child)
    # A direct Extract without full_content still returns billed excerpts; only an empty page or
    # an unreadable URL (results [], errors[]) is the free miss.
    extract = cat.adapters["parallel.web.extract"]
    assert not extract.is_miss({"results": [{"url": "u", "excerpts": ["text"], "full_content": None}]})
    assert not extract.is_miss({"results": [{"url": "u", "excerpts": [], "full_content": "text"}]})
    assert extract.is_miss({"results": [{"url": "u", "excerpts": [], "full_content": None}]})
    assert extract.is_miss({"results": [], "errors": [{"url": "u", "error_type": "connect_error"}]})
    # A multi-URL call is never judged by its first page: a later page may have text.
    assert not extract.is_miss({"results": [{"url": "a", "excerpts": [], "full_content": None},
                                            {"url": "b", "excerpts": ["text"], "full_content": "text"}]})
    # With several pages, an explicitly empty usage[] is the miss; an absent one is unobserved.
    empty = {"url": "a", "excerpts": [], "full_content": None}
    assert extract.is_miss({"results": [empty, empty], "usage": []})
    assert not extract.is_miss({"results": [empty, empty]})


def test_search_adapter_does_not_treat_an_answer_without_results_as_a_miss():
    adapter = catalog_store.load().adapters["linkup.web.search"]
    assert adapter.verified
    assert adapter.is_miss({"results": []})
    assert not adapter.is_miss({"answer": "A sourced answer", "sources": []})
    assert not adapter.is_miss({"data": {"answer": "A structured answer"}})


@pytest.mark.parametrize(("parent", "child", "routed_input", "upstream_body", "expected_body", "expected_cost"), [
    (
        "treg.web.search", "firecrawl.web.search",
        {"q": "example", "limit": 3},
        {"success": True, "data": {"web": [{"title": "Example", "url": "https://example.com"}]}, "creditsUsed": 2},
        {"query": "example", "limit": 3}, 10_000,
    ),
    (
        "treg.web.extract", "firecrawl.web.scrape",
        {"url": "https://example.com"},
        {"success": True, "data": {"markdown": "# Example", "metadata": {"sourceURL": "https://example.com"}}},
        {"url": "https://example.com", "formats": ["markdown"], "parsers": []}, 5_000,
    ),
    (
        "treg.web.map", "firecrawl.web.map",
        {"url": "https://example.com", "q": "docs", "limit": 3},
        {"success": True, "links": [{"url": "https://example.com/docs"}]},
        {"url": "https://example.com", "search": "docs", "limit": 3}, 5_000,
    ),
])
async def test_firecrawl_web_routed_calls_serve_and_settle(
    clients, monkeypatch, firecrawl_platform_on,
    parent, child, routed_input, upstream_body, expected_body, expected_cost,
):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({
        "firecrawl": [(200, upstream_body)],
    }, seen))

    before = await _balance(clients)
    response = await clients.post(
        f"/call/{parent}", json=routed_input,
        headers={"X-Treg-Route-Prefer": "firecrawl", "X-Treg-Route-Waterfall": "0"},
    )
    assert response.status_code == 200, response.text
    doc = response.json()
    assert doc["_treg"]["served_by"] == child
    assert doc["_treg"]["charged_micro"] == expected_cost
    assert response.headers["X-Treg-Route-Outcome"] == "hit"
    assert before - await _balance(clients) == expected_cost
    assert len(seen) == 1 and seen[0][3] == expected_body


async def test_a_ceiling_is_judged_on_the_adapted_request_not_the_plan_upper_bound(clients, monkeypatch):
    """The plan prices a v2 row at its upper bound; the child holds the adapted request's own price
    with margin. A ceiling between the two is enough, so the call runs."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_TAVILY", "PLATFORM-TAVILY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tavily")
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "platform_margin", 0.25)
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({
        "tavily": [(200, {"results": [{"title": "Example", "url": "https://example.com"}], "usage": {"credits": 1}})],
    }, seen))
    response = await clients.post("/call/treg.web.search", json={"q": "example query", "limit": 3},
                                  headers={"X-Treg-Route-Prefer": "tavily", "X-Treg-Route-Max-Cost": "0.016"})
    assert response.status_code == 200, response.text
    assert response.json()["_treg"]["served_by"] == "tavily.web.search" and [r[0] for r in seen] == ["tavily"]


async def test_tavily_routed_empty_search_is_a_paid_miss_then_falls_through(
    clients, monkeypatch,
):
    monkeypatch.setenv("TREG_PLATFORM_KEY_TAVILY", "PLATFORM-TAVILY")
    monkeypatch.setenv("TREG_PLATFORM_KEY_EXA", "PLATFORM-EXA")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tavily,exa")
    get_settings.cache_clear()
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({
        "tavily": [(200, {"results": [], "usage": {"credits": 1}})],
        "exa": [(200, {
            "results": [{"title": "Example", "url": "https://example.com"}],
            "costDollars": {"total": 0.007},
        })],
    }, seen))

    before = await _balance(clients)
    response = await clients.post(
        "/call/treg.web.search",
        json={"q": "example query", "limit": 3},
        headers={"X-Treg-Route-Prefer": "tavily,exa"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["_treg"]["served_by"] == "exa.web.search"
    assert [attempt["outcome"] for attempt in data["_treg"]["tried"]] == ["miss", "hit"]
    assert [attempt["charged_micro"] for attempt in data["_treg"]["tried"]] == [8_000, 7_000]
    assert data["_treg"]["charged_micro"] == 15_000
    assert before - await _balance(clients) == 15_000
    assert [row[0] for row in seen] == ["tavily", "exa"]
    assert seen[0][3] == {
        "query": "example query", "max_results": 3,
        "search_depth": "basic", "include_usage": True,
    }
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []
        entries = (await db.execute(select(LedgerEntry))).scalars().all()
    call_id = response.headers["X-Treg-Call-Id"]
    assert {entry.call_id for entry in entries if entry.kind == "settle"} == {
        call_id + ":r0", call_id + ":r1",
    }
    get_settings.cache_clear()


def test_identity_variants_derive_and_never_cross():
    contract = catalog_store.load().contracts["people.email.find"]
    ident, variant = canonical_identity(contract, {"full_name": "Patrick Collison", "domain": "stripe.com"})
    assert variant == ("domain", "full_name") and ident["first_name"] == "Patrick" and ident["last_name"] == "Collison"
    ident, variant = canonical_identity(contract, {"first_name": "Patrick", "last_name": "Collison", "domain": "stripe.com"})
    assert ident["full_name"] == "Patrick Collison"
    ident, variant = canonical_identity(contract, {"linkedin_url": "https://www.linkedin.com/in/x"})
    assert variant == ("linkedin_url",) and "domain" not in ident
    assert canonical_identity(contract, {"full_name": "Patrick Collison"})[1] is None


def test_cost_at_and_ranking_math():
    assert cost_at({"usd": 0.0038, "type": "per_result", "per": 1}, {"limit": 10}) == 38_000
    assert cost_at({"usd": 0.0044, "type": "per_result", "per": 25}, {"limit": 10}) == 110_000, "lusha: 1 credit per 25 rows, minimum 1"
    assert cost_at({"usd": 0.0044, "type": "per_result", "per": 25}, {"limit": 40}) == 220_000
    assert cost_at({"usd": 0.005, "type": "per_call"}, {"limit": 10}) == 5_000
    assert cost_at({"usd": None}, {}) is None
    def ep(i, t="per_success"):
        return {"id": i, "provider": i.split(".")[0], "cost": {"type": t}}
    a = Candidate(ep("a.x"), None, ("domain",), "platform", 24_500, hit_rate=0.4, ok_rate=None, p50_ms=100, last_ok_days=1)
    b = Candidate(ep("b.x", "per_call"), None, ("domain",), "platform", 20_000, hit_rate=0.8, ok_rate=None, p50_ms=100, last_ok_days=1)
    own = Candidate(ep("c.x"), None, ("domain",), "credential", 0, hit_rate=None, ok_rate=None, p50_ms=None, last_ok_days=None)
    anonymous = Candidate(ep("d.x"), None, ("domain",), "anonymous", 0, hit_rate=None, ok_rate=None, p50_ms=None, last_ok_days=None)
    assert a.expected_cost_per_hit == pytest.approx(24_500), "per-success: billed only on a hit → price per hit"
    assert b.expected_cost_per_hit == pytest.approx(25_000), "per-call at 80% hit rate: 20000/0.8"
    assert [c.endpoint["id"] for c in rank([a, b, anonymous, own])] == ["c.x", "d.x", "a.x", "b.x"]
    assert [c.endpoint["id"] for c in rank([a, b], prefer=["b"])] == ["b.x", "a.x"]
    assert [c.endpoint["id"] for c in rank([a, b], exclude=["a"])] == ["b.x"]
    a.exhausted = True
    assert [c.endpoint["id"] for c in rank([a, b])] == ["b.x"]


def test_an_unmeasured_child_ranks_on_the_prior_not_on_a_certain_hit():
    def child(i, price, hit_rate=None, ok_rate=None, t="per_call"):
        return Candidate({"id": i, "provider": i.split(".")[0], "cost": {"type": t}}, None, ("video_id",),
                         "platform", price, hit_rate=hit_rate, ok_rate=ok_rate, p50_ms=None, last_ok_days=None)
    measured = child("measured.x", 1_000, hit_rate=0.96, ok_rate=1.0)
    seen_twice = child("seen.x", 1_000)
    works = child("works.x", 1_000, ok_rate=0.9)
    cheap = child("cheap.x", 400)
    per_success = child("free_miss.x", 1_000, t="per_success")
    assert seen_twice.expected_cost_per_hit == pytest.approx(1_000 / UNMEASURED_HIT_PRIOR)
    assert works.expected_cost_per_hit == pytest.approx(1_000 / (UNMEASURED_HIT_PRIOR * 0.9)), "the ok rate still counts"
    assert per_success.expected_cost_per_hit == pytest.approx(1_000), "a miss it never bills costs nothing"
    assert [c.endpoint["id"] for c in rank([seen_twice, works, measured])] == ["measured.x", "seen.x", "works.x"]
    assert rank([measured, cheap])[0] is cheap, "a child cheap enough is still tried first, so it can be measured"


def test_a_hit_rate_counts_from_the_publication_floor():
    from treg.domain.catalog import stats

    def ranked(hit_decided):
        published = stats.publish(["new.x"], {"new.x": stats.Tally(n=hit_decided, ok=hit_decided,
                                                                  hits=hit_decided, hit_decided=hit_decided)})
        new = Candidate({"id": "new.x", "provider": "new", "cost": {"type": "per_call"}}, None, ("video_id",),
                        "platform", 1_000, published["new.x"]["hit_rate"], published["new.x"]["ok_rate"], None, None)
        old = Candidate({"id": "old.x", "provider": "old", "cost": {"type": "per_call"}}, None, ("video_id",),
                        "platform", 1_000, 0.96, 1.0, None, None)
        return [c.endpoint["id"] for c in rank([new, old])]
    assert ranked(stats.MIN_HIT_SAMPLES - 1) == ["old.x", "new.x"]
    assert ranked(stats.MIN_HIT_SAMPLES) == ["new.x", "old.x"]


def test_sampled_provider_is_stable_and_applies_per_provider():
    percentages = {"leadocean": 30, "second": 30, "other": 0}
    decisions = [sampled_provider_allowed("leadocean", percentages, org_id=1,
                 capability="people.email.find", identity={"linkedin_url": f"https://linkedin.com/in/person-{i}"})
                 for i in range(1000)]
    assert 250 <= sum(decisions) <= 350
    assert decisions == [sampled_provider_allowed("leadocean", percentages, org_id=1,
                         capability="people.email.find", identity={"linkedin_url": f"https://linkedin.com/in/person-{i}"})
                         for i in range(1000)]
    assert decisions != [sampled_provider_allowed("second", percentages, org_id=1,
                         capability="people.email.find", identity={"linkedin_url": f"https://linkedin.com/in/person-{i}"})
                         for i in range(1000)]
    assert sampled_provider_allowed("unlisted", percentages, org_id=1, capability="people.email.find", identity={})
    assert not sampled_provider_allowed("other", percentages, org_id=1, capability="people.email.find", identity={})


@pytest.mark.parametrize("value", ["not-json", "[]", '{"leadocean":-1}', '{"leadocean":101}',
                                          '{"leadocean":30.5}', '{"leadocean":true}', '{"LeadOcean":30}'])
def test_sampled_provider_config_rejects_invalid_values(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, route_provider_sample_pct=value)


async def test_sampled_provider_only_filters_shared_key_routes(clients, monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "leadocean")
    monkeypatch.setenv("TREG_PLATFORM_KEY_LEADOCEAN", "test-key")
    get_settings.cache_clear()
    ep = catalog_store.load().by_id["treg.people.phone.find"]
    options = call_route.RouteOptions.from_headers(lambda key: None)
    identity = {"linkedin_url": "https://www.linkedin.com/in/example"}

    class Caller:
        org_id = 1
        org = SimpleNamespace(id=1, platform_overflow_disabled=False)

    try:
        for value, expected in (("", True), ("{}", True), ('{"leadocean":0}', False),
                                ('{"leadocean":100}', True)):
            monkeypatch.setenv("TREG_ROUTE_PROVIDER_SAMPLE_PCT", value)
            get_settings.cache_clear()
            plan = await call_route.build_plan(ep, identity, Caller(), options)
            assert any(c.endpoint["provider"] == "leadocean" for c in plan.candidates) is expected

        async def own_key(*args, **kwargs):
            return "own-key"

        monkeypatch.setattr(call_route, "_marketplace_secret", own_key)
        monkeypatch.setenv("TREG_ROUTE_PROVIDER_SAMPLE_PCT", '{"leadocean":0}')
        get_settings.cache_clear()
        plan = await call_route.build_plan(ep, identity, Caller(), options)
        assert any(c.endpoint["provider"] == "leadocean" and c.tier == "credential"
                   for c in plan.candidates)
    finally:
        get_settings.cache_clear()


async def test_sampled_provider_changes_routed_call_not_direct_call(clients, monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "leadocean")
    monkeypatch.setenv("TREG_PLATFORM_KEY_LEADOCEAN", "test-key")
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({
        "leadocean": [(200, {"data": {"best": {"phone": "+15555550100"}}}),
                      (200, {"data": {"best": {"phone": "+15555550100"}}})],
    }, seen))
    identity = {"linkedin_url": "https://www.linkedin.com/in/example"}
    try:
        monkeypatch.setenv("TREG_ROUTE_PROVIDER_SAMPLE_PCT", '{"leadocean":0}')
        get_settings.cache_clear()
        refused = await clients.post("/call/treg.people.phone.find", json=identity)
        assert refused.status_code == 503
        assert seen == []
        direct = await clients.get("/call/leadocean.people.phone.find", params=identity)
        assert direct.status_code == 200, direct.text
        assert direct.json()["data"]["best"]["phone"] == "+15555550100"

        monkeypatch.setenv("TREG_ROUTE_PROVIDER_SAMPLE_PCT", '{"leadocean":100}')
        get_settings.cache_clear()
        served = await clients.post("/call/treg.people.phone.find", json=identity)
        assert served.status_code == 200, served.text
        assert served.json()["_treg"]["served_by"] == "leadocean.people.phone.find"
        assert served.json()["_treg"]["charged_micro"] == 5_000
        assert [provider for provider, *_ in seen] == ["leadocean", "leadocean"]
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize(("status", "outcome", "charged"), [
    ("verified", "hit", 5_000), ("catch_all", "miss", 0), ("untested", "miss", 0),
])
async def test_leadocean_work_email_routes_only_verified_hits(clients, monkeypatch, status, outcome, charged):
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "leadocean")
    monkeypatch.setenv("TREG_PLATFORM_KEY_LEADOCEAN", "test-key")
    monkeypatch.setenv("TREG_ROUTE_PROVIDER_SAMPLE_PCT", '{"leadocean":100}')
    get_settings.cache_clear()
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({
        "leadocean": [(200, {"data": {"email": "jane@example.com", "status": status}})],
    }, seen))

    try:
        before = await _balance(clients)
        response = await clients.post("/call/treg.people.email.find",
                                      json={"linkedin_url": "https://www.linkedin.com/in/example"},
                                      headers={"X-Treg-Route-Waterfall": "0"})
        assert response.status_code == 200, response.text
        routed = response.json()["_treg"]
        assert routed["served_by"] == "leadocean.people.email.find.work"
        assert routed["outcome"] == outcome
        assert routed["charged_micro"] == charged
        assert before - await _balance(clients) == charged
        assert [provider for provider, *_ in seen] == ["leadocean"]
    finally:
        get_settings.cache_clear()


async def test_concurrent_routed_plans_share_one_cached_observation_refresh(
    clients: AsyncClient, enrichment_on, monkeypatch,
):
    from treg.domain.catalog import stats

    class Source:
        calls = 0

        async def get_many(self, endpoint_ids):
            self.calls += 1
            return {
                endpoint_id: {
                    "samples": 20, "ok_rate": 1.0, "p50_ms": 20, "p95_ms": 40,
                    "last_ok_days": 0, "hit_rate": 0.5, "hit_samples": 20,
                }
                for endpoint_id in endpoint_ids
            }

    async def request_time_aggregate(*args, **kwargs):
        raise AssertionError("routed planning must not aggregate CallRecord on the request path")

    source = Source()
    reader = CachedEndpointObservationReader(source)
    monkeypatch.setattr(call_route, "_endpoint_observation_reader", reader, raising=False)
    monkeypatch.setattr(stats, "observed", request_time_aggregate)
    ep = catalog_store.load().by_id[ROUTED]

    class _Org:
        id = 1

    class _Caller:
        org_id = 1
        org = _Org()

    try:
        plans = await asyncio.gather(*(
            call_route.build_plan(
                ep, {"full_name": "Patrick Collison", "domain": "stripe.com"},
                _Caller(), call_route.RouteOptions.from_headers(lambda key: None),
            )
            for _ in range(20)
        ))
        await reader.wait_for_idle()
    finally:
        await reader.aclose()

    assert all(plan.candidates for plan in plans)
    assert source.calls == 1


async def test_routed_plan_keeps_per_success_hit_fallback_from_the_cache(
    clients: AsyncClient, enrichment_on, monkeypatch,
):
    from treg import api as A
    from treg.domain.catalog import stats

    endpoint_id = "tomba.people.email.find"
    async with session_maker() as db:
        for cost_micro in (8_900, 8_900, 0):
            db.add(CallRecord(
                org_id=1, user_email="a@b.c", tool_name=endpoint_id, method="GET", path="/x",
                status_code=200, endpoint_id=endpoint_id, cost_observed_micro=cost_micro, hit=None,
            ))
        await db.commit()
    monkeypatch.setattr(stats, "MIN_HIT_SAMPLES", 3)
    assert (await catalog_stats.refresh(session_maker, now=utcnow_naive() + timedelta(minutes=2)))["caught_up"]
    reader = A.app.state.endpoint_observation_reader
    assert await reader.get_many([endpoint_id]) == {}
    await reader.wait_for_idle()
    warm = await reader.get_many([endpoint_id])
    assert warm[endpoint_id]["hit_rate"] == pytest.approx(2 / 3, abs=1e-3)

    async def request_time_aggregate(*args, **kwargs):
        raise AssertionError("the routed plan must use the warm observation cache")

    monkeypatch.setattr(stats, "observed", request_time_aggregate)
    monkeypatch.setattr(call_route, "_endpoint_observation_reader", reader, raising=False)
    ep = catalog_store.load().by_id[ROUTED]

    class _Org:
        id = 1

    class _Caller:
        org_id = 1
        org = _Org()

    plan = await call_route.build_plan(
        ep, {"full_name": "Patrick Collison", "domain": "stripe.com"},
        _Caller(), call_route.RouteOptions.from_headers(lambda key: None),
    )
    tomba = next(candidate for candidate in plan.candidates if candidate.endpoint["id"] == endpoint_id)
    assert tomba.hit_rate == pytest.approx(2 / 3, abs=1e-3)


async def test_an_overflow_candidate_prices_a_miss_by_its_route_unit(clients: AsyncClient, monkeypatch):
    """Overflow replaces a direct per-result child's price with the aggregator's. An aggregator
    priced per call bills that price for an empty answer too, so unmeasured it costs twice its
    price per hit; one priced per result charges nothing for an empty answer."""
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "icypeas")
    monkeypatch.setenv("TREG_PLATFORM_KEY_ICYPEAS", "ICYPEAS-KEY")
    monkeypatch.setenv("TREG_OVERFLOW_MODE", "on")
    get_settings.cache_clear()
    monkeypatch.setattr(call_route.capacity_view, "is_exhausted",
                        lambda provider, endpoint_id=None, slot=None, **_: provider == "icypeas")
    route = SimpleNamespace(aggregator="orthogonal", agg_price_micro=10_000, agg_unit="call")
    monkeypatch.setattr(call_route.overflow_routes_view, "for_endpoint",
                        lambda endpoint_id: [route] if endpoint_id == "icypeas.people.search" else [])
    caller = SimpleNamespace(org_id=1, org=SimpleNamespace(id=1, platform_overflow_disabled=False))
    plan = await call_route.build_plan(catalog_store.load().by_id["treg.people.search"],
                                       {"title": "CTO", "limit": 1}, caller,
                                       call_route.RouteOptions.from_headers(lambda key: None))
    icypeas = next(c for c in plan.candidates if c.endpoint["id"] == "icypeas.people.search")
    assert icypeas.hit_rate is None and icypeas.ok_rate is None
    assert icypeas.price_micro == icypeas.miss_micro == 10_000
    assert icypeas.expected_cost_per_hit == 20_000

    # A route priced per result reports no rows on a miss and charges nothing for it.
    route.agg_unit = "result"
    plan = await call_route.build_plan(catalog_store.load().by_id["treg.people.search"],
                                       {"title": "CTO", "limit": 1}, caller,
                                       call_route.RouteOptions.from_headers(lambda key: None))
    icypeas = next(c for c in plan.candidates if c.endpoint["id"] == "icypeas.people.search")
    assert icypeas.miss_micro == 0 and icypeas.expected_cost_per_hit == 10_000


async def test_routed_plan_keeps_an_exhausted_provider_when_its_overflow_route_is_enabled(
    clients: AsyncClient, monkeypatch,
):
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "akta,predictleads")
    monkeypatch.setenv("TREG_PLATFORM_KEY_AKTA", "AKTA-KEY")
    monkeypatch.setenv("TREG_PLATFORM_KEY_PREDICTLEADS", "PREDICTLEADS-KEY")
    monkeypatch.setenv("TREG_OVERFLOW_MODE", "on")
    monkeypatch.setenv("TREG_OVERFLOW_KEY_MONID", "MONID-KEY")
    get_settings.cache_clear()
    monkeypatch.setattr(call_route.capacity_view, "is_exhausted",
                        lambda provider, endpoint_id=None, slot=None, **_: provider == "akta")
    route = SimpleNamespace(aggregator="monid", agg_price_micro=10_000)
    monkeypatch.setattr(call_route.overflow_routes_view, "for_endpoint",
                        lambda endpoint_id: [route] if endpoint_id == "akta.companies.news" else [])

    class _Org:
        id = 1
        platform_overflow_disabled = False

    class _Caller:
        org_id = 1
        org = _Org()

    ep = catalog_store.load().by_id["treg.companies.news"]
    options = call_route.RouteOptions.from_headers(lambda key: None)
    plan = await call_route.build_plan(ep, {"domain": "canva.com", "limit": 1}, _Caller(), options)
    akta = next(c for c in plan.candidates if c.endpoint["id"] == "akta.companies.news")
    assert plan.candidates[0] is akta
    assert akta.price_micro == 10_000
    assert not akta.exhausted
    assert akta.note == "direct account exhausted; overflow via monid"
    assert not any(d["endpoint_id"] == "akta.companies.news" for d in plan.dropped)

    monkeypatch.setattr(call_route.overflow_routes_view, "for_endpoint", lambda endpoint_id: [])
    without_overflow = await call_route.build_plan(
        ep, {"domain": "canva.com", "limit": 1}, _Caller(), options)
    assert not any(c.endpoint["id"] == "akta.companies.news" for c in without_overflow.candidates)
    assert any(d["endpoint_id"] == "akta.companies.news" and "exhausted" in d["why"]
               for d in without_overflow.dropped)


async def test_routed_call_reaches_an_enabled_overflow_before_the_next_provider(
    clients: AsyncClient, platform_on, monkeypatch,
):
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "akta,predictleads")
    monkeypatch.setenv("TREG_PLATFORM_KEY_AKTA", "AKTA-KEY")
    monkeypatch.setenv("TREG_PLATFORM_KEY_PREDICTLEADS", "PREDICTLEADS-KEY")
    monkeypatch.setenv("TREG_OVERFLOW_MODE", "on")
    monkeypatch.setenv("TREG_OVERFLOW_KEY_MONID", "MONID-KEY")
    monkeypatch.setenv("TREG_OVERFLOW_DAILY_BUDGET_USD", "10")
    get_settings.cache_clear()
    async with session_maker() as db:
        db.add(OverflowRoute(
            endpoint_id="akta.companies.news", aggregator="monid", provider="akta",
            method="GET", path="/v1/news/", agg_slug="akta", agg_path="/v1/news",
            agg_price_micro=10_000, agg_unit="call", ratio=1, enabled=True,
        ))
        await db.commit()
    call_route.overflow_routes_view.invalidate()
    monkeypatch.setattr(call_route.capacity_view, "is_exhausted",
                        lambda provider, endpoint_id=None, slot=None, **_: provider == "akta")

    async def overflow_send(client, req):
        return httpx.Response(200, json={
            "runId": "run-akta-news",
            "status": "COMPLETED",
            "output": {"data": [{"title": "served through Monid"}], "total": 1, "count": 1,
                       "limit": 1, "offset": 0},
            "providerResponse": {"httpStatus": 200},
            "billing": {"reportedCost": {"value": 5_500, "unit": "MICRO_DOLLAR"}},
        })

    async def direct_relay_must_not_run(*args, **kwargs):
        raise AssertionError("the routed call must skip direct Akta and never reach PredictLeads")

    monkeypatch.setattr(call_overflow, "_send", overflow_send)
    monkeypatch.setattr(call_service, "relay", direct_relay_must_not_run)
    response = await clients.post("/call/treg.companies.news", json={"domain": "canva.com", "limit": 1})
    assert response.status_code == 200, response.text
    doc = response.json()
    assert doc["output"]["articles"] == [{"title": "served through Monid"}]
    assert doc["_treg"]["served_by"] == "akta.companies.news"
    assert doc["_treg"]["provider"] == "akta"
    assert doc["_treg"]["charged_micro"] == 5_500
    assert [attempt["endpoint_id"] for attempt in doc["_treg"]["tried"]] == ["akta.companies.news"]


# ---- the call path ---------------------------------------------------------------------------


async def test_routed_call_runs_the_cheapest_child_and_returns_output_raw_and_provenance(clients: AsyncClient, enrichment_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [(200, {"data": {"email": "patrick@stripe.com", "score": 99, "first_name": "Patrick", "last_name": "Collison",
                                    "verification": {"status": "valid"}}})]}, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["output"] == {"email": "patrick@stripe.com", "confidence": 0.99, "first_name": "Patrick", "last_name": "Collison", "verified": True}
    assert d["raw"]["data"]["score"] == 99, "the winning provider's body, verbatim"
    assert d["_treg"]["served_by"] == "tomba.people.email.find" and d["_treg"]["outcome"] == "hit"
    assert "advice" not in d["_treg"], "the provider vouched for the mailbox — nothing to add"
    assert r.headers["X-Treg-Served-By"] == "tomba.people.email.find" and r.headers["X-Treg-Providers-Tried"] == "tomba"
    assert seen == [("tomba", "GET", {"domain": "stripe.com", "full_name": "Patrick Collison"}, None)]
    charged = int(r.headers["X-Treg-Cost-Micro"])
    assert charged == 8_900 and before - await _balance(clients) == charged, "tomba's price, nothing else"
    assert d["_treg"]["charged_micro"] == charged
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []
        entries = (await db.execute(select(LedgerEntry))).scalars().all()
    assert {e.call_id for e in entries if e.kind == "settle"} == {r.headers["X-Treg-Call-Id"] + ":r0"}
    await audit.drain()
    rows = (await clients.get("/calls")).json()
    kinds = {(x["tool_name"], x.get("credential_tier")) for x in rows}
    assert (ROUTED, "routed") in kinds and ("tomba.people.email.find", "platform") in kinds


async def test_a_lone_surrogate_in_the_winning_body_is_relayed_as_its_escape(clients: AsyncClient, enrichment_on, monkeypatch):
    """Re-encoding the winner's parsed body with ensure_ascii=False raised on a lone surrogate after
    the child had answered: the caller got a 500 for a hit. The escape is the upstream's own text."""
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [(200, {"data": {"email": "pat@stripe.com", "score": 99, "first_name": "Pat \ud835",
                                    "last_name": "C", "verification": {"status": "valid"}}})]}, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Pat C", "domain": "stripe.com"})
    assert r.status_code == 200, r.text
    assert b'"first_name":"Pat \\ud835"' in r.content
    assert r.json()["output"]["first_name"] == "Pat \ud835"
    assert before - await _balance(clients) == int(r.headers["X-Treg-Cost-Micro"]) == 8_900


async def test_an_unverified_hit_carries_verify_advice(clients: AsyncClient, enrichment_on, monkeypatch):
    """A found address the provider did not vouch for (Tomba's verification status is not `valid`
    — the catch-all shape that bounced for a recruiting team on 2026-09-06) is still a HIT and
    still billed, but the answer says so in `_treg.advice` and points at the verify endpoint. A
    suggestion, not a chained call: the balance moves by the find alone."""
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [(200, {"data": {"email": "alan@pruittstructures.com", "score": 96,
                                    "verification": {"status": "accept_all"}}})]}, []))
    before = await _balance(clients)
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Alan Marquez", "domain": "pruittstructures.com"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["output"]["email"] == "alan@pruittstructures.com" and d["output"]["verified"] is False
    assert d["_treg"]["outcome"] == "hit"
    assert "treg.people.email.verify" in d["_treg"]["advice"]
    assert before - await _balance(clients) == int(r.headers["X-Treg-Cost-Micro"]) == 8_900, "the find, nothing chained"


async def test_off_domain_hit_sets_domain_match_false(clients: AsyncClient, enrichment_on, monkeypatch):
    """When the caller sent a domain and the child returns an address at another host (a LinkedIn-
    only finder returning a former employer), the hit is still billed and the address is unchanged,
    but `_treg.domain_match` / `X-Treg-Domain-Match` disclose the mismatch and advice names it."""
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [(200, {"data": {"email": "alan@othercorp.example", "score": 90,
                                    "verification": {"status": "valid"}}})]}, []))
    before = await _balance(clients)
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Alan Marquez", "domain": "pruittstructures.com"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["output"]["email"] == "alan@othercorp.example", "the child's answer is not rewritten"
    assert d["_treg"]["outcome"] == "hit" and d["_treg"]["domain_match"] is False
    assert r.headers["X-Treg-Domain-Match"] == "false"
    assert "domain_match" in d["_treg"]["advice"] and "different domain" in d["_treg"]["advice"]
    assert before - await _balance(clients) == int(r.headers["X-Treg-Cost-Micro"]) == 8_900


async def test_matching_domain_hit_sets_domain_match_true(clients: AsyncClient, enrichment_on, monkeypatch):
    """A hit whose mailbox host equals the domain the caller sent reports domain_match true; the
    unverified advice still attaches when the provider did not vouch for deliverability."""
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [(200, {"data": {"email": "alan@pruittstructures.com", "score": 96,
                                    "verification": {"status": "accept_all"}}})]}, []))
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Alan Marquez", "domain": "pruittstructures.com"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["_treg"]["domain_match"] is True and r.headers["X-Treg-Domain-Match"] == "true"
    assert "treg.people.email.verify" in d["_treg"]["advice"], "match does not suppress unverified advice"


async def test_linkedin_only_find_omits_domain_match(clients: AsyncClient, enrichment_on, monkeypatch):
    """No domain in the request means no domain_match field — there is nothing to compare."""
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"findymail": [(200, {"contact": {"name": "Patrick Collison",
                                           "email": "patrick@stripe.com"}})],
         "*": [(404, {})] * 8}, []))
    r = await clients.post(f"/call/{ROUTED}",
                           json={"linkedin_url": "https://www.linkedin.com/in/patrickcollison"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["output"]["email"] == "patrick@stripe.com"
    assert "domain_match" not in d["_treg"] and "X-Treg-Domain-Match" not in r.headers


async def test_a_people_search_hit_always_carries_verify_advice(clients: AsyncClient, enrichment_on, monkeypatch):
    """Search rows are directory listings: a row's email is found, not confirmed deliverable. The
    contract has no `verified` output, so the advice attaches to every hit — Hunter domain-search
    rows with `verification: null` were 73 of one team's 79 bounces (2026-09-08). Still a
    suggestion: one child call, the find's price, nothing chained."""
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"hunter": [(200, {"data": {"emails": [{"value": "info@royalfarms.com", "type": "generic", "confidence": 10,
                                                 "verification": {"date": None, "status": None}}]},
                            "meta": {"results": 1}})],
         "*": [(200, {"persons": []})] * 12}, []))
    before = await _balance(clients)
    r = await clients.post("/call/treg.people.search", json={"company_domain": "royalfarms.com", "limit": 10})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["_treg"]["served_by"] == "hunter.companies.emails" and d["_treg"]["outcome"] == "hit"
    row = d["output"]["people"][0]
    assert row["email"] == "info@royalfarms.com" and "email_status" not in row, "no verdict from the provider, none claimed"
    assert d["raw"]["data"]["emails"][0]["verification"]["status"] is None, "the provider's row, untouched in raw"
    assert "treg.people.email.verify" in d["_treg"]["advice"] and "directory" in d["_treg"]["advice"]
    assert before - await _balance(clients) == int(r.headers["X-Treg-Cost-Micro"]), "the find, nothing chained"


async def test_error_on_the_first_child_falls_back_to_the_second(clients: AsyncClient, enrichment_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [(503, {"error": "down"})],
         "findymail": [(200, {"contact": {"name": "Patrick Collison", "email": "patrick@stripe.com"}})]}, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert [t["outcome"] for t in d["_treg"]["tried"]] == ["error", "hit"]
    assert d["_treg"]["served_by"] == "findymail.search.name" and d["output"]["email"] == "patrick@stripe.com"
    assert r.headers["X-Treg-Providers-Tried"] == "tomba,findymail"
    assert before - await _balance(clients) == 19_800, "the failed child released its hold; only findymail charged"
    assert seen[1] == ("findymail", "POST", {}, {"name": "Patrick Collison", "domain": "stripe.com"})


async def test_child_capability_pin_refusal_falls_back_to_the_pinned_provider(
    clients: AsyncClient, enrichment_on, monkeypatch,
):
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    pinned = await clients.post(
        f"/orgs/{org_id}/pins",
        json={"capability": "people.email.find", "provider": "hunter"},
    )
    assert pinned.status_code == 200, pinned.text
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({
        "hunter": [(200, {"data": {
            "email": "patrick@stripe.com", "score": 90,
            "verification": {"status": "valid"},
        }})],
    }, seen))

    response = await clients.post(
        f"/call/{ROUTED}",
        json={"full_name": "Patrick Collison", "domain": "stripe.com"},
        headers={"X-Treg-Route-Prefer": "tomba,hunter"},
    )

    assert response.status_code == 200, response.text
    doc = response.json()
    assert doc["_treg"]["served_by"] == "hunter.people.email.find"
    assert [attempt["outcome"] for attempt in doc["_treg"]["tried"]] == ["error", "hit"]
    assert doc["_treg"]["tried"][0]["endpoint_id"] == "tomba.people.email.find"
    assert [provider for provider, *_ in seen] == ["hunter"]


async def test_platform_vendor_401_falls_back_to_the_next_provider(
    clients: AsyncClient, enrichment_on, monkeypatch,
):
    findymail = catalog_store.load().by_id["findymail.search.name"]
    monkeypatch.setitem(findymail, "cost", {**findymail["cost"], "type": "per_call"})
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({
        "tomba": [(401, {"error": "invalid platform key"})],
        "findymail": [(200, {"contact": {
            "name": "Patrick Collison", "email": "patrick@stripe.com",
        }})],
    }, seen))

    response = await clients.post(
        f"/call/{ROUTED}",
        json={"full_name": "Patrick Collison", "domain": "stripe.com"},
        headers={
            "X-Treg-Route-Prefer": "tomba,findymail",
            "X-Treg-Route-Exclude": "hunter,leadmagic,leadsforge,aviato,fiber-ai",
        },
    )

    assert response.status_code == 200, response.text
    doc = response.json()
    assert doc["_treg"]["served_by"] == "findymail.search.name"
    assert [attempt["outcome"] for attempt in doc["_treg"]["tried"]] == ["error", "hit"]
    assert [provider for provider, *_ in seen] == ["tomba", "findymail"]


async def test_routed_insufficient_balance_still_stops_before_fallback(
    clients: AsyncClient, enrichment_on, monkeypatch,
):
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        await ledger.reserve(db, org_id, "drain routed balance", 1_000_000)
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({
        "tomba": [(200, {"data": {"email": "should-not-run@example.com"}})],
    }, seen))

    response = await clients.post(
        f"/call/{ROUTED}",
        json={"full_name": "Patrick Collison", "domain": "stripe.com"},
        headers={"X-Treg-Route-Prefer": "tomba,findymail"},
    )

    assert response.status_code == 402
    assert response.json()["detail"]["error"] == "insufficient_balance"
    assert seen == []


def _money_queue_busy_after(monkeypatch, admitted: int) -> None:
    """The team's money queue admits `admitted` reserves, then times out every later one."""
    from contextlib import asynccontextmanager

    from treg.infra import money_admission

    real, reserves = money_admission.admit, []

    def admit(orgs, *, operation, **kwargs):
        if operation != "reserve":
            return real(orgs, operation=operation, **kwargs)
        reserves.append(1)
        if len(reserves) <= admitted:
            return real(orgs, operation=operation, **kwargs)

        @asynccontextmanager
        async def refuse():
            raise money_admission.ReserveAdmissionTimeout("wait_timeout")
            yield  # pragma: no cover - makes this an async context manager
        return refuse()
    monkeypatch.setattr(money_admission, "admit", admit)


async def test_routed_busy_money_queue_is_treg_refusal_not_a_provider_failure(
    clients: AsyncClient, enrichment_on, monkeypatch,
):
    _money_queue_busy_after(monkeypatch, 0)
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({
        "tomba": [(200, {"data": {"email": "should-not-run@example.com"}})],
    }, seen))
    before = await _balance(clients)

    response = await clients.post(
        f"/call/{ROUTED}",
        json={"full_name": "Patrick Collison", "domain": "stripe.com"},
        headers={"X-Treg-Route-Prefer": "tomba,findymail"},
    )

    assert response.status_code == 429, response.text
    assert response.json()["detail"]["error"] == "money_admission_timeout"
    assert int(response.headers["Retry-After"]) >= 1
    assert seen == [], "no provider is asked against the same busy queue"
    assert await _balance(clients) == before


async def test_routed_refusal_after_a_paid_hit_keeps_the_answer(
    clients: AsyncClient, enrichment_on, monkeypatch,
):
    _money_queue_busy_after(monkeypatch, 1)
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({
        "tomba": [(200, {"data": {"email": "patrick@stripe.com", "score": 99,
                                  "verification": {"status": "valid"}}})],
    }, seen))

    response = await clients.post(
        f"/call/{ROUTED}",
        json={"full_name": "Patrick Collison", "domain": "stripe.com"},
        headers={"X-Treg-Route-Prefer": "tomba,hunter", "X-Treg-Route-Confirm": "true"},
    )

    assert response.status_code == 200, response.text
    doc = response.json()
    assert doc["output"]["email"] == "patrick@stripe.com"
    assert doc["_treg"]["served_by"] == "tomba.people.email.find"
    assert doc["_treg"]["confirmation"] == {"field": "email", "confirmed": False, "confirmed_by": None}
    assert [(t["outcome"], t["provider"]) for t in doc["_treg"]["tried"]] == [("hit", "tomba"), ("skipped", "hunter")]
    assert doc["_treg"]["tried"][1]["detail"] == "refused by treg: money_admission_timeout"
    assert int(response.headers["X-Treg-Cost-Micro"]) == doc["_treg"]["charged_micro"] > 0
    assert [provider for provider, *_ in seen] == ["tomba"]


async def test_waterfall_is_on_by_default_can_be_turned_off_and_respects_max_cost(clients: AsyncClient, enrichment_on, monkeypatch):
    miss_tomba = (200, {"data": {"email": None, "score": None, "verification": {"status": None}}})
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"tomba": [miss_tomba]}, seen))
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Nobody Here", "domain": "stripe.com"},
                           headers={"X-Treg-Route-Waterfall": "0"})
    assert r.status_code == 200 and r.json()["_treg"]["outcome"] == "miss" and r.json()["output"]["email"] is None
    assert r.headers["X-Treg-Route-Outcome"] == "miss" and len(seen) == 1, "waterfall off: stop at the first miss"
    # waterfall (the default): miss → next cheapest → hit; skips a candidate that would breach the ceiling
    seen.clear()
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [miss_tomba], "findymail": [(200, {"contact": {"name": "N H", "email": None}})],
         "hunter": [(200, {"data": {"email": "n@stripe.com", "score": 50, "verification": {"status": "valid"}}})]}, seen))
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Nobody Here", "domain": "stripe.com"},
                           headers={"X-Treg-Route-Max-Cost": "0.08"})
    assert r.status_code == 200, r.text
    tried = r.json()["_treg"]["tried"]
    assert [t["outcome"] for t in tried] == ["miss", "miss", "hit"] and r.json()["_treg"]["served_by"] == "hunter.people.email.find"
    assert [p for p, *_ in seen] == ["tomba", "findymail", "hunter"]
    assert r.json()["_treg"]["charged_micro"] == 24_500, "misses on per-success providers are free; only the hit is billed"
    assert [t["charged_micro"] for t in tried] == [0, 0, 24_500]
    # a ceiling the third candidate would breach stops the waterfall there
    seen.clear()
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [miss_tomba], "findymail": [(200, {"contact": {"name": "N H", "email": None}})],
         "hunter": [(200, {"data": {"email": None, "score": None}})]}, seen))
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Nobody Here", "domain": "stripe.com"},
                           headers={"X-Treg-Route-Max-Cost": "0.02"})
    assert r.status_code == 200 and r.json()["_treg"]["outcome"] == "miss"
    # free misses do not consume the ceiling, but hunter (2.45¢ > 2¢) and everything dearer is skipped
    assert [p for p, *_ in seen] == ["tomba", "findymail"]
    assert all(t["outcome"] in ("miss", "skipped") for t in r.json()["_treg"]["tried"]) and r.json()["_treg"]["charged_micro"] == 0


async def test_a_ceiling_at_the_catalog_price_skips_a_child_whose_hold_carries_margin(
        clients: AsyncClient, enrichment_on, monkeypatch):
    """The child holds its price plus margin, so a ceiling set to the bare price cannot fit it: the
    route skips it before any byte goes upstream and says what it would hold and what was left."""
    monkeypatch.setattr(get_settings(), "platform_margin", 0.25)
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"*": [(200, {})] * 6}, seen))
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Nobody Here", "domain": "stripe.com"},
                           headers={"X-Treg-Route-Max-Cost": "0.0245", "X-Treg-Route-Prefer": "hunter"})
    hunter = next(t for t in r.json()["_treg"]["tried"] if t["provider"] == "hunter")
    assert hunter["outcome"] == "skipped" and "hunter" not in [p for p, *_ in seen]
    assert "would hold $0.030625" in hunter["detail"] and "$0.0245 left" in hunter["detail"]


async def test_max_cost_below_the_cheapest_refuses_before_any_call(clients: AsyncClient, enrichment_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"tomba": [(200, {})]}, seen))
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "P C", "domain": "stripe.com"}, headers={"X-Treg-Route-Max-Cost": "0.001"})
    assert r.status_code == 402 and r.json()["detail"]["error"] == "route_max_cost" and seen == []
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []


async def test_identity_no_provider_accepts_is_422_naming_variants(clients: AsyncClient, enrichment_on):
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison"})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "identity_incomplete"
    assert ["domain", "full_name"] in r.json()["detail"]["variants"]


async def test_identity_not_of_its_declared_type_is_422_before_any_call(clients: AsyncClient, enrichment_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"tomba": [(200, {})]}, seen))
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "P C", "domain": "stripe.com", "linkedin_url": "N/A"})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "invalid_identity" and seen == []
    assert r.json()["detail"]["invalid"] == [{"field": "linkedin_url", "type": "url", "value": "N/A"}]


async def test_access_check_plans_a_url_row_from_its_own_example(clients: AsyncClient):
    """The type check guards what a caller sends; the access view plans from the row's placeholder."""
    r = await clients.get("/catalog/endpoints/treg.web.extract/access")
    assert r.status_code == 200, r.text


async def test_caller_fault_on_a_child_stops_and_own_key_ranks_first(clients: AsyncClient, enrichment_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"hunter": [(200, {"data": {"email": "p@stripe.com", "score": 90, "verification": {"status": "valid"}}})]}, seen))
    await clients.post("/secrets", json={"name": "hunter", "value": "MY-HUNTER-KEY"})  # tier 2 for hunter
    before = await _balance(clients)
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"})
    assert r.status_code == 200 and r.json()["_treg"]["served_by"] == "hunter.people.email.find"
    assert r.json()["_treg"]["tier"] == "credential" and await _balance(clients) == before, "own key: first, and free"
    # a vendor 4xx on the child goes on ONLY to providers that bill nothing for a rejected request
    # (per_success / free): tomba is per_success, so it is asked; when it rejects too, the caller gets
    # route_caller_fault naming both — and no paid-per-call provider was ever asked.
    seen.clear()
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"hunter": [(400, {"errors": [{"details": "bad"}]})], "tomba": [(400, {"error": "bad"})], "*": [(400, {"error": "bad"})]}, seen))
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "route_caller_fault", r.text
    assert [p for p, *_ in seen][:2] == ["hunter", "tomba"] and len(seen) == 3, "at most two fallbacks, then the 4xx is the caller's"
    outcomes = {t["endpoint_id"]: t["outcome"] for t in r.json()["detail"]["tried"]}
    assert outcomes["hunter.people.email.find"] == "error" and outcomes["tomba.people.email.find"] == "error"
    # a scraper's "please retry" 400 (tikhub, live 2026-08-28) is why: the next free-on-failure provider answers
    seen.clear()
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"hunter": [(400, {"errors": [{"details": "bad"}]})],
         "tomba": [(200, {"data": {"email": "p@stripe.com", "score": 90, "verification": {"status": "valid"}}})]}, seen))
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"})
    assert r.status_code == 200 and r.json()["_treg"]["served_by"] == "tomba.people.email.find", r.text


async def test_a_2xx_without_the_required_core_is_a_miss_not_a_hit(clients: AsyncClient, enrichment_on, monkeypatch):
    """A 200 whose body lacks the contract's required field (a null result under a success envelope)
    is a MISS: the waterfall goes on, and the verdict/hit-rate never counts it as answered."""
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"hunter": [(200, {"data": {"email": None, "score": None}})],
         "tomba": [(200, {"data": {"email": "p@stripe.com", "score": 90, "verification": {"status": "valid"}}})]}, seen))
    await clients.post("/secrets", json={"name": "hunter", "value": "MY-HUNTER-KEY"})
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"})
    assert r.status_code == 200, r.text
    outcomes = {t["endpoint_id"]: t["outcome"] for t in r.json()["_treg"]["tried"]}
    assert outcomes["hunter.people.email.find"] == "miss" and r.json()["_treg"]["served_by"] == "tomba.people.email.find"


async def test_catalog_get_on_the_routed_endpoint_shows_the_plan(clients: AsyncClient, enrichment_on):
    r = await clients.get(f"/catalog/endpoints/{ROUTED}")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["endpoint"]["kind"] == "routed" and d["routing"]["contract"]["identity"]
    # the same job from unrouted providers is named here too — the search page points at this row
    also = {a["endpoint_id"] for a in d["routing"]["also"]}
    assert also.isdisjoint(d["endpoint"]["routed_children"]) and all(i.endswith("email.find") or "." in i for i in also)
    plan = d["routing"]["plan"]
    assert plan and plan[0]["usd"] <= plan[-1]["usd"] and plan[0]["accepts"]
    assert "hit_rate" not in plan[0], "unmeasured says nothing rather than nulls"
    assert {c["endpoint_id"] for c in plan} <= set(d["endpoint"]["routed_children"] if "routed_children" in d["endpoint"] else [c["endpoint_id"] for c in plan])


async def test_the_routed_quote_ranks_the_children_the_router_skips_last(clients: AsyncClient, enrichment_on, monkeypatch):
    """The quote and the call agree: a child treg's key cannot serve (here `platform_blocked`)
    stays in the plan for its `accepts`, after every child the router would call, saying why."""
    from treg.domain.catalog import store as catalog_store
    monkeypatch.setitem(catalog_store.load().by_id["tomba.people.email.find"], "platform_blocked", "test")
    plan = (await clients.get(f"/catalog/endpoints/{ROUTED}")).json()["routing"]["plan"]
    skipped = [c.get("skipped") is not None for c in plan]
    assert skipped == sorted(skipped) and any(skipped) and not skipped[0]
    tomba = next(c for c in plan if c["endpoint_id"] == "tomba.people.email.find")
    assert tomba["skipped"] == "not platform-eligible and no own key" and tomba["accepts"]


async def test_idempotent_replay_of_a_routed_call_never_calls_a_provider_twice(clients: AsyncClient, enrichment_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [(200, {"data": {"email": "p@stripe.com", "score": 90, "verification": {"status": "valid"}}})]}, seen))
    h = {"Idempotency-Key": "route-1"}
    r1 = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"}, headers=h)
    r2 = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"}, headers=h)
    assert r1.status_code == 200 and r2.status_code == 200 and r2.headers.get("X-Treg-Idempotent-Replay") == "true"
    assert r2.json() == r1.json() and len(seen) == 1


async def test_treg_accounts_used_up_allowance_is_skipped_not_the_routes_error(
    clients: AsyncClient, enrichment_on, monkeypatch,
):
    """Live 2026-11-04: every title-capable provider answered "nobody" but one, whose answer on
    treg's account was LeadOcean's 429 "Fair-use pace reached"; that one error turned the caller's
    honest no-match into a 502 `route_failed`. treg's own account being used up says nothing about
    the question: the attempt is skipped, and the call ends as the 200 miss the others answered."""
    cat = catalog_store.load()
    provs = sorted({cat.by_id[e]["provider"] for e in cat.adapters
                    if cat.by_id.get(e, {}).get("capability") == "people.search"})
    for p in provs:
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{p.upper().replace('-', '_')}", "PLATFORM-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", ",".join(provs))
    get_settings.cache_clear()
    pace = (429, {"success": False, "error": {"message": "Fair-use pace reached — this account has received "
                                                          "50,000 search records in the current period"}})
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"leadocean": [pace] * 4, "*": [(200, {})] * 40}, seen))
    r = await clients.post("/call/treg.people.search",
                           json={"company_domain": "stjosephacademy.com", "title": "librarian", "limit": 2},
                           headers={"X-Treg-Route-Prefer": "leadocean"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["_treg"]["outcome"] == "miss"
    lead = [t for t in d["_treg"]["tried"] if t["provider"] == "leadocean"]
    assert lead and lead[0]["outcome"] == "skipped" and "used up" in lead[0].get("detail", "")
    assert all(t["outcome"] in ("miss", "skipped") for t in d["_treg"]["tried"])
    get_settings.cache_clear()


async def test_a_miss_with_a_provider_down_on_the_way_is_an_incomplete_miss(
    clients: AsyncClient, enrichment_on, monkeypatch,
):
    """tomba answers (a billed miss) and leadmagic is down. One provider's outage does not turn the
    other's honest answer into a 502 `route_failed`: the call is a 200 miss marked incomplete,
    charged for tomba's miss as any miss is. (A routed call that does fail, where no provider
    answered, still charges nothing: owner decision 2026-09-21,
    test_adapter_throws_on_all_children_returns_structured_502_with_tried.)"""
    routed = "treg.people.email.verify"
    tomba_miss = (200, {"data": {"email": {"status": None, "score": None}}})
    down = (503, {"message": "provider down"})
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [tomba_miss], "leadmagic": [down]}, seen))
    headers = {
        "Idempotency-Key": "incomplete-miss",
        "X-Treg-Route-Prefer": "tomba,leadmagic",
        "X-Treg-Route-Exclude": "hunter",
    }
    before = await _balance(clients)

    r1 = await clients.post(f"/call/{routed}", json={"email": "bad@example.com"}, headers=headers)

    assert r1.status_code == 200, r1.text
    doc = r1.json()["_treg"]
    assert doc["outcome"] == "miss" and doc["incomplete"] is True
    assert r1.headers["X-Treg-Route-Incomplete"] == "true"
    assert [(t["provider"], t["outcome"], t["status"]) for t in doc["tried"]] == [
        ("tomba", "miss", 200), ("leadmagic", "error", 503)]
    assert doc["charged_micro"] == 8_900 == before - await _balance(clients)
    assert [provider for provider, *_ in seen] == ["tomba", "leadmagic"]
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []


async def test_a_400_after_another_provider_answered_is_that_providers_own_rejection(
    clients: AsyncClient, enrichment_on, monkeypatch,
):
    """Live 2026-09-23: prospeo's 400 after two providers had answered ended a people.search as the
    caller's 400 and threw away rows the caller had paid for. When another provider already
    answered the same question, the question is valid: the 400 is recorded as `rejected` and the
    call ends on what the others said (here a 200 miss, charged as a miss is)."""
    routed = "treg.people.email.verify"
    tomba_miss = (200, {"data": {"email": {"status": None, "score": None}}})
    bad = (400, {"message": "invalid email"})
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [tomba_miss], "leadmagic": [bad]}, seen))
    headers = {"X-Treg-Route-Prefer": "tomba,leadmagic", "X-Treg-Route-Exclude": "hunter"}
    before = await _balance(clients)

    r = await clients.post(f"/call/{routed}", json={"email": "bad@example.com"}, headers=headers)

    assert r.status_code == 200
    body = r.json()["_treg"]
    assert body["outcome"] == "miss"
    assert [a["outcome"] for a in body["tried"]] == ["miss", "rejected"]
    assert before - await _balance(clients) == 8_900 == int(r.headers["X-Treg-Cost-Micro"])


def test_a_per_success_miss_settles_at_zero_when_the_adapter_can_tell():
    """Live 2026-08-28: the first waterfall charged tomba, findymail and leadsforge for misses the
    catalog calls free. The adapter's `miss` predicate is the missing knowledge."""
    from test_marketplace_call import _mk
    from treg.application.call import settle as A
    miss_tomba = b'{"data": {"email": null, "score": null, "first_name": "Z", "verification": {"status": null}}}'
    assert A._observed_cost_micro(_mk("tomba", endpoint_id="tomba.people.email.find", cost_type="per_success"), miss_tomba) == 0
    hit_tomba = b'{"data": {"email": "z@x.io", "score": 90}}'
    assert A._observed_cost_micro(_mk("tomba", endpoint_id="tomba.people.email.find", cost_type="per_success"), hit_tomba) is None, "a hit still settles at the estimate"
    assert A._observed_cost_micro(_mk("findymail", endpoint_id="findymail.search.name", cost_type="per_success"), b'{"contact": {"email": null}}') == 0
    assert A._observed_cost_micro(_mk("leadsforge", endpoint_id="leadsforge.people.email.find", cost_type="per_success"), b'{"email": null, "status": "failed"}') == 0
    assert A._observed_cost_micro(_mk("leadsforge", endpoint_id="leadsforge.people.email.find", cost_type="per_call"), b'{"email": null}') is None, "per_call bills the call"
    assert A._observed_cost_micro(_mk("tomba", endpoint_id="tomba.companies.emails.count", cost_type="per_success"), b'{"data": {}}') is None, "no adapter → no opinion"


async def test_discovery_puts_the_routed_parent_first_and_its_children_under_it(clients: AsyncClient):
    r = await clients.get("/catalog/search", params={"q": "find work email"})
    rows = r.json()["results"]
    ids = [x["id"] for x in rows]
    parent = ids.index(ROUTED)
    kids = [i for i, x in enumerate(rows) if x["capability"] == "people.email.find" and x["id"] != ROUTED]
    assert kids and parent < min(kids), "the routed parent leads its capability group"
    assert kids == list(range(parent + 1, parent + 1 + len(kids))), "children sit right under the parent"
    assert rows[parent]["routed_children"] and any("ROUTED" in h for h in r.json()["hints"])
    p = await clients.get("/catalog/platforms/people")
    group = next(c for c in p.json()["capabilities"] if c["id"] == "people.email.find")
    assert group["endpoints"][0]["id"] == ROUTED
    from treg.domain.catalog.store import group_routed
    plain = [{"id": "a", "capability": "x", "kind": "data"}, {"id": "b", "capability": "y", "kind": "data"}]
    assert group_routed(plain) == plain, "no routed row → order untouched"
    from treg import mcp as M
    out = await M._catalog_search_impl("find work email", 12, surface=M._TEAM_SURFACE)
    mcp_ids = [row["endpoint_id"] for row in out["results"]]
    assert out["results"][mcp_ids.index(ROUTED)]["routed"].startswith("treg picks among"), "MCP search labels the routed parent"


async def test_hit_verdict_is_recorded_and_becomes_a_hit_rate(clients: AsyncClient, enrichment_on, monkeypatch):
    from treg.domain.catalog import stats
    from treg.models import CallRecord
    hit = (200, {"data": {"email": "p@stripe.com", "score": 90, "verification": {"status": "valid"}}})
    miss = (200, {"data": {"email": None, "score": None, "verification": {"status": None}}})
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"tomba": [hit, miss, hit]}, seen))
    for _ in range(3):
        assert (await clients.get("/call/tomba.people.email.find?full_name=P%20C&domain=stripe.com")).status_code == 200
    await audit.drain()
    async with session_maker() as db:
        rows = (await db.execute(select(CallRecord).where(CallRecord.endpoint_id == "tomba.people.email.find"))).scalars().all()
        assert sorted(r.hit for r in rows) == [False, True, True], "the verdict, never the body"
        # below the floor → None; the floor is about evidence, not a bug
        assert (await stats.observed(db, ["tomba.people.email.find"]))["tomba.people.email.find"]["hit_rate"] is None
        monkeypatch.setattr(stats, "MIN_HIT_SAMPLES", 3)
        s = (await stats.observed(db, ["tomba.people.email.find"], per_success={"tomba.people.email.find"}))["tomba.people.email.find"]
        assert s["hit_rate"] == pytest.approx(2 / 3, abs=1e-3) and s["hit_samples"] == 3
        # historical rows without a verdict: a per-success 2xx with cost_observed 0 is a miss, > 0 a hit
        for r in rows:
            r.hit = None
            r.cost_observed_micro = 8_900 if r.status_code == 200 and "x" else 0
        rows[0].cost_observed_micro = 0
        await db.commit()
        s = (await stats.observed(db, ["tomba.people.email.find"], per_success={"tomba.people.email.find"}))["tomba.people.email.find"]
        assert s["hit_samples"] == 3 and s["hit_rate"] == pytest.approx(2 / 3, abs=1e-3)
        s = (await stats.observed(db, ["tomba.people.email.find"]))["tomba.people.email.find"]
        assert s["hit_samples"] == 0, "the zero-cost fallback applies to per-success endpoints only"
    # the plan reads it: with a measured hit rate the confidence flips from unmeasured to measured
    monkeypatch.setattr(stats, "MIN_HIT_SAMPLES", 3)
    # the catalog reads observations through the process cache: a cold entry answers nothing and
    # refreshes in the background, so warm it the way test_endpoint_stats does
    from treg import api as A
    monkeypatch.setattr(call_route, "_endpoint_observation_reader", A.app.state.endpoint_observation_reader)
    assert (await catalog_stats.refresh(session_maker, now=utcnow_naive() + timedelta(minutes=2)))["caught_up"]
    await clients.get(f"/catalog/endpoints/{ROUTED}")
    await A.app.state.endpoint_observation_reader.wait_for_idle()
    r = await clients.get(f"/catalog/endpoints/{ROUTED}")
    tomba = next(c for c in r.json()["routing"]["plan"] if c["endpoint_id"] == "tomba.people.email.find")
    assert tomba["hit_rate"] == pytest.approx(2 / 3, abs=1e-3) and tomba["usd_per_hit"] == pytest.approx(0.0089, abs=1e-4)


async def test_a_registered_tool_for_a_provider_ranks_first_and_is_free(clients: AsyncClient, enrichment_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"hunter": [(200, {"data": {"email": "p@stripe.com", "score": 90, "verification": {"status": "valid"}}})]}, seen))
    sid = (await clients.post("/secrets", json={"name": "my-hunter", "value": "OWN-HUNTER"})).json()["id"]
    r = await clients.post("/tools", json={"name": "our-hunter", "base_url": "https://api.hunter.io/v2", "secret_id": sid})
    assert r.status_code == 200, r.text
    before = await _balance(clients)
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"})
    assert r.status_code == 200, r.text
    assert r.json()["_treg"]["served_by"] == "hunter.people.email.find" and r.json()["_treg"]["tier"] == "tool"
    assert await _balance(clients) == before and r.json()["_treg"]["charged_micro"] == 0


def test_filters_reach_adapters_through_in_expr_and_array_bodies():
    cat = catalog_store.load()
    contract = cat.contracts["google.keywords.ideas"]
    req, variant = canonical_identity(contract, {"keyword": "coffee"})
    assert variant == ("keyword",) and req["country"] == "us" and req["limit"] == 20, "filter defaults ride with the identity"
    req, _ = canonical_identity(contract, {"keyword": "coffee", "country": "GB", "limit": 5})
    q, b = cat.adapters["dataforseo.google.keywords.ideas"].to_upstream(req)
    assert b == [{"keyword": "coffee", "location_code": 2826, "language_code": "en", "limit": 5}], "task list body, GB → 2826"
    q, b = cat.adapters["seranking.google.keywords.ideas"].to_upstream(req)
    assert q == {"keyword": "coffee", "source": "uk", "limit": "5"}
    q, b = cat.adapters["serpapi.google.keywords.ideas"].to_upstream(req)
    assert q == {"q": "coffee", "gl": "gb", "hl": "en", "engine": "google_autocomplete"}
    q, b = cat.adapters["tomba.people.email.verify"].to_upstream({"email": "a@b.io"})
    assert q == {"email": "a@b.io"}, "Tomba verification requires the email query parameter"
    assert cat.by_id["tomba.people.email.verify"]["path"] == "/v1/email-verifier"
    assert cost_at({"usd": 0.00179, "type": "per_result", "per": 1}, req) == 8_950, "priced at the requested limit"
    ep = cat.by_id["treg.google.keywords.ideas"]
    assert ep["input"]["body"]["country"]["note"].startswith("filter; default 'us'")


def test_a_contract_may_set_its_own_default_ceiling():
    from treg.application.call.route import RouteOptions, DEFAULT_MAX_COST_MICRO
    cat = catalog_store.load()
    assert cat.contracts["people.search"].default_max_cost_usd is None, "the $1 default covers every current ladder"
    assert RouteOptions.from_headers(lambda k: None, 500_000).max_cost_micro == 500_000
    assert RouteOptions.from_headers(lambda k: None).max_cost_micro == DEFAULT_MAX_COST_MICRO
    assert RouteOptions.from_headers(lambda k: "0.02" if k == "x-treg-route-max-cost" else None, 500_000).max_cost_micro == 20_000


async def test_routed_call_and_access_name_the_providers_dropped_for_this_deployment(clients: AsyncClient, enrichment_on, monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_AVIATO", "")
    get_settings.cache_clear()
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [(200, {"data": {"email": None, "score": None, "verification": {"status": None}}})],
         "findymail": [(200, {"contact": {"name": "x", "email": None}})],
         "leadsforge": [(200, {"email": None, "status": "failed"})]}, seen))
    r = await clients.post(f"/call/{ROUTED}", json={"linkedin_url": "https://www.linkedin.com/in/x"}, headers={"X-Treg-Route-Max-Cost": "0.03"})
    assert r.status_code == 200 and r.json()["_treg"]["outcome"] == "miss"
    dropped = r.json()["_treg"]["dropped"]
    assert any(d["endpoint_id"] == "aviato.people.email.find" and "no aviato key" in d["why"] for d in dropped)
    hunter = next(d for d in dropped if d["endpoint_id"] == "hunter.people.email.find")
    assert hunter == {"endpoint_id": "hunter.people.email.find", "why": "needs {domain, full_name} | {domain, first_name, last_name}"}
    a = await clients.get(f"/catalog/endpoints/{ROUTED}/access")
    assert a.status_code == 200 and a.json()["tier"] == "routed" and a.json()["detail"].startswith("routed — ")
    assert "aviato.people.email.find" in a.json()["detail"]


def test_a_caller_may_send_everything_it_knows_and_each_provider_gets_only_its_variant():
    cat = catalog_store.load()
    contract = cat.contracts["people.phone.find"]
    everything = {"email": "p@stripe.com", "linkedin_url": "https://www.linkedin.com/in/p", "full_name": "Patrick Collison", "domain": "stripe.com"}
    ident, variant = canonical_identity(contract, everything)
    assert ident["first_name"] == "Patrick"
    from treg.domain.catalog.routing.contracts import adapter_accepts
    tomba = cat.adapters["tomba.people.phone.find"]
    v = adapter_accepts(tomba, ident)
    q, b = tomba.to_upstream(ident, v)
    assert q == {"email": "p@stripe.com"}, "tomba insists on exactly one identifier — only the matched variant is sent"
    lf = cat.adapters["leadsforge.people.phone.find"]
    q, b = lf.to_upstream(ident, adapter_accepts(lf, ident))
    assert b == {"firstName": "Patrick", "lastName": "Collison", "companyDomain": "stripe.com"}, "derived names, and not the LinkedIn URL"
    # filters always travel, whatever the variant
    kw = cat.contracts["google.keywords.ideas"]
    req, v = canonical_identity(kw, {"keyword": "coffee", "country": "de"})
    q, b = cat.adapters["seranking.google.keywords.ideas"].to_upstream(req, v)
    assert q == {"keyword": "coffee", "source": "de", "limit": "20"}
    # every adapter still verifies with the change
    assert all(a.verified for a in cat.adapters.values())


def test_rank_prefers_the_candidate_that_uses_more_of_the_identity():
    """Given {company_domain, title}, a title-aware provider outranks a cheaper domain-only one —
    the cheaper answer would be to a different question (the whole company)."""
    from treg.domain.catalog.routing.plan import Candidate, rank
    def cand(eid, variant, price):
        return Candidate(endpoint={"id": eid, "provider": eid.split(".")[0], "cost": {"type": "per_result"}}, adapter=None,
                         variant=variant, tier="platform", price_micro=price, hit_rate=None, ok_rate=None, p50_ms=None, last_ok_days=None)
    free_domain = cand("hunter.x.multi-domain-search", ("company_domain",), 0)
    title_aware = cand("icypeas.people.search", ("company_domain", "title"), 380)
    dearer_title = cand("companyenrich.people.search", ("company_domain", "title"), 19_600)
    given = {"company_domain", "title"}
    assert [c.endpoint["id"] for c in rank([free_domain, dearer_title, title_aware], given=given)] == [
        "icypeas.people.search", "companyenrich.people.search", "hunter.x.multi-domain-search"]
    # a key the caller did NOT send (reached via derive) earns nothing: price decides again
    assert rank([free_domain, title_aware], given={"company_domain"})[0] is free_domain
    # …but a variant DERIVED from what the caller sent covers it: {first,last,domain} from a supplied
    # full_name is as specific as {full_name, domain}, so the cheaper of the two (hunter) leads
    derive = {"first_name": "split_first(full_name)", "last_name": "split_last(full_name)"}
    hunter = cand("hunter.people.email.find", ("first_name", "last_name", "domain"), 4_900)
    apollo = cand("apollo.people.enrich", ("full_name", "domain"), 26_000)
    assert rank([apollo, hunter], given={"full_name", "domain"}, derive=derive)[0] is hunter


@pytest.mark.parametrize("capability", ["people.phone.find", "people.email.find"])
def test_a_one_subject_lookup_ranks_cheapest_per_hit_first_whatever_keys_a_child_reads(capability):
    """A caller holding a LinkedIn URL, a name and a domain sent phone finds to a name+domain child
    at ~$0.25 ahead of LinkedIn children at a fraction of a cent that hit on the same people:
    specificity ranked ahead of price. On a one-subject lookup every variant names the same
    person, so price per hit leads and the plan is in ascending order of it."""
    from treg.domain.catalog.routing.plan import candidates_for
    cat = catalog_store.load()
    contract = cat.contracts[capability]
    assert contract.one_subject
    sent = {"linkedin_url": "https://www.linkedin.com/in/patrickcollison", "full_name": "Patrick Collison",
            "domain": "stripe.com"}
    ident, _ = canonical_identity(contract, sent)
    eps = [e for e in cat.endpoints if e.get("capability") == capability]
    raw, _ = candidates_for(contract, eps, cat.adapters, ident)
    cands = [Candidate(endpoint=e, adapter=ad, variant=v, tier="platform",
                       price_micro=cost_at(cat.cost_view(e.get("cost"), e["provider"]), ident, ad),
                       hit_rate=None, ok_rate=None, p50_ms=None, last_ok_days=None) for e, ad, v in raw]
    ranked = rank(cands, given=set(sent), derive=contract.derive, one_subject=contract.one_subject)
    costs = [c.expected_cost_per_hit for c in ranked]
    assert costs == sorted(costs), [(c.endpoint["id"], c.expected_cost_per_hit) for c in ranked]
    # a search keeps specificity first: the flag is what changes the order, not the price data
    assert not cat.contracts["people.search"].one_subject


def test_a_provider_that_cannot_express_a_supplied_filter_ranks_last_among_equals():
    """Live 2026-08-29: `{q, title, location: London, country: GB}` went to the cheapest candidate,
    which had no place for either geo filter, and returned people in Bengaluru and San Francisco —
    reported as a hit. Cheapness must not buy an answer to a looser question."""
    from treg.domain.catalog.routing.plan import Candidate, ignored_filters, rank
    def cand(eid, price, ignored=()):
        return Candidate(endpoint={"id": eid, "provider": eid.split(".")[0], "cost": {"type": "per_result"}},
                         adapter=None, variant=("q",), tier="platform", price_micro=price, hit_rate=None,
                         ok_rate=None, p50_ms=None, last_ok_days=None, ignored=ignored)
    geo_blind = cand("aviato.people.search", 2_500, ignored=("country", "location"))
    geo_aware = cand("icypeas.people.search", 5_700)
    assert [c.endpoint["id"] for c in rank([geo_blind, geo_aware], given={"q"})] == [
        "icypeas.people.search", "aviato.people.search"], "the dearer provider that honours the filters leads"
    # still reachable when it is the only candidate, and price still decides among equals
    assert rank([geo_blind], given={"q"})[0] is geo_blind
    assert rank([geo_blind, cand("z.people.search", 9_000, ignored=("country", "location"))], given={"q"})[0] is geo_blind

    # and the set itself is read off the adapter's input map, not guessed
    cat = catalog_store.load()
    contract = cat.contracts["people.search"]
    ident = {"q": "backend engineers", "country": "GB", "location": "London, United Kingdom", "limit": 15}
    assert "country" in ignored_filters(cat.adapters["aviato.people.search"], contract, ident)
    assert ignored_filters(cat.adapters["icypeas.people.search"], contract, ident) == (), \
        "icypeas is the only people.search adapter that maps geo — the rule must float it to the top"
    # `limit` is a page size, not a looser question: a role finder without one must not rank below
    # a search that bills its empty pages
    assert ignored_filters(cat.adapters["leadmagic.x.role-finder"], contract,
                           {"company_domain": "acme.com", "title": "CEO", "limit": 10}) == ()
    # the full_name variant has exactly two candidates and neither mapped `country` — so a GT search
    # went to New York and was billed (voice-ai-outbound, 2026-09-03). aviato's simple search takes
    # country NAMES (live 2026-09-04: `Guatemala` → 84,145 rows, `GT` → 0), hence country_name().
    simple = cat.adapters["aviato.people.search.simple"]
    by_name, _ = canonical_identity(contract, {"full_name": "Carlos Lopez", "country": "GT", "limit": 5})
    assert ignored_filters(simple, contract, by_name) == ()
    q, _ = simple.to_upstream(by_name, ("full_name",))
    assert q["country"] == "Guatemala", q  # a query value travels as one string, never a list repr
    assert "country" not in simple.to_upstream({**by_name, "country": None}, ("full_name",))[0]


def test_a_title_search_drops_a_child_that_cannot_send_the_title():
    """`{company_domain, title}` answered by a domain-only child returns random employees of the
    company, billed as a hit: the title scopes the question, so such a child is not a candidate."""
    from treg.domain.catalog.routing.plan import unscoped
    cat = catalog_store.load()
    contract = cat.contracts["people.search"]
    asked, _ = canonical_identity(contract, {"company_domain": "swegon.com", "title": "Head of Procurement"})
    assert unscoped(cat.adapters["leadsforge.people.search"], contract, asked) == ("title",)
    assert unscoped(cat.adapters["crawl4ai.people.search"], contract, asked) == ()
    domain_only, _ = canonical_identity(contract, {"company_domain": "swegon.com"})
    assert unscoped(cat.adapters["leadsforge.people.search"], contract, domain_only) == ()


@pytest.mark.parametrize("country, sent", [
    ("United Kingdom", "gb"), ("GB", "gb"), ("uk", "gb"), ("spain", "es"), ("Narnia", "Narnia"), (None, None)])
def test_country_code_turns_a_name_or_code_into_the_iso_code(country, sent):
    """A provider that takes only a two-letter code answers 400 to a country name; a value that names
    no country passes through so the provider still says why."""
    assert P.evaluate("country_code(country)", {"country": country}) == sent


@pytest.mark.parametrize("status, error", [
    (511, True), (404, True), ("503", True), (200, False), (301, False), (600, False), (None, False), ("n/a", False)])
def test_http_error_is_true_only_for_a_4xx_or_5xx_status(status, error):
    assert P.evaluate("http_error(code)", {"code": status}) is error


@pytest.mark.parametrize("q, links, off", [
    ("site:ovhcloud.com vps price", ["https://www.reddit.com/r/x", "https://en.wiktionary.org/wiki/vps"], True),
    ("site:ovhcloud.com vps price", ["https://www.reddit.com/r/x", "https://www.ovhcloud.com/en/vps/"], False),
    ("site:ovhcloud.com vps price", ["https://us.ovhcloud.com/vps"], False),
    ("site:samsung.com/cz/support washer", ["https://www.samsung.com/uk/support/x"], True),
    ("site:samsung.com/cz/support washer", ["https://www.samsung.com/cz/support/washers/"], False),
    ("site:a.com OR site:b.org pricing", ["https://b.org/pricing"], False),
    ("pricing -site:reddit.com", ["https://reddit.com/x"], False),
    ("stripe radar rules", ["https://reddit.com/x"], False),
    ("site:ovhcloud.com vps", [], False),
    ("site:ovhcloud.com vps", [None], False),
])
def test_no_result_on_site_is_true_only_when_every_row_is_off_the_requested_site(q, links, off):
    rows = [{"title": "t"} if u is None else {"url": u} for u in links]
    assert P.evaluate("no_result_on_site(q, results)", {"q": q, "results": rows}) is off


@pytest.mark.parametrize("q, rows, absent", [
    ('"Dolby Family Ventures" digital health', [{"title": "Family - dictionary"}, {"title": "The Family (film)"}], True),
    ('"Dolby Family Ventures" digital health', [{"title": "x"}, {"snippet": "Dolby Family Ventures backs ..."}], False),
    ('"Café Société" menu', [{"title": "Cafe Societe, Paris"}], False),
    ('"4006381333931"', [{"title": "Generic pens"}], True),
    ('"4006381333931"', [{"url": "https://shop.example/p/4006381333931"}], False),
    ('"Acme" OR "Globex" ceo', [{"title": "Globex Corporation leadership"}], False),
    ("\u201cTeleClinic\u201d youtube", [{"title": "Miami Dolphins forum"}], True),
    ("teleclinic youtube", [{"title": "Miami Dolphins forum"}], False),
    ('Acme pricing -"careers"', [{"title": "Acme pricing plans"}], False),
    ('Acme -intitle:"jobs board"', [{"title": "Acme pricing plans"}], False),
    ('"Acme" -"careers"', [{"title": "Careers at Globex"}], True),
    ('"ab" x', [], False),
])
def test_quoted_absent_is_true_only_when_no_row_mentions_a_quoted_phrase(q, rows, absent):
    assert P.evaluate("quoted_absent(q, results)", {"q": q, "results": rows}) is absent


def test_a_question_miss_turns_an_off_site_hit_into_a_miss_and_only_with_the_question():
    """A child's rows can only be judged off target against the question: a site: search answered
    from other sites is a miss the route moves on from; the same rows for a plain search stay a hit."""
    from dataclasses import replace
    cat = catalog_store.load()
    contract = replace(cat.contracts["web.search"], question_miss="no_result_on_site(q, results)")
    ep = cat.by_id["crawl4ai.web.search"]
    adapter = cat.adapters["crawl4ai.web.search"]
    raw = json.dumps({"results": [{"url": "https://www.reddit.com/r/vps", "title": "VPS"}]}).encode()
    off_site = call_route.child_outcome(ep, contract, adapter, 200, raw, tier="platform",
                                        identity={"q": "site:ovhcloud.com vps price"})
    assert off_site.kind == "miss" and "question_miss" in off_site.why
    plain = call_route.child_outcome(ep, contract, adapter, 200, raw, tier="platform",
                                     identity={"q": "vps price"})
    assert plain.kind == "hit"
    assert call_route.child_outcome(ep, contract, adapter, 200, raw, tier="platform").kind == "hit", \
        "without the question (a replay record) nothing is judged"


@pytest.mark.parametrize("asked, ended, lost", [
    ("https://shop.fr/fr/idees-cadeaux/251-vernis", "https://shop.fr/", True),
    ("https://shop.fr/fr/idees-cadeaux/251-vernis", "https://shop.fr/en-us/", True),
    ("https://shop.fr/p/251", "https://shop.fr/p/251?ref=x", False),
    ("https://shop.fr/p/251", "https://shop.fr/products/251", False),
    ("https://shop.fr/", "https://shop.fr/", False),
    ("https://shop.fr/fr", "https://shop.fr/fr/", False),
    ("https://shop.fr/about", "https://shop.fr/blog", False),
    ("https://shop.fr/p/251", None, False),
])
def test_lost_path_is_true_only_when_a_deep_page_ended_on_the_home_page(asked, ended, lost):
    assert P.evaluate("lost_path(a, b)", {"a": asked, "b": ended}) is lost


@pytest.mark.parametrize("doc, miss", [
    ({"code": 502, "text": "# 502 Bad Gateway"}, True), ({"code": 200, "text": "a page"}, False),
    ({"text": "a page"}, False), ({"code": 200, "text": ""}, True)])
def test_a_miss_rule_can_combine_a_target_error_status_with_an_empty_text(doc, miss):
    """The shape a provider row uses: the target's error status decides first, empty text second."""
    assert P.evaluate("choose(http_error(code), true, coalesce(text, '') == '')", doc) is miss


async def test_the_geo_aware_child_wins_a_filtered_search_and_the_answer_says_what_was_dropped(
    clients: AsyncClient, enrichment_on, monkeypatch
):
    """End to end on the real people.search ladder: the caller sends geo, so the child that maps it
    is called even though a CHEAPER one is callable — and when the winner drops a filter, the
    envelope and a header say so, where a caller will see it (not buried in `tried[]`).

    Live 2026-08-29 this went to aviato ($0.0025, maps neither `country` nor `location`) and came
    back with people in Bengaluru and San Francisco for a London brief, reported as a hit."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_ICYPEAS", "PLATFORM-ICYPEAS-KEY")
    monkeypatch.setenv("TREG_PLATFORM_KEY_EXA", "PLATFORM-EXA-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "hunter,tomba,leadmagic,leadsforge,findymail,exa,fiber-ai,icypeas")
    get_settings.cache_clear()
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"icypeas": [(200, {"leads": [{"firstname": "Aleksei", "lastname": "Strizhak", "lastJobTitle": "Senior Backend Engineer",
                                       "address": "London Area, United Kingdom", "profileUrl": "https://linkedin.com/in/as"}]})],
         "*": [(200, {"persons": []})] * 12}, seen))
    r = await clients.post("/call/treg.people.search",
                           json={"q": "backend engineer", "title": "Backend Engineer",
                                 "location": "London, United Kingdom", "country": "GB", "limit": 15})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["_treg"]["served_by"] == "icypeas.people.search", \
        "the child that maps country/location leads, though the cheaper geo-blind aviato is callable"
    assert seen == [("icypeas", "POST", {}, {"query": {"currentJobTitle": {"include": ["Backend Engineer"]},
                                                      "location": {"include": ["London, United Kingdom"]}},
                                             "pagination": {"size": 15}})], \
        "one call, and the geography actually reached the provider"
    assert "ignored_filters" not in d["_treg"] and "X-Treg-Ignored-Filters" not in r.headers

    # …and when the winner cannot express a filter, the answer says which — envelope, header, attempt.
    # exa writes country into its text, so this half gives it an adapter without that.
    import dataclasses
    cat = catalog_store.load()
    exa = cat.adapters["exa.people.search"]
    monkeypatch.setitem(cat.adapters, "exa.people.search", dataclasses.replace(
        exa, in_expr={**exa.in_expr, "body.query": "join(q, company_domain, csv(keywords))",
                      "body.contents.highlights.query": "join(q, csv(keywords))"}))
    seen2 = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"*": [(200, {"results": [{"name": "Ada L", "url": "https://linkedin.com/in/al"}]})] * 12}, seen2))
    r2 = await clients.post("/call/treg.people.search", json={"q": "backend engineer", "country": "GB", "limit": 15})
    assert r2.status_code == 200, r2.text
    d2 = r2.json()
    assert d2["_treg"]["served_by"] == "exa.people.search", "no geo-aware child answers a {q}-only brief here"
    assert d2["_treg"]["ignored_filters"] == ["country"], "the caller sent it; exa has no place for it"
    assert r2.headers["X-Treg-Ignored-Filters"] == "country"
    assert d2["_treg"]["tried"][-1]["ignored_filters"] == ["country"], "same set in all three places"


async def test_routed_discovery_is_a_runtime_switch(clients: AsyncClient, enrichment_on, monkeypatch):
    """`TREG_ROUTED_DISCOVERY=off` stops search LEADING with `treg.<capability>` — nothing else.
    The endpoints stay callable, priced and reachable by id; only the steering goes away, so a
    deployment can answer "should every agent be pointed at the router by default" with traffic
    instead of an argument, and can undo it without a redeploy."""
    q = {"q": "find someone's work email from their name and company", "limit": 8}
    on = (await clients.get("/catalog/search", params=q)).json()
    ids_on = [r["id"] for r in on["results"]]
    assert any(i.startswith("treg.") for i in ids_on), "steering on: the routed row is in the page"

    monkeypatch.setenv("TREG_ROUTED_DISCOVERY", "off")
    get_settings.cache_clear()
    off = (await clients.get("/catalog/search", params=q)).json()
    ids_off = [r["id"] for r in off["results"]]
    assert not any(i.startswith("treg.") for i in ids_off), \
        "steering off: search looks as it did before routing shipped"
    assert ids_off, "and it still returns the providers themselves"

    # every OTHER discovery surface follows the same switch, or the deployment contradicts itself
    plat = (await clients.get("/catalog/platforms/people")).json()
    flat = json.dumps(plat)
    assert "treg.people." not in flat, "browse view: no routed row while steering is off"
    for path in ("/skill.md", "/llms.txt"):
        body = (await clients.get(path)).text
        assert "Routed endpoints" not in body, f"{path} must not teach what search hides"
        assert "<!--routed" not in body, f"{path} leaked a marker"
        if path == "/skill.md":  # the failure table lives in the skill; llms.txt links to it
            assert "provider_capacity_unavailable" in body, f"{path} lost the unrelated overflow guidance"

    # …but the endpoint is untouched: still callable, still priced, still found by id
    r = await clients.get("/catalog/endpoints/treg.people.email.find")
    assert r.status_code == 200 and r.json()["endpoint"]["kind"] == "routed"
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [(200, {"data": {"email": "p@stripe.com", "score": 99, "first_name": "Patrick",
                                   "last_name": "Collison", "verification": {"status": "valid"}}})]}, []))
    call = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"})
    assert call.status_code == 200 and call.json()["_treg"]["served_by"] == "tomba.people.email.find"
    get_settings.cache_clear()

def test_keywords_are_a_filter_so_they_reach_every_provider_that_can_express_them():
    """The brief's SUBSTANCE lives in its keywords. As identity they would be dropped whenever
    another variant matched — icypeas matches {title}, so a `q` carrying "microservices" never
    reached it and the search degenerated to title+location (bench 2026-08-29: "football scouting
    analysts" reached the provider as title="Football Analyst" and scored 0 qualified of 15)."""
    cat = catalog_store.load()
    contract = cat.contracts["people.search"]
    assert "keywords" in contract.filters and "keywords" not in {k for v in contract.identity for k in v}
    ident, variant = canonical_identity(contract, {
        "q": "backend developers with microservices", "title": "Backend Engineer",
        "location": "London, United Kingdom", "country": "GB",
        "keywords": ["microservices", "architecture"], "limit": 15})
    from treg.domain.catalog.routing.contracts import adapter_accepts
    icy = cat.adapters["icypeas.people.search"]
    _, body = icy.to_upstream(ident, adapter_accepts(icy, ident))
    assert body["query"]["keyword"]["include"] == ["microservices", "architecture"], \
        "icypeas takes them natively — the whole point of the contract field"
    exa = cat.adapters["exa.people.search"]
    _, body = exa.to_upstream(ident, adapter_accepts(exa, ident))
    assert "microservices" in body["query"], "a semantic provider gets them folded into the query"
    # and a provider with nowhere to put them says so, which ranks it down (PR #254)
    from treg.domain.catalog.routing.plan import ignored_filters
    assert "keywords" in ignored_filters(cat.adapters["aviato.people.search"], contract, ident)


async def test_a_thin_hit_does_not_end_the_waterfall_when_the_caller_set_min_results(
    clients: AsyncClient, enrichment_on, monkeypatch
):
    """`X-Treg-Route-Min-Results: 3` — one row is not an answer to "find me candidates". The
    router keeps going and the FULLEST answer wins; without it the first non-empty body stops the
    search (bench 2026-08-29: the hand-written policy's `if len(rows) < 3 -> fall through` was the
    single behaviour the routed path could not express)."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_ICYPEAS", "PLATFORM-ICYPEAS-KEY")
    monkeypatch.setenv("TREG_PLATFORM_KEY_EXA", "PLATFORM-EXA-KEY")
    monkeypatch.setenv("TREG_PLATFORM_KEY_QUICKENRICH", "PLATFORM-QUICKENRICH-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "hunter,tomba,leadmagic,leadsforge,findymail,exa,fiber-ai,icypeas,quickenrich")
    get_settings.cache_clear()
    thin = {"leads": [{"firstname": "Solo", "lastname": "Row", "profileUrl": "https://linkedin.com/in/s"}]}
    full = {"results": [{"name": f"P{i}", "url": f"https://linkedin.com/in/p{i}"} for i in range(9)]}
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"icypeas": [(200, thin)], "quickenrich": [(200, {"data": full["results"]})], "*": [(200, full)] * 12}, seen))
    body = {"q": "backend engineer", "title": "Backend Engineer",
            "location": "London, United Kingdom", "country": "GB", "limit": 15}
    r = await clients.post("/call/treg.people.search", json=body, headers={"X-Treg-Route-Min-Results": "3", "X-Treg-Route-Prefer": "icypeas"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert [t["outcome"] for t in d["_treg"]["tried"]][0] == "weak", "1 row < 3 is not an answer"
    assert d["_treg"]["served_by"] != "icypeas.people.search" and len(d["output"]["people"]) == 9

    # default (min_results 1): the same thin answer ends the search, as before
    seen2 = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"icypeas": [(200, thin)], "quickenrich": [(200, {"data": full["results"]})], "*": [(200, full)] * 12}, seen2))
    r2 = await clients.post("/call/treg.people.search", json=body, headers={"X-Treg-Route-Prefer": "icypeas"})
    assert r2.json()["_treg"]["served_by"] == "icypeas.people.search" and len(seen2) == 1

    # and when NOBODY clears the bar, the fullest weak answer is still returned, not a miss
    seen3 = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"icypeas": [(200, thin)], "quickenrich": [(200, {"data": [{"name": "Two"}, {"name": "Rows"}]})],
         "*": [(200, {"results": [{"name": "Two"}, {"name": "Rows"}]})] * 12}, seen3))
    r3 = await clients.post("/call/treg.people.search", json=body, headers={"X-Treg-Route-Min-Results": "5", "X-Treg-Route-Prefer": "icypeas"})
    assert r3.status_code == 200 and len(r3.json()["output"]["people"]) == 2, "best effort beats nothing"


async def test_the_weak_hit_fallback_is_bounded_like_the_error_fallback(
    clients: AsyncClient, enrichment_on, monkeypatch
):
    """Some briefs HAVE only one right answer ("who runs engineering at X"), so no provider ever
    clears min_results and an unbounded rule pays the whole ladder on every call. Measured on the
    bench's deterministic set: 12.7x ($1.76 -> $22.35 over 28 queries) for answers already correct.
    At most MAX_WEAK_FALLBACKS extra providers are asked, then the fullest answer is returned."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_ICYPEAS", "PLATFORM-ICYPEAS-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "hunter,tomba,leadmagic,leadsforge,findymail,aviato,fiber-ai,icypeas")
    get_settings.cache_clear()
    # Every provider answers ONE row in ITS OWN shape — a real thin HIT, not a miss (a miss does
    # not consume the bound, and must not: the bound is about paying for thin answers).
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"icypeas":    [(200, {"leads": [{"firstname": "One", "lastname": "Row"}], "total": 1})] * 4,
         "leadsforge": [(200, {"leads": [{"firstName": "One", "lastName": "Row"}]})] * 4,
         "leadmagic":  [(200, {"data": [{"full_name": "One Row"}], "total_count": 1})] * 4,
         "hunter":     [(200, {"data": {"emails": [{"value": "one@stripe.com"}]}, "meta": {"results": 1}})] * 4,
         "*":          [(200, {"items": [{"fullName": "One Row"}], "count": {"value": 1}})] * 8}, seen))
    # {company_domain} has the deepest ladder, so the BOUND is what stops this, not running out
    body = {"company_domain": "stripe.com", "limit": 15}
    r = await clients.post("/call/treg.people.search", json=body,
                           headers={"X-Treg-Route-Min-Results": "5"})   # nothing will ever clear 5
    assert r.status_code == 200, r.text
    d = r.json()
    attempts = [t for t in d["_treg"]["tried"] if t["outcome"] == "weak"]
    assert len(attempts) == call_route.MAX_WEAK_FALLBACKS + 1, \
        f"the first ask plus at most {call_route.MAX_WEAK_FALLBACKS} more, not the whole ladder"
    assert len(seen) == len(attempts), "and no provider beyond the bound was ever called"
    assert len(d["output"]["people"]) == 1, "and the caller still gets the answer that exists"


async def test_merge_unions_the_rows_the_caller_already_paid_for(
    clients: AsyncClient, enrichment_on, monkeypatch
):
    """`X-Treg-Route-Merge: 1` — a list answer is the one shape a union makes sense for, and the
    caller is charged for EVERY attempt already (`charged_micro` sums them), so returning only the
    winner's rows throws away results the team bought. Bench 2026-08-29: a people.search that fell
    through returned the fullest single provider's rows, never icypeas' 5 plus exa's 10."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_ICYPEAS", "PLATFORM-ICYPEAS-KEY")
    monkeypatch.setenv("TREG_PLATFORM_KEY_EXA", "PLATFORM-EXA-KEY")
    monkeypatch.setenv("TREG_PLATFORM_KEY_QUICKENRICH", "PLATFORM-QUICKENRICH-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "hunter,tomba,leadmagic,leadsforge,findymail,exa,fiber-ai,icypeas,quickenrich")
    get_settings.cache_clear()
    icy = {"leads": [{"firstname": "Ada", "lastname": "L", "profileUrl": "https://linkedin.com/in/ada"},
                     {"firstname": "Bo", "lastname": "M", "profileUrl": "https://linkedin.com/in/bo"}], "total": 2}
    # one row OVERLAPS on the profile url (different casing/scheme), one is new
    other = {"results": [{"name": "Ada L", "url": "https://www.linkedin.com/in/Ada/"},
                         {"name": "Cy N", "url": "https://linkedin.com/in/cy"}]}
    body = {"q": "backend engineer", "title": "Backend Engineer", "limit": 15}

    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"icypeas": [(200, icy)], "quickenrich": [(200, {"data": other["results"]})], "*": [(200, other)] * 8}, seen))
    r = await clients.post("/call/treg.people.search", json=body,
                           headers={"X-Treg-Route-Min-Results": "5", "X-Treg-Route-Merge": "1"})
    assert r.status_code == 200, r.text
    d = r.json()
    people = d["output"]["people"]
    keys = sorted(call_route._row_key(p) for p in people)
    assert keys == ["linkedin.com/in/ada", "linkedin.com/in/bo", "linkedin.com/in/cy"], \
        "the union of both providers, and Ada — who both returned, spelled differently — appears ONCE"
    assert len(people) == 3, "3 distinct people from two answers of 2 rows each"
    assert len(d["_treg"]["merged_from"]) >= 2 and "X-Treg-Merged-From" in r.headers
    assert d["_treg"]["charged_micro"] == int(r.headers["X-Treg-Cost-Micro"]), \
        "merging changes no money: the sum over attempts is what it always was"

    # without the header the winner's rows alone come back — the pre-existing contract
    seen2 = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"icypeas": [(200, icy)], "*": [(200, other)] * 8}, seen2))
    r2 = await clients.post("/call/treg.people.search", json=body,
                            headers={"X-Treg-Route-Min-Results": "5"})
    assert "merged_from" not in r2.json()["_treg"] and len(r2.json()["output"]["people"]) == 2


def _person(i: int) -> dict:
    return {"name": f"P{i}", "url": f"https://linkedin.com/in/p{i}"}


async def test_a_q_search_fills_to_the_limit_the_caller_sent(clients: AsyncClient, people_search_on, monkeypatch):
    """`fill_limit_when: [[q]]` on people.search: a free-text search asks for everyone who fits, so
    the caller's `limit` is a row count to reach. A short answer goes on to the next provider, the
    rows add up, and the walk stops once the union reaches the limit."""
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"exa": [(200, {"results": [_person(0), _person(1)]})],
         # one row overlaps with exa's: the distinct union, not the raw count, is what reaches 5
         "parallel": [(200, {"entities": [_person(1), _person(2), _person(3), _person(4)]})],
         "*": [(200, {"results": [_person(9)]})] * 4}, seen))
    # The order for a free-text q is a published route rule; the caller's header stands in for it here.
    r = await clients.post("/call/treg.people.search", json={"q": "head of tax in phoenix", "limit": 5},
                           headers={"X-Treg-Route-Prefer": "exa,parallel"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert [p for p, *_ in seen] == ["exa", "parallel"], "2 + 3 new rows reach 5: no third provider"
    assert len(d["output"]["people"]) == 5 and d["_treg"]["merged_from"] == [
        "exa.people.search", "parallel.people.search"]

    # the caller's own headers win over the contract's default
    seen.clear()
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"exa": [(200, {"results": [_person(0), _person(1)]})], "*": [(200, {"results": []})] * 4}, seen))
    r2 = await clients.post("/call/treg.people.search", json={"q": "head of tax in phoenix", "limit": 5},
                            headers={"X-Treg-Route-Min-Results": "1", "X-Treg-Route-Prefer": "exa,parallel"})
    assert [p for p, *_ in seen] == ["exa"] and "merged_from" not in r2.json()["_treg"]


async def test_a_title_search_answers_with_current_holders_first(clients: AsyncClient, people_search_on, monkeypatch):
    def person(i, history):
        return {"url": f"https://linkedin.com/in/p{i}", "entities": [{"properties": {"name": f"P{i}", "workHistory": history}}]}
    left = person(0, [{"title": "Support Engineer", "dates": {"from": "2026-01", "to": None}},
                      {"title": "Developer Evangelist", "dates": {"from": "2016", "to": "2025-12"}}])
    now = person(1, [{"title": "Developer Evangelist", "dates": {"from": "2023-01", "to": None}}])
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"*": [(200, {"results": [left, now]})] * 4}, seen))
    r = await clients.post("/call/treg.people.search", json={"title": "Developer Evangelist", "limit": 2},
                           headers={"X-Treg-Route-Prefer": "exa", "X-Treg-Route-Waterfall": "0"})
    assert r.status_code == 200, r.text
    people = r.json()["output"]["people"]
    assert [(p["name"], p.get("title_match")) for p in people] == [("P1", "current"), ("P0", "past")]
    assert people[1]["past_titles"] == ["Developer Evangelist"]


async def test_a_company_scoped_q_never_falls_back_to_a_company_only_answer(
        clients: AsyncClient, people_search_on, monkeypatch):
    """{company_domain, q}: exa and parallel send both; crawl4ai and icypeas would answer by company
    alone, so when both semantic searches miss the answer is a miss, not the company's interns."""
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"exa": [(200, {"results": []})], "parallel": [(200, {"entities": []})],
         "*": [(200, {"data": [{"name": "Intern"}]})] * 4}, seen))
    r = await clients.post("/call/treg.people.search",
                           json={"q": "executives", "company_domain": "example.com", "limit": 3})
    assert r.status_code == 200, r.text
    assert sorted(p for p, *_ in seen) == ["exa", "parallel"] and not (r.json()["output"] or {}).get("people")
    assert seen[0][3].get("query", seen[0][3].get("objective")) == "executives example.com"


def test_a_people_search_adapter_lists_its_wider_variants_first():
    """The first variant fully present is the one sent and the one ranked: a narrower one ahead of a
    wider one drops the caller's extra key (leadocean answered {company_domain, title} by the
    domain alone) or hides it from ranking (prospeo sent the title through in_expr, unranked)."""
    cat = catalog_store.load()
    for eid, ad in cat.adapters.items():
        if (cat.by_id.get(eid) or {}).get("capability") != "people.search":
            continue
        for i, narrow in enumerate(ad.accepts):
            assert not any(set(narrow) < set(wide) for wide in ad.accepts[i + 1:]), (eid, narrow)
    leadocean = cat.adapters["leadocean.people.search"]
    contract = cat.contracts["people.search"]
    ident, _ = canonical_identity(contract, {"company_domain": "example.com", "title": "CFO", "q": "speaks Japanese"})
    assert leadocean.to_upstream(ident, adapter_accepts_of(leadocean, ident))[0] == {
        "domain": "example.com", "title": "CFO", "q": "speaks Japanese", "limit": "10"}
    # its person sits under profile_data, which a merge must read to tell a repeat from a new row
    assert leadocean.rows([{"profile_data": {"profile_full_name": "Jane Doe", "profile_headline": "CFO at Example",
                                             "profile_url": "https://www.linkedin.com/in/jane",
                                             "profile_address": {"city": "Austin", "country": "US"}},
                            "contact_data": {"contact_current_experiences": [
                                {"company_name": "Example Inc", "company_domain": "example.com", "job_title": "CFO"}]}}]) == [
        {"name": "Jane Doe", "profile_url": "https://www.linkedin.com/in/jane", "title": "CFO",
         "company": "Example Inc", "company_domain": "example.com", "location": "Austin, US",
         "summary": "CFO at Example"}]


def test_every_routed_people_search_adapter_answers_in_the_contract_row_shape():
    """A row map that cannot read the provider's body hands the caller the vendor's own row
    (dropleads' camelCase leads, aiark's employer under company.summary): every routed adapter
    maps rows, and each example row comes back with contract keys only. A row is a person a caller
    can act on: a name and a way to reach or check them (title, profile or email), not a bare URL
    or a bare name (fiber-ai before its map)."""
    cat = catalog_store.load()
    contract = cat.contracts["people.search"]
    for eid, ad in cat.adapters.items():
        ep = cat.by_id.get(eid) or {}
        if ep.get("capability") != "people.search" or not ad.route:
            continue
        assert ad.row_map and set(ad.row_map) <= set(contract.row_fields), eid
        example = json.loads(catalog_store.example_path(eid).read_text())
        # a saved example's truncation marker ("… 8 more item(s) truncated") is not a row
        for row in [r for r in ad.from_upstream(example).get("people") or [] if not isinstance(r, str)]:
            assert isinstance(row, dict) and set(row) <= set(contract.row_fields), (eid, row)
            assert any(row.get(k) for k in contract.row_needs), (eid, row)
            assert row.get("name") and any(row.get(k) for k in ("title", "profile_url", "email")), (eid, row)


def adapter_accepts_of(adapter, identity):
    from treg.domain.catalog.routing.contracts import adapter_accepts
    return adapter_accepts(adapter, identity)


async def test_a_title_sent_through_an_expression_still_ranks_on_price(clients, platform_on, monkeypatch):
    """prospeo and companyenrich both send {company_domain, title}; with prospeo's narrower
    {company_domain} variant listed first its title went unranked and the dearer one led."""
    for p in ("PROSPEO", "COMPANYENRICH"):
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{p}", f"PLATFORM-{p}-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "prospeo,companyenrich")
    get_settings.cache_clear()
    got = await _first_providers("treg.people.search", {"company_domain": "example.com", "title": "CFO", "limit": 3}, n=2)
    get_settings.cache_clear()
    assert got == ["prospeo", "companyenrich"]


async def test_keys_no_provider_can_send_together_are_refused_422_not_503(
        clients: AsyncClient, enrichment_on, monkeypatch):
    """{q, company_domain, full_name}: every provider is dropped because it cannot send all three,
    and a retry never changes that, so it is the caller's 422, not a capacity 503."""
    cat = catalog_store.load()
    provs = sorted({cat.by_id[e]["provider"] for e in cat.adapters
                    if cat.by_id.get(e, {}).get("capability") == "people.search"})
    for p in provs:
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{p.upper().replace('-', '_')}", "PLATFORM-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", ",".join(provs))
    get_settings.cache_clear()
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"*": [(200, {})] * 4}, seen))
    r = await clients.post("/call/treg.people.search", json={
        "q": "speaks fluent Japanese", "company_domain": "example.com", "full_name": "Alice Cooper"})
    get_settings.cache_clear()
    assert r.status_code == 422, r.text
    assert "send every key" in r.json()["detail"]["message"] and seen == []
    # A caller who excluded the providers that could is told to check that list
    r2 = await clients.post("/call/treg.people.search", json={"q": "executives", "company_domain": "example.com"},
                            headers={"X-Treg-Route-Exclude": "exa,parallel,leadocean,prospeo"})
    assert r2.status_code == 422 and "x-treg-route-exclude" in r2.json()["detail"]["message"].lower(), r2.text


async def test_a_q_that_is_not_a_string_is_refused_before_any_provider(
        clients: AsyncClient, people_search_on, monkeypatch):
    """`q` is `text`: a list would be skipped by the providers' text joins, which would then search
    the domain alone and bill for it."""
    from treg.domain.catalog.routing import contracts as C

    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"*": [(200, {"results": []})] * 4}, seen))
    r = await clients.post("/call/treg.people.search",
                           json={"q": ["executives"], "company_domain": "example.com", "limit": 3})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "invalid_identity" and seen == []
    doc = {"identity": [{"q": "str", "title": "str"}], "output": {"a": "str"}, "scoping": ["q"]}
    assert C.parse_contracts({"contracts": {"y": {**doc, "carried_by": {"q": ["title"]}}}})["y"].carried_by == {
        "q": ("title",)}
    for bad in ({"title": ["q"]}, {"q": []}, {"q": ["nope"]}, {"q": ["q"]}, ["q"]):
        with pytest.raises(ValueError, match="carried_by"):
            C.parse_contracts({"contracts": {"y": {**doc, "carried_by": bad}}})


@pytest.mark.parametrize("body, target", [
    ({"q": "head of tax", "limit": 15}, 15),
    ({"q": "head of tax", "company_domain": "stripe.com", "limit": 15}, 15),
    ({"q": "   ", "company_domain": "stripe.com", "limit": 15}, None),   # a blank q was not sent
    ({"q": "head of tax", "limit": 15, "country": "US", "keywords": ["vat"]}, 15),   # filters are not identity
    ({"q": "head of tax"}, None),                                  # the contract default is no one's ask
    ({"q": "head of tax", "limit": 1}, None),                      # one row is reached by any hit
    ({"q": "head of tax", "limit": True}, None),
    ({"q": "head of tax", "limit": "15"}, None),
    ({"q": "head of tax", "title": "Head of Tax", "limit": 15}, None),   # exactly the listed keys
    ({"title": "Head of Tax", "limit": 15}, None),
    ({"q": "", "title": "Head of Tax", "limit": 15}, None),         # an empty key was not sent
])
def test_fill_target_is_the_limit_only_on_the_listed_key_set(body, target):
    assert catalog_store.load().contracts["people.search"].fill_target(body) == target


@pytest.mark.parametrize("raw", [
    "q", [], ["q"], [[]], [["nope"]], [[1]], {"q": True},
])
def test_a_malformed_fill_limit_when_fails_the_load(raw):
    base = {"identity": [{"q": "str"}], "filters": {"limit": {"type": "int"}},
            "output": {"people": {"type": "list", "required": True}}}
    assert parse_contracts({"contracts": {"y": {**base, "fill_limit_when": [["q"]]}}})["y"].fill_limit_when == (("q",),)
    with pytest.raises(ValueError, match="fill_limit_when"):
        parse_contracts({"contracts": {"y": {**base, "fill_limit_when": raw}}})
    for broken in ({**base, "filters": {}}, {**base, "output": {"people": {"type": "str"}}}):
        with pytest.raises(ValueError, match="fill_limit_when"):
            parse_contracts({"contracts": {"y": {**broken, "fill_limit_when": [["q"]]}}})


async def test_routed_child_that_ignores_limit_keeps_every_row_and_discloses(
    clients: AsyncClient, monkeypatch
):
    """A serving child with no place for `limit` (crawl4ai.web.search) still returns its full page.
    The route keeps every billed row and names the excess on `_treg.limit_exceeded` — it never
    trims the child's answer. Strict-Filters does not change this: `limit` is a page size, not a
    narrowing filter, so it stays out of `ignored_filters`."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_CRAWL4AI", "PLATFORM-C4AI")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "crawl4ai")
    get_settings.cache_clear()
    rows = [{"title": f"t{i}", "url": f"https://example.com/{i}", "snippet": "s", "source": "gs"}
            for i in range(10)]
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"crawl4ai": [(200, {"results": rows, "n": 10})]}, seen))
    r = await clients.post("/call/treg.web.search",
                           json={"q": "golden retriever", "limit": 3},
                           headers={"X-Treg-Route-Strict-Filters": "1"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["_treg"]["served_by"] == "crawl4ai.web.search"
    assert len(d["output"]["results"]) == 10 and d["output"]["count"] == 10,         "every row the child returned reaches the caller"
    note = d["_treg"]["limit_exceeded"]
    assert note == {"asked": 3, "returned": 10, "why": "crawl4ai.web.search does not accept limit"}
    assert r.headers["X-Treg-Limit-Exceeded"] == "asked=3,returned=10"
    assert "ignored_filters" not in d["_treg"] and "X-Treg-Ignored-Filters" not in r.headers
    # without a sent limit, the contract default is not a question anyone asked
    seen.clear()
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"crawl4ai": [(200, {"results": rows, "n": 10})]}, seen))
    r2 = await clients.post("/call/treg.web.search", json={"q": "golden retriever"})
    assert r2.status_code == 200 and "limit_exceeded" not in r2.json()["_treg"]
    get_settings.cache_clear()


def test_a_long_cursor_leaves_output_and_is_named_on_treg():
    ad = SimpleNamespace(out_map={"next_cursor": "pagination.token"})
    short = {"people": [], "next_cursor": "abc"}
    assert call_route._long_cursor(short, ad, False) == (short, None)
    long = {"people": [{"name": "A"}], "next_cursor": "x" * (call_route.CURSOR_MAX + 1)}
    out, note = call_route._long_cursor(long, ad, False)
    assert out == {"people": [{"name": "A"}], "next_cursor": None} and long["next_cursor"], "a copy, not a mutation"
    assert note == {"chars": call_route.CURSOR_MAX + 1, "in_raw": "pagination.token"}
    assert call_route._long_cursor(long, ad, True)[1] == {"chars": call_route.CURSOR_MAX + 1}, "a merge names no one path"
    assert call_route._long_cursor(None, ad, False) == (None, None)


async def test_a_long_provider_cursor_stays_in_raw_only(clients: AsyncClient, enrichment_on, monkeypatch):
    """icypeas answers a people search with a pagination token of many KB. A routed endpoint takes
    no cursor, so the token only serves a direct icypeas call: `raw` keeps it whole, `output`
    drops it, and `_treg.next_cursor` says where it is."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_ICYPEAS", "PLATFORM-ICYPEAS-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "icypeas")
    get_settings.cache_clear()
    token = "t" * 20_000
    icy = {"success": True, "total": 1, "pagination": {"token": token, "size": 1},
           "leads": [{"firstname": "Ada", "lastname": "L", "profileUrl": "https://linkedin.com/in/ada"}]}
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"icypeas": [(200, icy)]}, []))
    r = await clients.post("/call/treg.people.search", json={"title": "Backend Engineer", "limit": 1})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["_treg"]["served_by"] == "icypeas.people.search"
    assert d["output"]["next_cursor"] is None and d["output"]["people"]
    assert d["raw"]["pagination"]["token"] == token
    assert d["_treg"]["next_cursor"] == {"chars": len(token), "in_raw": "pagination.token"}
    get_settings.cache_clear()


async def test_merge_past_limit_keeps_every_row_and_discloses(
    clients: AsyncClient, enrichment_on, monkeypatch
):
    """`X-Treg-Route-Merge: 1` used to slice the union to `limit` even though its docstring said
    discarding paid rows is waste. Keep the full union and disclose the excess."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_ICYPEAS", "PLATFORM-ICYPEAS-KEY")
    monkeypatch.setenv("TREG_PLATFORM_KEY_EXA", "PLATFORM-EXA-KEY")
    monkeypatch.setenv("TREG_PLATFORM_KEY_QUICKENRICH", "PLATFORM-QUICKENRICH-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "hunter,tomba,leadmagic,leadsforge,findymail,exa,fiber-ai,icypeas,quickenrich")
    get_settings.cache_clear()
    icy = {"leads": [{"firstname": "Ada", "lastname": "L", "profileUrl": "https://linkedin.com/in/ada"},
                     {"firstname": "Bo", "lastname": "M", "profileUrl": "https://linkedin.com/in/bo"}], "total": 2}
    other = {"results": [{"name": "Ada L", "url": "https://www.linkedin.com/in/Ada/"},
                         {"name": "Cy N", "url": "https://linkedin.com/in/cy"}]}
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"icypeas": [(200, icy)], "quickenrich": [(200, {"data": other["results"]})], "*": [(200, other)] * 8}, seen))
    r = await clients.post("/call/treg.people.search",
                           json={"q": "backend engineer", "title": "Backend Engineer", "limit": 2},
                           headers={"X-Treg-Route-Min-Results": "5", "X-Treg-Route-Merge": "1"})
    assert r.status_code == 200, r.text
    d = r.json()
    people = d["output"]["people"]
    assert len(people) == 3 and d["output"]["count"] == 3, "full union, not sliced to limit=2"
    assert d["_treg"]["limit_exceeded"] == {
        "asked": 2, "returned": 3, "why": "merged answers exceeded the asked limit"}
    assert r.headers["X-Treg-Limit-Exceeded"] == "asked=2,returned=3"
    get_settings.cache_clear()


async def test_child_that_returns_exactly_limit_does_not_claim_excess(
    clients: AsyncClient, monkeypatch
):
    """When returned row count equals the sent `limit`, there is nothing to disclose — even if the
    child never mapped `limit` (crawl4ai). Excess disclosure is about count, not adapter shape."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_CRAWL4AI", "PLATFORM-C4AI")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "crawl4ai")
    get_settings.cache_clear()
    rows = [{"title": f"t{i}", "url": f"https://example.com/{i}", "snippet": "s", "source": "gs"}
            for i in range(3)]
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"crawl4ai": [(200, {"results": rows, "n": 3})]}, seen))
    r = await clients.post("/call/treg.web.search", json={"q": "rust web crawler", "limit": 3})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["_treg"]["served_by"] == "crawl4ai.web.search"
    assert len(d["output"]["results"]) == 3 and "limit_exceeded" not in d["_treg"]
    assert "X-Treg-Limit-Exceeded" not in r.headers
    get_settings.cache_clear()


def test_a_per_success_endpoint_with_no_adapter_settles_on_the_providers_own_success_rule():
    """The adapter's `miss` predicate covers routed children; 1330 of 1517 per_success endpoints
    have no adapter, and they are the SCRAPERS — whose failure mode is an HTTP 200 carrying an
    error code. Those providers publish a success rule and the catalog records it as `expect`,
    which until now only `scripts/catalog_verify.py` read.

    Live 2026-08-29: `justoneapi.x.linkedin-search-user-v1` answered
    `{"code": 301, "message": "COLLECT FAILED, SEND REQUEST AGAIN"}` — free on the vendor's own
    published terms ("only a code-0 response is billed") — and treg settled $0.0295 against the
    caller."""
    from test_marketplace_call import _mk
    from treg.application.call import settle as A
    from treg.application.call.resolve import QueryValues
    from treg.application.replay.engine import platform_call
    cat = catalog_store.load()
    eid = "justoneapi.x.linkedin-search-user-v1"
    ep = cat.by_id[eid]
    assert (ep.get("cost") or {}).get("type") == "per_success" and cat.adapters.get(eid) is None, \
        "the shape this rule exists for: priced per success, no adapter to ask"
    assert ep.get("expect") == {"path": "response.code", "op": "equals", "value": 0}, \
        "the row inherits its provider's rule"

    mk = platform_call(ep, QueryValues(()), b"", set(), routed_child=False)
    fail = b'{"code": 301, "data": null, "message": "COLLECT FAILED, SEND REQUEST AGAIN"}'
    decision = A.decide_settle(mk, 200, fail)
    assert (decision.observed_micro, decision.actual_micro) == (0, 0), \
        "a vendor-side failure the vendor does not bill"
    ok = b'{"code": 0, "data": {"users": [{"name": "Ada"}]}}'
    assert A.decide_settle(mk, 200, ok).actual_micro == mk.estimate_micro > 0, \
        "a real hit still settles at the estimate"

    # a nested rule form (dataforseo's task envelope) reads the same way
    dfs = [e for e in cat.by_id.values()
           if (e.get("expect") or {}).get("json_path") == "tasks.0.status_code"
           and cat.adapters.get(e["id"]) is None
           and (e.get("cost") or {}).get("type") == "per_success"]
    if dfs:
        m2 = _mk(dfs[0]["provider"], endpoint_id=dfs[0]["id"], cost_type="per_success")
        assert A._observed_cost_micro(m2, b'{"tasks": [{"status_code": 40501}]}') == 0
        assert A._observed_cost_micro(m2, b'{"tasks": [{"status_code": 20000}]}') is None

async def test_a_gone_endpoint_is_the_providers_fault_not_the_callers(clients: AsyncClient, enrichment_on, monkeypatch):
    """410 Gone (a discontinued route), 405 and 501 say the PROVIDER cannot serve this, whatever the
    caller sent: the waterfall goes on as after a 5xx, to paid-per-call providers too. Before this a
    410 read as the caller's fault and ended the call when no free-on-failure provider was left
    (live 2026-10-04: aviato discontinued its LinkedIn post routes and linkedin.user.posts failed)."""
    seen = []
    gone = (410, {"error": "Gone", "message": "This endpoint has been discontinued and is no longer available."})
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"*": [gone] * 10}, seen))
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"})
    assert r.json()["detail"]["error"] != "route_caller_fault", r.text
    assert r.status_code == 502 and len(seen) >= 2, r.text


async def test_a_declared_miss_status_is_a_miss_not_a_caller_fault(clients: AsyncClient, enrichment_on, monkeypatch):
    """aviato answers HTTP 404 `Not Found` for a person it has no record of. The endpoint's YAML says
    so (`miss: {status: 404}`), and the router must read it: before this a waterfall in which the
    other providers all missed ended in a 502 `route_failed` (live 2026-09-03, voice-ai-outbound —
    768 of 1,824 phone.find 502s in 30 days had no failure but an aviato 404), when the honest
    answer was a 200 miss."""
    routed = "treg.people.phone.find"
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"aviato": [(404, {"message": "Not Found"})],
         "tomba": [(200, {"data": {"e164_format": None}})],
         "leadmagic": [(200, {"mobile_number": None, "credits_consumed": 0})],
         "findymail": [(200, {"phone": None})],
         "leadsforge": [(200, {"phoneNumber": None})]}, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{routed}", json={"linkedin_url": "https://www.linkedin.com/in/nobody-here"})
    assert r.status_code == 200, r.text
    assert r.headers["X-Treg-Route-Outcome"] == "miss" and r.json()["_treg"]["served_by"] is None
    outcomes = {t["endpoint_id"]: t["outcome"] for t in r.json()["_treg"]["tried"]}
    assert outcomes["aviato.people.phone.find"] == "miss" and len(seen) == 5, "the 404 is a miss; every provider was still asked"
    assert await _balance(clients) == before, "nobody found anything, nothing was charged"
    # an UNDECLARED 4xx keeps its meaning: a vendor rejecting the request is still the caller's fault
    seen.clear()
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"aviato": [(422, {"message": "bad identifier"})], "*": [(422, {"message": "bad"})] * 4}, seen))
    r = await clients.post(f"/call/{routed}", json={"linkedin_url": "https://www.linkedin.com/in/nobody-here"},
                           headers={"X-Treg-Route-Prefer": "aviato"})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "route_caller_fault", r.text
    # and with the waterfall off, the declared 404 alone is the (free) miss the caller asked for
    seen.clear()
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"aviato": [(404, {"message": "Not Found"})]}, seen))
    r = await clients.post(f"/call/{routed}", json={"linkedin_url": "https://www.linkedin.com/in/nobody-here"},
                           headers={"X-Treg-Route-Prefer": "aviato", "X-Treg-Route-Waterfall": "0"})
    assert r.status_code == 200 and r.headers["X-Treg-Route-Outcome"] == "miss" and len(seen) == 1, r.text
    assert r.json()["output"]["phone"] is None


def test_every_declared_miss_status_names_its_meaning():
    """`miss: {status, means}` is agent-facing (`endpoint_view`) and router-facing: both halves
    are required. The router honours a 4xx only — a `status: 200` block is documentation for the
    agent (tikhub answers 200 with a null body for an unknown id) and the adapter's own predicate
    decides that case, so `_miss_status` must never turn a success into a miss."""
    cat = catalog_store.load()
    declared = {e["id"]: e["miss"] for e in cat.by_id.values() if e.get("miss") and not is_v2(e)}
    assert "aviato.people.phone.find" in declared and "aviato.companies.enrich" in declared
    for eid, m in declared.items():
        assert isinstance(m, dict) and m.get("status") is not None and m.get("means"), eid
        assert int(m["status"]) < 500, f"{eid}: a 5xx is never 'asked and answered'"
    assert call_route._miss_status(cat.by_id["aviato.people.phone.find"]) == 404
    assert call_route._miss_status(cat.by_id["tikhub.x.reddit-app-fetch-post-comments"]) is None
    assert call_route._miss_status({"id": "x"}) is None


async def test_capped_signal_when_max_cost_truncates_waterfall(clients: AsyncClient, enrichment_with_quickenrich_on, monkeypatch):
    """Feedback #131: When max-cost stops the waterfall early, the result should indicate that more
    expensive providers were skipped (capped=true). This lets callers distinguish an exhaustive miss
    from one truncated by budget - they can raise their ceiling if they need to try all providers."""
    routed = "treg.people.phone.find"
    miss = {'success': False, 'message': 'No data found for this profile'}
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({'quickenrich': [(200, miss)]}, []))
    r = await clients.post(f"/call/{routed}", json={"linkedin_url": "https://www.linkedin.com/in/example"},
                           headers={"X-Treg-Route-Max-Cost": "0.01"})  # ~$0.01 allows QuickEnrich only
    assert r.status_code == 200, r.text
    treg = r.json()["_treg"]
    assert treg["outcome"] == "miss"
    assert treg.get("capped") is True, f"Expected capped=true when waterfall truncated: {treg}"
    assert r.headers.get("X-Treg-Route-Capped") == "true", "Expected X-Treg-Route-Capped header"
    skipped = [t for t in treg["tried"] if t["outcome"] == "skipped" and "would exceed" in t.get("detail", "")]
    assert skipped, f"Expected some providers skipped due to cost: {treg['tried']}"


async def test_strict_filters_refuses_a_looser_answer_instead_of_billing_it(clients: AsyncClient, enrichment_on, monkeypatch):
    """voice-ai-outbound, 2026-09-03: `{full_name, country: GT}` went to a candidate that ignored
    the country and was billed for people in New York. Opt-in, the caller is refused instead —
    unbilled, told which filter, and what identity a filter-aware provider would take."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_CRUSTDATA", "PLATFORM-CRUSTDATA-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "hunter,tomba,leadmagic,leadsforge,findymail,aviato,fiber-ai,crustdata")
    get_settings.cache_clear()
    # This regression compares Crustdata with Aviato, independently of other catalog additions.
    cat = catalog_store.load()
    for eid in cat.by_id["treg.people.search"]["routed_children"]:
        if not eid.startswith(("crustdata.", "aviato.")):
            monkeypatch.delitem(cat.adapters, eid)
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"*": [(200, {"profiles": [{"name": "Someone"}], "total_count": 1})] * 3}, seen))
    before = await _balance(clients)
    # crustdata's people.search takes full_name and nothing geographic: with the header it is dropped
    r = await clients.post("/call/treg.people.search", json={"full_name": "Carlos Lopez", "country": "GT", "limit": 3},
                           headers={"X-Treg-Route-Strict-Filters": "1", "X-Treg-Route-Exclude": "aviato"})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "no_route_candidate", r.text
    d = r.json()["detail"]
    assert seen == [] and await _balance(clients) == before, "refused before any provider was asked; nothing billed"
    assert any(x["endpoint_id"] == "crustdata.people.search" and x.get("strict") and "country" in x["why"] for x in d["dropped"]), d
    assert "X-Treg-Route-Strict-Filters" in d["message"] and "full_name" in d["message"]
    # without the header the same call goes out, is billed, and says what it ignored
    r = await clients.post("/call/treg.people.search", json={"full_name": "Carlos Lopez", "country": "GT", "limit": 3},
                           headers={"X-Treg-Route-Exclude": "aviato"})
    assert r.status_code == 200 and r.headers["X-Treg-Ignored-Filters"] == "country" and r.json()["_treg"]["ignored_filters"] == ["country"], r.text
    assert len(seen) == 1
    # and a candidate that CAN express the filter is unaffected by the header (aviato's simple search maps country)
    seen.clear()
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"aviato": [(200, {"items": [{"fullName": "Carlos Lopez", "location": "Guatemala"}], "totalResults": 1})]}, seen))
    r = await clients.post("/call/treg.people.search", json={"full_name": "Carlos Lopez", "country": "GT", "limit": 3},
                           headers={"X-Treg-Route-Strict-Filters": "1"})
    assert r.status_code == 200 and r.json()["_treg"]["served_by"] == "aviato.people.search.simple", r.text
    assert "X-Treg-Ignored-Filters" not in r.headers and seen[0][2]["country"] == "Guatemala"
    get_settings.cache_clear()


@pytest.mark.parametrize('expression,expected', [('0', 0), ('2', 9668), ('-1', None), ('true', None), ("'2'", None)])
def test_adapter_unit_quote_requires_nonnegative_integer(expression, expected):
    from dataclasses import replace
    ad = replace(catalog_store.load().adapters['quickenrich.people.search.domain'], cost_units=expression)
    assert cost_at({'usd': 0.004834}, {}, ad) == expected


@pytest.mark.parametrize('endpoint,given,expected', [
    ('people.email.find', {'first_name': 'Example', 'last_name': 'Person', 'domain': 'example.com'}, 4834),
    ('people.phone.find', {'linkedin_url': 'https://linkedin.com/in/example'}, 4834),
    ('people.enrich', {'email': 'person@example.com'}, 4834),
    ('people.search', {'company_domain': 'example.com', 'limit': 10}, 0),
    ('people.search.domain', {'company_domain': 'example.com', 'limit': 1}, 4834),
    ('people.search.domain', {'company_domain': 'example.com', 'title': 'CEO', 'limit': 1}, 96680),
    ('companies.search', {'domain': 'example.com'}, 48340),
    ('companies.search', {'domain': 'example.com', 'limit': 1}, 4834),
    ('companies.search', {'domain': 'example.com', 'limit': 100}, 483400),
])
def test_enrichment_route_quote_matches_direct_reservation(endpoint, given, expected):
    from treg.application.call.resolve import _marketplace_pricing
    from treg.domain.catalog.routing.contracts import adapter_accepts
    cat = catalog_store.load()
    ep = cat.by_id['quickenrich.' + endpoint]
    ad = cat.adapters[ep['id']]
    ident, _ = canonical_identity(cat.contracts[ep['capability']], given)
    query, body = ad.to_upstream(ident, adapter_accepts(ad, ident))
    cost = cat.cost_view(ep['cost'], ep['provider'])
    direct, _ = _marketplace_pricing(ep['provider'], ep['id'], cost, query, json.dumps(body).encode())
    assert direct == cost_at(cost, ident, ad) == expected


@pytest.mark.parametrize('value,expected', [
    ({'a.example': {'name': 'A'}, 'b.example': {'name': 'B'}}, [{'name': 'A'}, {'name': 'B'}]),
    ([{'name': 'A'}], [{'name': 'A'}]), ({}, []), ([], []), (None, None), ('bad', None),
])
def test_row_values_and_nested_lookup_expressions(value, expected):
    assert P.evaluate('values(rows)', {'rows': value}) == expected
    assert P.evaluate("get(values(rows), '[0].name')", {'rows': value}) == (
        'A' if expected else None)


# ---- regression: adapter exceptions after child success must not crash the parent (2026-09) ----


def _make_throwing_adapter(real, throw_on: str):
    """Create a wrapper adapter that throws on the specified method."""
    class ThrowingAdapter:
        def __init__(self, real):
            self._real = real
            # Copy ALL attributes from the real Adapter dataclass
            self.endpoint_id = real.endpoint_id
            self.accepts = real.accepts
            self.in_map = real.in_map
            self.out_map = real.out_map
            self.miss = real.miss
            self.const = getattr(real, 'const', {})
            self.in_expr = getattr(real, 'in_expr', {})
            self.body_array = getattr(real, 'body_array', False)
            self.test_identity = getattr(real, 'test_identity', {})
            self.route = real.route
            self.cost_units = getattr(real, 'cost_units', '')
            self.additional_capabilities = getattr(real, 'additional_capabilities', ())
            self.verified_capabilities = getattr(real, 'verified_capabilities', ())
            self.verified = real.verified
            self.verify_note = getattr(real, 'verify_note', '')
            self._filter_keys = getattr(real, '_filter_keys', ())

        def to_upstream(self, identity, variant):
            if throw_on == 'to_upstream':
                raise KeyError("simulated to_upstream failure")
            return self._real.to_upstream(identity, variant)

        def from_upstream(self, provider_body):
            if throw_on == 'from_upstream':
                raise ValueError("simulated from_upstream failure")
            return self._real.from_upstream(provider_body)

        def is_miss(self, provider_body):
            if throw_on == 'is_miss':
                raise TypeError("simulated is_miss failure")
            return self._real.is_miss(provider_body)

    return ThrowingAdapter(real)


def _patched_catalog_with_throwing_adapter(original_cat, endpoint_id: str, throw_on: str):
    """Return a new Catalog with one adapter replaced by a throwing wrapper."""
    from dataclasses import replace
    new_adapters = dict(original_cat.adapters)
    new_adapters[endpoint_id] = _make_throwing_adapter(original_cat.adapters[endpoint_id], throw_on)
    return replace(original_cat, adapters=new_adapters)


def _patched_catalog_all_email_find_throw(original_cat):
    """Return a new Catalog where all email.find adapters throw on from_upstream."""
    from dataclasses import replace
    new_adapters = {}
    for eid, adapter in original_cat.adapters.items():
        if 'email.find' in eid:
            new_adapters[eid] = _make_throwing_adapter(adapter, 'from_upstream')
        else:
            new_adapters[eid] = adapter
    return replace(original_cat, adapters=new_adapters)


@pytest.mark.parametrize(("throw_on", "tomba_answers"), [
    ("from_upstream", [(200, {'data': {'email': 'bad@format.test', 'unexpectedField': True}})]),
    ("to_upstream", []),  # throws before the child call, so tomba is never relayed
    ("is_miss", [(200, {'data': {'email': 'tomba@test.test', 'score': 99}})]),
])
async def test_adapter_from_upstream_throws_after_child_200_waterfall_continues(
    clients: AsyncClient, enrichment_on, monkeypatch, throw_on, tomba_answers,
):
    """Regression for 2026-09 bug: adapter.from_upstream throwing after a child returned 200 used
    to crash the parent with a bare 502, leaving children audited OK but parent failed. Now the
    adapter failure (in to_upstream, from_upstream or is_miss) is recorded as an error and the
    waterfall continues to the next provider."""
    original_cat = catalog_store.load()
    # Patch tomba's adapter to throw (tomba is first in price order for this identity)
    patched_cat = _patched_catalog_with_throwing_adapter(original_cat, "tomba.people.email.find", throw_on)

    monkeypatch.setattr(catalog_store, 'load', lambda: patched_cat)

    seen = []
    # hunter returns 200 and works fine
    # '*' catches other providers in waterfall (findymail, etc) returning miss
    monkeypatch.setattr(call_service, 'relay', _relay_by_provider({
        '*': [(200, {'data': None})] * 10,  # Other providers return miss-like response
        'tomba': list(tomba_answers),
        'hunter': [(200, {'data': {'email': 'found@example.test', 'score': 80, 'verification': {'status': 'valid'}}})],
    }, seen))

    r = await clients.post(f'/call/{ROUTED}', json={'full_name': 'Example Person', 'domain': 'example.com'})
    assert r.status_code == 200, r.text
    doc = r.json()
    # Waterfall continued to hunter after tomba's adapter failed
    assert doc['_treg']['served_by'] == 'hunter.people.email.find'
    assert doc['_treg']['outcome'] == 'hit'
    # The tomba error should be recorded in `tried`
    tried = {t['endpoint_id']: t for t in doc['_treg']['tried']}
    assert 'tomba.people.email.find' in tried
    assert tried['tomba.people.email.find']['outcome'] == 'error'
    assert f'adapter.{throw_on} failed' in tried['tomba.people.email.find']['detail']
    # Hunter succeeded
    assert tried['hunter.people.email.find']['outcome'] == 'hit'


async def test_adapter_throws_on_all_children_returns_structured_502_with_tried(
    clients: AsyncClient, enrichment_on, monkeypatch,
):
    """When ALL adapters throw on 200 responses, the parent must return a structured 502
    with proper error details and `tried` list - not a bare 500 or empty 502. (findymail's name
    search is not patched to throw; it would answer a miss, and the call would then be a miss.)"""
    original_cat = catalog_store.load()
    patched_cat = _patched_catalog_all_email_find_throw(original_cat)

    monkeypatch.setattr(catalog_store, 'load', lambda: patched_cat)

    seen = []
    # All providers return 200 but adapters throw
    # '*' wildcard catches all providers - return data that adapters will parse
    monkeypatch.setattr(call_service, 'relay', _relay_by_provider({
        '*': [(200, {'data': {'email': 'x@test.test'}})] * 15,
    }, seen))

    before = await _balance(clients)
    r = await clients.post(f'/call/{ROUTED}', json={'full_name': 'Example Person', 'domain': 'example.com'},
                           headers={'X-Treg-Route-Exclude': 'findymail'})
    # Should be 502 route_failed, not 500 or empty body
    assert r.status_code == 502, r.text
    doc = r.json()
    assert doc['detail']['error'] == 'route_failed'
    assert await _balance(clients) == before
    # The tried list should have some error outcomes (adapters that threw)
    tried = doc['detail']['tried']
    assert len(tried) > 0
    error_outcomes = [t for t in tried if t['outcome'] == 'error']
    # At least one adapter should have thrown (those with email.find in name)
    assert len(error_outcomes) > 0, f"Expected at least one error outcome, got: {tried}"
    # Check that error details mention adapter failure
    for t in error_outcomes:
        if 'detail' in t and t['detail']:
            assert 'adapter' in t['detail'] or 'failed' in t['detail'], f"Unexpected error detail: {t}"


async def test_a_miss_with_a_failed_answer_on_the_way_is_an_incomplete_miss_that_charges_only_misses(
    clients: AsyncClient, enrichment_on, monkeypatch,
):
    """One provider's failure no longer turns the others' honest "nobody" into a 502: the call is a
    200 miss marked incomplete. A billed answer the adapter could not read is refunded; the caller
    pays only for the misses."""
    patched_cat = _patched_catalog_all_email_find_throw(catalog_store.load())
    monkeypatch.setattr(catalog_store, 'load', lambda: patched_cat)
    seen = []
    monkeypatch.setattr(call_service, 'relay', _relay_by_provider({
        'findymail': [(200, {'contact': None})] * 3,
        '*': [(200, {'data': {'email': 'x@test.test'}})] * 15,
    }, seen))
    before = await _balance(clients)
    r = await clients.post(f'/call/{ROUTED}', json={'full_name': 'Example Person', 'domain': 'example.com'})
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc['_treg']['outcome'] == 'miss' and doc['_treg']['incomplete'] is True
    assert r.headers['X-Treg-Route-Incomplete'] == 'true'
    outcomes = {t['outcome'] for t in doc['_treg']['tried']}
    assert {'miss', 'error'} <= outcomes
    assert all(t['charged_micro'] == 0 for t in doc['_treg']['tried'] if t['outcome'] == 'error')
    billed_misses = sum(t['charged_micro'] for t in doc['_treg']['tried'] if t['outcome'] == 'miss')
    assert doc['_treg']['charged_micro'] == billed_misses
    assert before - await _balance(clients) == billed_misses
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []


@pytest.mark.parametrize(("error_code", "tomba_answers", "status", "outcome"), [
    # a semantic miss, not a caller fault: the waterfall continues and the parent does not 502
    ("NO_MATCH", [(200, {'data': {'email': 'found@example.test', 'score': 99, 'verification': {'status': 'valid'}}})],
     200, "miss"),
    # the same 400 with another body is a rejected request: an error, never a clean miss (the
    # others' misses make the call an incomplete miss)
    ("INVALID_DATAPOINTS", [(200, {'data': None})], 200, "error"),
])
async def test_prospeo_no_match_400_is_treated_as_miss_not_error(
    clients: AsyncClient, enrichment_with_miss_declarers_on, monkeypatch,
    error_code, tomba_answers, status, outcome,
):
    """Prospeo returns 400 with error_code=NO_MATCH for 'no result'. The same 400 with a
    non-NO_MATCH body is recorded as an error, the waterfall goes on to free-on-failure providers,
    and the outcome is never a clean miss."""
    seen = []
    monkeypatch.setattr(call_service, 'relay', _relay_by_provider({
        '*': [(200, {'data': None})] * 10,  # Other providers miss
        'prospeo': [(400, {'error': True, 'error_code': error_code})],
        'tomba': list(tomba_answers),
    }, seen))

    r = await clients.post(f'/call/{ROUTED}', json={'full_name': 'Example Person', 'domain': 'example.com'},
                           headers={'X-Treg-Route-Prefer': 'prospeo'})  # ask it first; otherwise tomba's hit ends the waterfall before it runs
    assert r.status_code == status, r.text
    doc = r.json()
    if error_code == "NO_MATCH":
        assert doc['_treg']['outcome'] == 'hit'  # waterfall continued past Prospeo's NO_MATCH
    else:
        assert doc['_treg']['outcome'] == 'miss' and doc['_treg']['incomplete'] is True
    tried = doc['_treg']['tried'] if status == 200 else doc['detail']['tried']
    prospeo_attempts = [t for t in tried if t['provider'] == 'prospeo']
    assert prospeo_attempts, tried
    assert all(t['outcome'] == outcome for t in prospeo_attempts), prospeo_attempts


# ---- miss.when: one status, two meanings (prospeo 400 NO_MATCH vs INVALID_DATAPOINTS) ----


def test_declared_miss_honours_when_predicate_and_never_crashes():
    from treg.application.call import route as call_route
    ep = {"id": "x", "miss": {"status": 400, "when": "error_code == 'NO_MATCH'", "means": "no match"}}
    assert call_route._declared_miss(ep, 400, b'{"error":true,"error_code":"NO_MATCH"}')
    assert not call_route._declared_miss(ep, 400, b'{"error":true,"error_code":"INVALID_DATAPOINTS"}')
    assert not call_route._declared_miss(ep, 404, b'{"error":true,"error_code":"NO_MATCH"}')
    assert not call_route._declared_miss(ep, 400, b'["NO_MATCH"]')      # array body: no crash, not a miss
    assert not call_route._declared_miss(ep, 400, b'not json')
    plain = {"id": "y", "miss": {"status": 404, "means": "gone"}}
    assert call_route._declared_miss(plain, 404, b'Not Found')
    assert not call_route._declared_miss(plain, 400, b'')
    cat = catalog_store.load()
    prospeo = cat.by_id["prospeo.people.email.find"]
    assert "when" not in catalog_store.endpoint_view(prospeo, "Prospeo", cat)["miss"], "internal predicate leaks to agents"


def test_linkedin_url_is_normalised_once_for_every_adapter():
    from treg.domain.catalog.routing import paths as P
    from treg.domain.catalog.routing.contracts import canonical_identity
    assert P.linkedin_url("linkedin.com/in/patrickcollison") == "https://linkedin.com/in/patrickcollison"
    assert P.linkedin_url("www.linkedin.com/in/patrickcollison/") == "https://www.linkedin.com/in/patrickcollison/"
    assert P.linkedin_url("https://www.linkedin.com/in/patrickcollison") == "https://www.linkedin.com/in/patrickcollison"
    assert P.linkedin_url("patrickcollison") == "https://www.linkedin.com/in/patrickcollison"
    contract = catalog_store.load().contracts["people.email.find"]
    ident, variant = canonical_identity(contract, {"linkedin_url": "linkedin.com/in/patrickcollison"})
    assert variant == ("linkedin_url",)
    assert ident["linkedin_url"] == "https://linkedin.com/in/patrickcollison"
    assert ident["linkedin_handle"] == "patrickcollison"
    assert P.linkedin_handle(P.linkedin_url("WWW.LinkedIn.com/in/Patrick")) == "Patrick"


@pytest.mark.parametrize(("raw", "expected"), [
    # a path that merely mentions linkedin.com is never promoted to that host
    ("evil.example/?linkedin.com/in/x", None),
    # a derived URL is built only from something handle-shaped, never `/in/N/A`
    ("N/A", None), ("#N/A", None), ("-", None), ("patrickcollison", "https://www.linkedin.com/in/patrickcollison"),
    ("uk.linkedin.com/in/x", "https://uk.linkedin.com/in/x"),
    # the host is lowercased so the handle derives
    ("LinkedIn.com/in/Patrick", "https://linkedin.com/in/Patrick"),
])
def test_linkedin_url_only_trusts_a_linkedin_host(raw, expected):
    from treg.domain.catalog.routing import paths as P
    assert P.linkedin_url(raw) == expected


@pytest.mark.parametrize(("raw", "expected"), [
    ("https://www.linkedin.com/in/patrickcollison", "https://www.linkedin.com/in/patrickcollison"),
    ("https://www.linkedin.com/in/%E5%BC%A0%E4%B8%89-1a2b3c/en", "https://www.linkedin.com/in/%E5%BC%A0%E4%B8%89-1a2b3c/en"),
    ("WWW.LinkedIn.com/in/Patrick", "https://WWW.LinkedIn.com/in/Patrick"),
    ("m.linkedin.com/in/x?trk=a", "https://m.linkedin.com/in/x?trk=a"),
    ("stripe.com", "https://stripe.com"),
    ("http://bücher.de/x", "http://bücher.de/x"),
    ("https://example.com/a page", "https://example.com/a page"),
    ("N/A", None), ("null", None), ("-", None), ("patrickcollison", None),
    ("ftp://stripe.com/x", None), ("https://", None), ("see https://stripe.com", None), (".com/x", None),
    ("http://localhost/", None), ("https://…", None),
    ("john@stripe.com", None), ("mailto:a@b.com", None), ("user:pw@stripe.com/x", None), ("   ", None),
    ("linkedin.com/in/x?r=https://g.com/", "https://linkedin.com/in/x?r=https://g.com/"),
    ("https://ตัวอย่าง.ไทย/", "https://ตัวอย่าง.ไทย/"), ("https://stripe.com./", "https://stripe.com./"),
    ("https://[2001:db8::1]/", "https://[2001:db8::1]/"),
    ("https://example.com/?q=" + "a" * 3000, "https://example.com/?q=" + "a" * 3000),
])
def test_url_identity_is_checked_by_its_declared_type(raw, expected):
    from treg.domain.catalog.routing.contracts import _as_url
    assert _as_url(raw) == expected


def test_identity_type_check_treats_blank_as_absent_and_echoes_safely():
    from treg.domain.catalog.routing.contracts import invalid_identity
    contract = catalog_store.load().contracts["people.email.find"]
    assert invalid_identity(contract, {"full_name": "P C", "domain": "stripe.com", "linkedin_url": "   "}) == []
    [bad] = invalid_identity(contract, {"linkedin_url": "\ud83d"})
    bad["value"].encode("utf-8")


def test_arena_and_router_read_the_miss_block_the_same_way():
    from treg.domain import arena
    cat = catalog_store.load()
    ep = cat.by_id["prospeo.people.email.find"]
    ad = cat.adapters[ep["id"]]
    contract = cat.contracts["people.email.find"]
    assert arena.classify(contract, ad, ep, 400, {"error": True, "error_code": "NO_MATCH"})[0] == "miss"
    assert arena.classify(contract, ad, ep, 400, {"error": True, "error_code": "INVALID_DATAPOINTS"})[0] == "error"


async def test_company_blind_search_provider_is_dropped_not_billed(clients: AsyncClient, platform_on, monkeypatch):
    """`people.search` declares `scoping: [company_domain]`. A title-only provider asked for
    `{company_domain, title}` answers with the same strangers for every company and bills them as
    a hit, so it leaves the plan instead of ranking last — and still serves a title-only search."""
    for p in ("LUSHA", "COMPANYENRICH"):
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{p}", f"PLATFORM-{p}-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "lusha,companyenrich")
    get_settings.cache_clear()
    cat = catalog_store.load()
    for eid in cat.by_id["treg.people.search"]["routed_children"]:
        if eid not in ("lusha.people.search", "companyenrich.people.search"):
            monkeypatch.delitem(cat.adapters, eid, raising=False)
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({
        "lusha": [(200, {"results": [{"name": "Some CEO"}], "pagination": {"total": 1}})],
        "companyenrich": [(200, {"items": [], "totalItems": 0})]}, seen))
    r = await clients.post("/call/treg.people.search", json={"company_domain": "example.com", "title": "CEO"})
    assert [s[0] for s in seen] == ["companyenrich"], seen
    assert r.status_code == 200 and r.json()["_treg"]["outcome"] == "miss", r.text
    assert any(d["endpoint_id"] == "lusha.people.search" and "company_domain" in d["why"]
               for d in r.json()["_treg"]["dropped"]), r.json()["_treg"]
    seen.clear()
    r = await clients.post("/call/treg.people.search", json={"title": "CEO"})
    assert [s[0] for s in seen] == ["lusha"] and r.json()["_treg"]["served_by"] == "lusha.people.search", r.text
    get_settings.cache_clear()


def _example(eid: str) -> dict:
    from pathlib import Path
    return json.loads((Path(catalog_store.__file__).parents[2] / "catalog" / "examples" / f"{eid}.json").read_text())


async def test_people_enrich_not_found_from_dropleads_and_aiark_is_a_miss_not_a_502(clients, monkeypatch):
    """Dropleads "Person not found" 400s and AI Ark "data not found" 404s were read as provider
    faults, so a person nobody had came back as 502 route_failed. Both are now declared misses; a rate limit or another 400 stays an error."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_DROPLEADS", "PLATFORM-DROPLEADS")
    monkeypatch.setenv("TREG_PLATFORM_KEY_AIARK", "PLATFORM-AIARK")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "dropleads,aiark")
    get_settings.cache_clear()
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({
        "dropleads": [(400, {"success": False, "error": "Person not found. Try providing more information like LinkedIn URL or company name."})],
        "ai-ark": [(404, {"status": 404, "error": "data not found", "path": ""})],
    }, seen))
    r = await clients.post("/call/treg.people.enrich", json={"email": "nobody@example.com"},
                           headers={"X-Treg-Route-Prefer": "dropleads,aiark"})
    assert r.status_code == 200, r.text
    assert r.json()["_treg"]["outcome"] == "miss"
    assert [t["outcome"] for t in r.json()["_treg"]["tried"]] == ["miss", "miss"]
    assert len(seen) == 2
    get_settings.cache_clear()


# ---- web.extract / web.search: crawl4ai first by default -------------------------------------------
@pytest.mark.parametrize("job, body, answer, first_seen", [
    ("treg.web.extract", {"url": "https://example.com"},
     {"ok": True, "markdown": "# Example Domain", "content_hash": "x", "engine": "archive"},
     {"url": "https://example.com", "format": "md"}),
    ("treg.web.search", {"q": "rust web crawler"},
     {"results": [{"title": "spider", "url": "https://github.com/spider-rs/spider", "snippet": "s", "source": "gs"}], "n": 1},
     None),
])
async def test_crawl4ai_answers_scrape_and_search_first_by_default(clients, monkeypatch, job, body, answer, first_seen):
    monkeypatch.setenv("TREG_PLATFORM_KEY_CRAWL4AI", "PLATFORM-C4AI")
    monkeypatch.setenv("TREG_PLATFORM_KEY_FIRECRAWL", "PLATFORM-FIRECRAWL")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "crawl4ai,firecrawl")
    get_settings.cache_clear()
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"crawl4ai": [(200, answer)], "*": []}, seen))
    r = await clients.post(f"/call/{job}", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["_treg"]["provider"] == "crawl4ai" and [s[0] for s in seen] == ["crawl4ai"]
    if first_seen is not None:
        assert seen[0][3] == first_seen
    else:
        assert seen[0][2] == {"q": "rust web crawler"}
    get_settings.cache_clear()


async def test_a_declared_not_found_ends_the_job_and_an_empty_page_is_a_miss(clients, monkeypatch):
    """A scraped page that does not exist: the first provider's declared "the site answered 404"
    (`not_found:`) ends the call with that 404, charging nothing, instead of asking providers that
    answer the same page with an empty "success". An empty page is a miss, so the job goes on."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_CRAWL4AI", "PLATFORM-C4AI")
    monkeypatch.setenv("TREG_PLATFORM_KEY_FIRECRAWL", "PLATFORM-FIRECRAWL")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "crawl4ai,firecrawl")
    get_settings.cache_clear()
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"crawl4ai": [(404, {"ok": False, "reason": "origin-error:404"})], "*": [(200, {"data": {"markdown": ""}})]}, seen))
    before = await _balance(clients)
    r = await clients.post("/call/treg.web.extract", json={"url": "https://example.com/gone"})
    assert r.status_code == 404 and r.json()["detail"]["error"] == "route_not_found", r.text
    assert [s[0] for s in seen] == ["crawl4ai"], "no other provider is asked"
    assert [t["outcome"] for t in r.json()["detail"]["tried"]] == ["not_found"]
    assert await _balance(clients) == before
    # a 404 that is not the site's own answer keeps its old meaning: the job goes on
    seen.clear()
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"crawl4ai": [(404, {"ok": False, "reason": "something-else"})], "*": [(200, {"data": {"markdown": ""}})]}, seen))
    r = await clients.post("/call/treg.web.extract", json={"url": "https://example.com/gone"})
    assert [s[0] for s in seen] == ["crawl4ai", "*"]
    assert r.status_code == 200 and r.json()["_treg"]["tried"][-1]["outcome"] == "miss", "an empty page is a miss, not a hit"
    assert r.json()["_treg"]["incomplete"] is True, "crawl4ai's 404 failed on the way"
    get_settings.cache_clear()


async def test_google_organic_asks_crawl4ai_first_and_falls_back_when_it_is_empty(clients, monkeypatch):
    """The routed Google organic job sends crawl4ai the query, country, language and the depth rounded
    up to whole Google pages; an empty answer is a miss and the next provider answers."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_CRAWL4AI", "PLATFORM-C4AI")
    monkeypatch.setenv("TREG_PLATFORM_KEY_ANYAPI", "PLATFORM-ANYAPI")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "crawl4ai,anyapi")
    get_settings.cache_clear()
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({
        "crawl4ai": [(200, {"results": [], "empty_reason": "rescue_no_results"})],
        "*": [(200, {"output": {"data": {"results": [{"title": "t", "link": "https://example.com"}]}}})]}, seen))
    r = await clients.post("/call/treg.google.serp.organic", json={"q": "pizza", "country": "PL", "language": "pl", "limit": 25})
    assert r.status_code == 200, r.text
    assert seen[0][0] == "crawl4ai" and seen[0][2] == {"q": "pizza", "country": "pl", "language": "pl", "num": "30"}
    assert [t["outcome"] for t in r.json()["_treg"]["tried"]] == ["miss", "hit"]
    assert r.json()["_treg"]["provider"] == "anyapi"
    get_settings.cache_clear()



def test_empty_output_uses_list_and_count_defaults_not_null():
    """A routed miss must keep list-typed contract fields as [] (and count as 0), never null.

    Other ints (followers, likes, …) stay null — a miss must not look like a measured zero.
    """
    from treg.application.call.route import _empty_output
    from types import SimpleNamespace
    contract = SimpleNamespace(output={
        "results": {"type": "list", "required": True},
        "count": {"type": "int"},
        "email": {"type": "str"},
        "followers": {"type": "int"},
    })
    assert _empty_output(contract) == {
        "results": [], "count": 0, "email": None, "followers": None,
    }


async def test_routed_web_search_miss_emits_empty_results_list_not_null(clients, monkeypatch):
    """Catalog contract: results is a required list. An all-miss waterfall used to return
    output.results=null / count=null; callers then mistook HTTP 200 for a broken/usable hit shape.
    Empty answers stay outcome=miss with results=[] and count=0."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_CRAWL4AI", "PLATFORM-C4AI")
    monkeypatch.setenv("TREG_PLATFORM_KEY_TINYFISH", "PLATFORM-TINYFISH")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "crawl4ai,tinyfish")
    get_settings.cache_clear()
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({
        "crawl4ai": [(200, {"results": [], "n": 0})],
        "tinyfish": [(200, {"results": [], "total_results": 0})],
    }, seen))
    r = await clients.post("/call/treg.web.search", json={"q": "site:example.invalid nonexistent-xyzzy-12345", "limit": 5},
                           headers={"X-Treg-Route-Prefer": "crawl4ai,tinyfish", "X-Treg-Route-Max-Cost": "0.05"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["_treg"]["outcome"] == "miss"
    assert body["_treg"]["served_by"] is None
    assert body["output"] == {"results": [], "count": 0}
    assert isinstance(body["output"]["results"], list) and body["output"]["results"] == []
    assert all(t["outcome"] in ("miss", "skipped") for t in body["_treg"]["tried"])
    assert [s[0] for s in seen] == ["crawl4ai", "tinyfish"]
    get_settings.cache_clear()


async def test_routed_google_serp_organic_miss_emits_empty_results_list_not_null(clients, monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_CRAWL4AI", "PLATFORM-C4AI")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "crawl4ai")
    get_settings.cache_clear()
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({
        "crawl4ai": [(200, {"results": [], "empty_reason": "rescue_no_results"})],
    }, seen))
    r = await clients.post("/call/treg.google.serp.organic", json={"q": "site:example.invalid xyzzy-no-hits", "limit": 5},
                           headers={"X-Treg-Route-Prefer": "crawl4ai", "X-Treg-Route-Max-Cost": "0.05"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["_treg"]["outcome"] == "miss"
    assert body["output"] == {"results": [], "count": 0}
    get_settings.cache_clear()


# ---- web.extract.structured: a new routed job, crawl4ai first ----------------------------------------
_SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}


@pytest.fixture
def _structured_keys(monkeypatch):
    for k, v in (("CRAWL4AI", "PLATFORM-C4AI"), ("LINKUP", "PLATFORM-LINKUP"), ("SEARCH1API", "PLATFORM-S1")):
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{k}", v)
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "crawl4ai,linkup,search1api")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_structured_extract_goes_to_crawl4ai_first(clients, monkeypatch, _structured_keys):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"crawl4ai": [(200, {"ok": True, "data": {"title": "Example Domain"}})], "*": []}, seen))
    r = await clients.post("/call/treg.web.extract.structured",
                           json={"url": "https://example.com", "schema": _SCHEMA, "instruction": "the page title"})
    assert r.status_code == 200, r.text
    assert r.json()["output"]["data"] == {"title": "Example Domain"} and [s[0] for s in seen] == ["crawl4ai"]
    assert seen[0][3] == {"url": "https://example.com", "schema": _SCHEMA, "instruction": "the page title"}


async def test_an_instruction_without_a_schema_only_fits_crawl4ai(clients, monkeypatch, _structured_keys):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"crawl4ai": [(200, {"ok": True, "data": [{"title": "a"}]})], "*": []}, seen))
    r = await clients.post("/call/treg.web.extract.structured",
                           json={"url": "https://example.com", "instruction": "the story titles"})
    assert r.status_code == 200, r.text
    assert [s[0] for s in seen] == ["crawl4ai"]


async def test_when_crawl4ai_fails_linkup_answers_with_the_same_schema(clients, monkeypatch, _structured_keys):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({
        "crawl4ai": [(502, {"error": "fetch_failed"})],
        "linkup": [(200, {"data": {"title": "Example Domain"}, "markdown": "x"})],
        "*": [],
    }, seen))
    r = await clients.post("/call/treg.web.extract.structured", json={"url": "https://example.com", "schema": _SCHEMA},
                           headers={"X-Treg-Route-Prefer": "crawl4ai,linkup"})
    assert r.status_code == 200, r.text
    assert [s[0] for s in seen] == ["crawl4ai", "linkup"] and r.json()["_treg"]["provider"] == "linkup"
    assert seen[1][3]["schema"] == _SCHEMA and seen[1][3]["mode"] == "standard"


def test_every_iso_country_reaches_dataforseo_as_its_own_location():
    """`country` on a routed SEO capability once reached DataForSEO as the United States for every
    country outside a short hand-written table: a Thai or Russian volume question was answered with
    US volumes and billed as a hit, and nothing in the answer said so. DataForSEO's country codes
    are Google Ads geo target ids (2000 + the ISO 3166-1 numeric code), so the whole ISO table maps."""
    cat = catalog_store.load()
    contract = cat.contracts["google.keywords.volume"]
    dfs = cat.adapters["dataforseo.google.keywords.volume"]
    anchors = {"us": 2840, "uk": 2826, "th": 2764}
    for code, location in anchors.items():
        ident, _ = canonical_identity(contract, {"keywords": ["kahve"], "country": code.upper()})
        _, body = dfs.to_upstream(ident)
        assert body[0]["location_code"] == location, (code, body)
    countries = json.loads((catalog_store.CATALOG_DIR / "countries.json").read_text())
    assert all(P.dfs_location(c) for c in countries), "every country treg knows has a DataForSEO location"
    assert P.dfs_location("Thailand") == 2764, "a country name is the same country"


def test_a_country_dataforseo_cannot_be_asked_about_is_reported_not_answered_for_the_us():
    """A value that names no country must not quietly become the United States: the location is
    left out and the filter is reported as ignored, the same way a provider with no geo field is."""
    from treg.domain.catalog.routing.plan import ignored_filters
    cat = catalog_store.load()
    contract = cat.contracts["google.serp.organic"]
    dfs = cat.adapters["dataforseo.google.serp.organic"]
    ident, _ = canonical_identity(contract, {"q": "coffee", "country": "xx"})
    assert P.dfs_location("xx") is None
    assert "location_code" not in dfs.to_upstream(ident)[1][0]
    assert ignored_filters(dfs, contract, ident) == ("country",)
    good, _ = canonical_identity(contract, {"q": "coffee", "country": "th"})
    assert ignored_filters(dfs, contract, good) == ()


def test_a_contract_default_the_caller_did_not_send_is_not_an_ignored_filter():
    """`google.keywords.volume` defaults `language: en`. SE Ranking has no language field, so the
    default alone ranked it behind a child fifty times its price on every call, while the catalog
    quote (which ranks without the default) named SE Ranking first. Only a filter the caller sent
    can make an answer looser than the question."""
    from treg.domain.catalog.routing.plan import ignored_filters
    cat = catalog_store.load()
    contract = cat.contracts["google.keywords.volume"]
    seranking = cat.adapters["seranking.google.keywords.volume"]
    ident, _ = canonical_identity(contract, {"keywords": ["tea"]})
    assert ident["language"] == "en", "the default still reaches the providers that take it"
    assert ignored_filters(seranking, contract, ident, given={"keywords"}) == ()
    sent, _ = canonical_identity(contract, {"keywords": ["tea"], "language": "de"})
    assert ignored_filters(seranking, contract, sent, given={"keywords", "language"}) == ("language",)

    def cand(eid, price, ignored):
        return Candidate(endpoint={"id": eid, "provider": eid.split(".")[0], "cost": {"type": "per_call"}},
                         adapter=None, variant=("keywords",), tier="platform", price_micro=price, hit_rate=None,
                         ok_rate=None, p50_ms=None, last_ok_days=None, ignored=ignored)
    plan = [cand("seranking.google.keywords.volume", 1_790,
                 ignored_filters(seranking, contract, ident, given={"keywords"})),
            cand("dataforseo.google.keywords.volume", 90_000,
                 ignored_filters(cat.adapters["dataforseo.google.keywords.volume"], contract, ident, given={"keywords"}))]
    quote = [cand(c.endpoint["id"], c.price_micro, ()) for c in plan]
    assert [c.endpoint["id"] for c in rank(plan, given={"keywords"})] == \
        [c.endpoint["id"] for c in rank(quote)] == ["seranking.google.keywords.volume", "dataforseo.google.keywords.volume"]


async def test_a_missed_child_releases_its_hold_before_the_next_child_reserves(
    clients: AsyncClient, enrichment_on, monkeypatch,
):
    """A balance that covers one child at a time, not two. A free miss used to keep its hold open
    until the routed call closed, so the next child's reserve answered a false 402 mid-waterfall."""
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        await ledger.reserve(db, org_id, "leave room for one child", await _balance(clients) - 25_000)
    miss_tomba = (200, {"data": {"email": None, "score": None, "verification": {"status": None}}})
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({
        "tomba": [miss_tomba],
        "findymail": [(200, {"contact": {"name": "Patrick Collison", "email": "patrick@stripe.com"}})],
    }, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"},
                           headers={"X-Treg-Route-Prefer": "tomba,findymail"})
    assert r.status_code == 200, r.text
    assert [t["outcome"] for t in r.json()["_treg"]["tried"]] == ["miss", "hit"]
    assert before - await _balance(clients) == int(r.headers["X-Treg-Cost-Micro"]) == 19_800
    async with session_maker() as db:
        holds = (await db.execute(select(Hold))).scalars().all()
    assert len(holds) == 1 and holds[0].amount_micro > 25_000, "only the drain is open; every child closed once"
    async with session_maker() as db:
        entries = (await db.execute(select(LedgerEntry))).scalars().all()
    call_id = r.headers["X-Treg-Call-Id"]
    assert sorted((e.call_id, e.kind) for e in entries if (e.call_id or "").startswith(call_id)) == [
        (f"{call_id}:r0", "release"), (f"{call_id}:r0", "reserve"),
        (f"{call_id}:r1", "reserve"), (f"{call_id}:r1", "settle")]


@pytest.mark.parametrize("limit", [0, 50.0])
async def test_a_count_a_routed_child_prices_by_is_refused_before_any_candidate_is_called(
    clients: AsyncClient, monkeypatch, platform_on, limit,
):
    """A routed filter with no range of its own that a v2 child prices its hold by (Hunter's
    `reserve.rows` limit, a whole number from 1 to 100) refuses the routed call itself when the
    route is planned, naming the routed call's own parameter, before any candidate is held or
    called. A whole number above the child's cap only drops that child."""
    import dataclasses
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "icypeas,hunter")
    monkeypatch.setenv("TREG_PLATFORM_KEY_ICYPEAS", "ICYPEAS-KEY")
    monkeypatch.setenv("TREG_PLATFORM_KEY_HUNTER", "HUNTER-KEY")
    get_settings.cache_clear()
    cat = catalog_store.load()
    contract = cat.contracts["people.search"]
    unranged = {k: v for k, v in contract.filters["limit"].items() if k not in ("min", "max")}
    monkeypatch.setitem(cat.contracts, "people.search",
                        dataclasses.replace(contract, filters={**contract.filters, "limit": unranged}))
    hunter = (200, {"data": {"domain": "stripe.com", "emails": [{"value": "a@stripe.com", "first_name": "A",
                                                                  "last_name": "B", "position": "CTO"}]}})

    async def post(asked):
        seen = []
        monkeypatch.setattr(call_service, "relay", _relay_by_provider(
            {"icypeas": [(200, {"leads": [], "total": 0})], "hunter": [hunter]}, seen))
        r = await clients.post("/call/treg.people.search", json={"company_domain": "stripe.com", "limit": asked},
                               headers={"X-Treg-Route-Prefer": "icypeas,hunter"})
        return r, [provider for provider, *_ in seen]

    r, called = await post(limit)
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "catalog_parameter_invalid"
    assert (detail["endpoint_id"], detail["parameter"]) == ("treg.people.search", "limit")
    assert detail["message"] == "treg.people.search needs limit: a whole number from 1 to 100"
    assert called == []
    r, called = await post(10)
    assert r.status_code == 200, r.text
    assert called == ["icypeas", "hunter"]
    assert r.json()["_treg"]["served_by"] == "hunter.companies.emails"
    for above in (150, "150"):
        r, called = await post(above)
        assert r.status_code == 200, r.text
        assert called == ["icypeas"]
        assert {"endpoint_id": "hunter.companies.emails", "why": "needs limit <= 100"} in r.json()["_treg"]["dropped"]


_RANGED_LIMIT = {
    "web.map": ("anyapi,tavily", {"url": "https://example.com"},
                {"anyapi": {"output": {"data": {"results": ["https://example.com/a"]}}},
                 "tavily": {"results": ["https://example.com/a"]}}),
    "people.search": ("harvestapi,hunter", {"company_domain": "stripe.com"},
                      {"harvestapi": {"elements": [{"firstName": "A", "lastName": "B",
                                                    "linkedinUrl": "https://linkedin.com/in/ab"}],
                                      "pagination": {"totalElements": 1}},
                       "hunter": {"data": {"domain": "stripe.com", "emails": [
                           {"value": "a@stripe.com", "first_name": "A", "last_name": "B", "position": "CTO"}]}}}),
}


async def _post_ranged_limit(clients, monkeypatch, capability, limit):
    providers, body, answers = _RANGED_LIMIT[capability]
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", providers)
    for p in providers.split(","):
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{p.upper()}", f"{p.upper()}-KEY")
    get_settings.cache_clear()
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({p: [(200, doc)] * 2 for p, doc in answers.items()}, seen))
    r = await clients.post(f"/call/treg.{capability}", json={**body, "limit": limit})
    get_settings.cache_clear()
    return r, seen


@pytest.mark.parametrize("capability", ["web.map", "people.search"])
@pytest.mark.parametrize("limit", [5.5, "abc", True, 0, -1, " 5"])
async def test_a_routed_limit_outside_its_declared_range_is_refused_before_any_hold(
    clients: AsyncClient, monkeypatch, platform_on, capability, limit,
):
    """web.map and people.search declare `limit` with min 1: a value that is not a whole number of
    at least 1, read as a count is read, is refused naming the routed call's own body.limit before
    anything is planned, held or called."""
    r, seen = await _post_ranged_limit(clients, monkeypatch, capability, limit)
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == {
        "error": "catalog_parameter_invalid", "endpoint_id": f"treg.{capability}", "parameter": "body.limit",
        "message": f"treg.{capability} needs limit: a whole number of at least 1", "charged_micro": 0}
    assert seen == []
    async with session_maker() as db:
        assert (await db.execute(select(LedgerEntry).where(LedgerEntry.kind == "reserve"))).scalars().all() == []


@pytest.mark.parametrize("capability", ["web.map", "people.search"])
@pytest.mark.parametrize("limit", [10, "5", "0005", 1001, 5000, "1" + "0" * 30])
async def test_a_routed_limit_inside_its_declared_range_is_served(
    clients: AsyncClient, monkeypatch, platform_on, capability, limit,
):
    """A JSON integer or ASCII digits of at least 1, however large, passes the range and is sent as
    given; a child whose adapter caps the limit below it is dropped, not the routed call."""
    r, seen = await _post_ranged_limit(clients, monkeypatch, capability, limit)
    assert r.status_code == 200, r.text
    assert seen
    async with session_maker() as db:
        assert (await db.execute(select(LedgerEntry).where(LedgerEntry.kind == "reserve"))).scalars().all()


async def test_a_provider_that_caps_a_filter_is_skipped_above_the_cap(clients: AsyncClient, monkeypatch):
    """`max: {limit: N}` on an adapter: a request asking for more than N skips that provider with a
    named drop instead of answering less than was asked; at or under N it stays a candidate."""
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "akta,predictleads")
    monkeypatch.setenv("TREG_PLATFORM_KEY_AKTA", "AKTA-KEY")
    monkeypatch.setenv("TREG_PLATFORM_KEY_PREDICTLEADS", "PREDICTLEADS-KEY")
    get_settings.cache_clear()
    cat = catalog_store.load()
    import dataclasses
    monkeypatch.setitem(cat.adapters, "akta.companies.news",
                        dataclasses.replace(cat.adapters["akta.companies.news"], filter_max={"limit": 3}))

    class _Org:
        id = 1
        platform_overflow_disabled = False

    class _Caller:
        org_id = 1
        org = _Org()

    ep = cat.by_id["treg.companies.news"]
    options = call_route.RouteOptions.from_headers(lambda key: None)
    over = await call_route.build_plan(ep, {"domain": "canva.com", "limit": 5}, _Caller(), options)
    assert all(c.endpoint["id"] != "akta.companies.news" for c in over.candidates)
    assert {"endpoint_id": "akta.companies.news", "why": "needs limit <= 3"} in over.dropped
    under = await call_route.build_plan(ep, {"domain": "canva.com", "limit": 3}, _Caller(), options)
    assert any(c.endpoint["id"] == "akta.companies.news" for c in under.candidates)
    for limit in ("5", "0" * 5000 + "4", "9" * 5000):
        over = await call_route.build_plan(ep, {"domain": "canva.com", "limit": limit}, _Caller(), options)
        assert {"endpoint_id": "akta.companies.news", "why": "needs limit <= 3"} in over.dropped, limit[:8]
    under = await call_route.build_plan(ep, {"domain": "canva.com", "limit": "03"}, _Caller(), options)
    assert any(c.endpoint["id"] == "akta.companies.news" for c in under.candidates)


def test_a_per_result_child_is_ranked_free_on_a_miss_unless_its_cost_bills_one():
    """A per-result price bills the rows a provider returns, so an empty answer is free and the
    expected cost per hit is the hit price, as for per-success. A `call_fee` bills every run, hit
    or miss; `bills_miss` bills the whole request on a miss, or `miss_units` rows of it."""
    from treg.domain.catalog.routing import contracts as C, plan as RP

    def view(cost):
        return {"usd": 0.001, "per": 1, **cost}

    def cand(cost, limit=1):
        cv, req = view(cost), {"limit": limit}
        price = RP.cost_at(cv, req)
        return RP.Candidate(endpoint={"id": "x.find", "provider": "x", "cost": cost},
                            adapter=C.Adapter("x.find", (("q",),), {"q": "body.q"}, {}, "rows == []"),
                            variant=("q",), tier="platform", price_micro=price, hit_rate=0.25,
                            ok_rate=None, p50_ms=None, last_ok_days=None,
                            miss_micro=RP.miss_charge_micro(cv, req, None, price))

    assert cand({"type": "per_result"}).expected_cost_per_hit == 1_000
    assert cand({"type": "per_success"}).expected_cost_per_hit == 1_000
    assert cand({"type": "per_result", "bills_miss": True}).expected_cost_per_hit == 4_000
    assert cand({"type": "per_call"}).expected_cost_per_hit == 4_000
    # The start fee is in the hit price too: 1,000 + 200 a hit, 200 for each of three misses.
    fee = cand({"type": "per_result", "call_fee": 0.0002})
    assert fee.price_micro == 1_200 and fee.miss_micro == 200 and fee.expected_cost_per_hit == 1_800
    # A one-row minimum on an empty page bills one row, not the twenty asked for.
    floor = cand({"type": "per_result", "bills_miss": True, "miss_units": 1}, limit=20)
    assert floor.price_micro == 20_000 and floor.miss_micro == 1_000
    assert floor.expected_cost_per_hit == 23_000
    # A per-success partial miss charge is settled from the provider's report and ranked free.
    assert cand({"type": "per_success", "bills_miss": True}).expected_cost_per_hit == 1_000
    assert RP.bills_miss({"type": "per_success", "bills_miss": True})
    assert not RP.bills_miss({"type": "per_result"})


def _with_page_size(record: dict, n: int) -> dict:
    """A replay record whose request body asks for `n` rows wherever it names a page size."""
    import base64

    request = dict(record["request"])
    body = json.loads(base64.b64decode(request["body_b64"]))
    for key in ("pageSize", "limit", "size", "count", "per_page"):
        if key in body:
            body[key] = n
    request["body_b64"] = base64.b64encode(json.dumps(body).encode()).decode()
    return {**record, "request": request}


def test_a_routed_per_result_child_settles_a_miss_at_what_ranking_assumes():
    """Each routed per-result child's synthetic miss is replayed through settlement. A child ranked
    free on a miss must settle it at zero; one that bills a miss must settle it at the charge
    ranking prices (`miss_units` rows, else the whole hold). A synthetic miss keeps the hit
    example's reported charge, so a child that settles on the provider's own report cannot show
    its zero here: those are listed with the evidence that an empty answer reports zero. A child
    whose provider bills each returned row, a row its adapter reads as a miss included, settles
    its synthetic miss (the one row asked for) at the hold."""
    from treg.application.replay import engine, fixtures
    from treg.domain.catalog.routing.plan import bills_miss, cost_at

    reported_zero = {
        "crawl4ai.people.search": "x-c4-cost header; an empty answer charges 0",
        "crustdata.companies.jobs.search": "X-Credits-Used header; empty results consume none",
        "crustdata.companies.search": "X-Credits-Used header; charged per company returned",
        "crustdata.people.search": "X-Credits-Used header; charged per profile returned",
        "dropleads.companies.enrich": "credits.creditsDeducted; charged per returned company",
        "dropleads.companies.search": "credits.creditsDeducted; empty results are free",
        "fiber-ai.companies.search": "chargeInfo.creditsCharged; charged per company returned",
        "fiber-ai.people.search": "chargeInfo.creditsCharged; charged per profile returned",
        "leadmagic.x.companies-search-v3": "credits_consumed; free if zero matches",
        "leadmagic.x.employee-finder": "credits_consumed; free if no results",
        "lusha.companies.search": "billing.creditsCharged; an unmatched search charges 0",
        "lusha.people.search": "billing.creditsCharged; an unmatched search charges 0",
        "quickenrich.companies.search": "meta.credits_used; empty results free",
        "quickenrich.people.search.domain": "meta.credits_used; empty pages are free",
        "sumble.organizations": "credits_used; unmatched inputs free",
    }
    billed_per_row = {
        "tavily.web.extract": "a returned page without raw_content is a routing miss and a billed result",
    }
    cat = catalog_store.load()
    kids = {c for r in cat.by_id.values() if r.get("kind") == "routed" for c in r["routed_children"]}
    wrong = []
    for rec in fixtures.generate():
        eid = rec["endpoint_id"]
        cost = cat.by_id[eid].get("cost") or {} if eid in kids else {}
        # a row that settles on the provider's reported charge pays what it reports, 0 for an empty page
        if (not rec["rid"].endswith(":miss") or cost.get("type") != "per_result" or eid in reported_zero
                or cost.get("reported_charge")):
            continue
        line = engine.replay(rec)
        if (line.get("routing") or {}).get("kind") != "miss":
            continue
        charged = (line["settle"] or {}).get("charged_micro")
        cv = cat.cost_view(cost, cat.by_id[eid]["provider"])
        if eid in billed_per_row:
            expected = line["reserve"]["hold_micro"]
        elif not bills_miss(cost):
            expected = 0
        elif cost.get("bills_miss") is not True:
            continue  # a call_fee alone: the fee, checked where the run body is a dataset list
        elif cost.get("miss_units"):
            # The floor holds at any page size: ask for twenty rows and still pay `miss_units`.
            line = engine.replay(_with_page_size(rec, 20))
            charged = (line["settle"] or {}).get("charged_micro")
            expected = cost_at(cv, {"limit": cost["miss_units"]})
        else:
            expected = line["reserve"]["hold_micro"]
        if charged != expected:
            wrong.append((eid, charged, expected))
    assert wrong == []


@pytest.mark.parametrize("limit", [1, 3])
def test_a_whole_request_miss_is_ranked_at_the_hold_the_routed_request_reserves(limit):
    """A per-result child that settles a miss at its whole hold must be ranked at that hold for
    the request its adapter actually sends: a fixed page size (`const`) or a list the caller sent
    sets the units, not the contract's `limit` alone. Two keywords, a fixed ten-row page and a
    limit of 1 or 3 all have to agree with what `/call` reserves and settles."""
    from treg.application.call.resolve import QueryValues, price_call
    from treg.domain.catalog.routing.contracts import canonical_identity
    from treg.domain.catalog.routing.plan import miss_charge_micro

    cat = catalog_store.load()
    examples = {"domain": "stripe.com", "company_domain": "stripe.com", "url": "https://stripe.com"}
    wrong = []
    for row in (r for r in cat.by_id.values() if r.get("kind") == "routed"):
        contract = cat.contracts[row["capability"]]
        for eid in row["routed_children"]:
            ep = cat.by_id[eid]
            cost = ep.get("cost") or {}
            if cost.get("type") != "per_result" or cost.get("bills_miss") is not True or cost.get("miss_units"):
                continue
            adapter = cat.adapters[eid]
            ident = {k: (["alpha", "beta"] if contract.identity_types.get(k) == "list" else examples.get(k, "alpha"))
                     for k in adapter.accepts[0]}
            if "limit" in (contract.filters or {}):
                ident["limit"] = limit
            ident, _ = canonical_identity(contract, ident)
            query, body = adapter.to_upstream(ident)
            hold = price_call(ep, QueryValues(tuple((str(k), str(v)) for k, v in (query or {}).items())),
                              json.dumps(body).encode() if body else b"", set()).estimate_micro
            ranked = miss_charge_micro(cat.cost_view(cost, ep["provider"]), ident, adapter)
            if ranked != hold:
                wrong.append((eid, ranked, hold))
    assert wrong == []


@pytest.fixture
def findymail_and_prospeo_on(monkeypatch, platform_on):
    for p in ("FINDYMAIL", "PROSPEO"):
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{p}", f"PLATFORM-{p}-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "findymail,prospeo")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_a_company_scoped_people_search_tries_findymail_before_prospeo(clients, findymail_and_prospeo_on):
    """findymail.search.employees bills per contact found and nothing when none: unmeasured, its
    expected cost per hit is its hit price, not twice that, so it goes ahead of prospeo's dearer
    per-success page for a one-person {company_domain, title} search."""
    options = call_route.RouteOptions.from_headers(lambda key: None)
    caller = SimpleNamespace(org_id=1, org=SimpleNamespace(id=1, platform_overflow_disabled=False))
    plan = await call_route.build_plan(catalog_store.load().by_id["treg.people.search"],
                                       {"company_domain": "stripe.com", "title": "CTO", "limit": 1}, caller, options)
    by = {c.endpoint["provider"]: c for c in plan.candidates}
    assert [c.endpoint["provider"] for c in plan.candidates][:2] == ["findymail", "prospeo"]
    findymail = by["findymail"]
    assert findymail.endpoint["id"] == "findymail.search.employees" and findymail.hit_rate is None
    # Price decides once both use the title and the domain: it must not double findymail's price.
    assert findymail.expected_cost_per_hit == findymail.price_micro
    assert findymail.expected_cost_per_hit < by["prospeo"].expected_cost_per_hit


def test_routed_miss_count_includes_a_per_success_child_that_bills_a_miss():
    """A per-success child is free on a miss unless cost.bills_miss is true; the routed row counts
    those children in miss_billed_by and never claims misses are free while one of them bills."""
    from treg.domain.catalog.routing import contracts as C, synthetic

    contract = C.Contract(capability="x.find", summary="", identity=(("q",),), identity_types={"q": "str"},
                          derive={}, filters={}, output={"rows": {"required": True}}, miss="rows == []")
    ids = ("a.find", "b.find", "c.find")
    adapters = {eid: C.Adapter(eid, (("q",),), {"q": "body.q"}, {"rows": "rows"}, "rows == []",
                               route=True, verified=True) for eid in ids}
    endpoints = [
        {"id": "a.find", "provider": "a",
         "cost": {"type": "per_success", "value": 1, "bills_miss": True}},
        {"id": "b.find", "provider": "b",
         "cost": {"type": "per_success", "value": 2}},
        {"id": "c.find", "provider": "c",
         "cost": {"type": "per_call", "value": 3}},
    ]

    def cost_view(cost, provider):
        return {"usd": float(cost["value"])} if cost and cost.get("value") is not None else None

    row = synthetic.routed_endpoint(contract, endpoints, adapters, cost_view)
    assert row["miss_billed_by"] == ["a.find", "c.find"]
    assert "b.find" not in row["miss_billed_by"]
    assert row["cost"].get("varies") is True
    assert "misses are free" not in row["cost"]["note"]
    assert row["cost"]["note"].startswith("Not free:")

    free_only = [
        {"id": "b.find", "provider": "b", "cost": {"type": "per_success", "value": 2}},
        {"id": "d.find", "provider": "d", "cost": {"type": "per_success", "value": 4}},
    ]
    free_adapters = {eid: C.Adapter(eid, (("q",),), {"q": "body.q"}, {"rows": "rows"}, "rows == []",
                                    route=True, verified=True) for eid in ("b.find", "d.find")}
    free_row = synthetic.routed_endpoint(contract, free_only, free_adapters, cost_view)
    assert free_row["miss_billed_by"] == []
    assert "misses are free" in free_row["cost"]["note"]
    assert free_row["cost"].get("varies") is not True


# ---- a provider first by the domain's suffix (`prefer_by_domain_suffix`) ---------------------------
def _suffix_contract(rule, **extra):
    from treg.domain.catalog.routing.contracts import parse_contracts
    return parse_contracts({"contracts": {"x.find": {
        "identity": [{"full_name": "str", "domain": "str"}, {"linkedin_url": "url"}],
        "output": {"email": {"type": "str"}}, "prefer_by_domain_suffix": rule, **extra}}})["x.find"]


@pytest.mark.parametrize("domain, first", [
    ("acme.fr", ["alpha"]), ("Paris.Acme.FR.", ["alpha"]), (" acme.fr ", ["alpha"]),
    ("acme.com", []), ("frenchco.com", []), ("acme.fr.example.com", []),
])
def test_a_suffix_rule_matches_whole_labels_at_the_end_of_the_domain(domain, first):
    from treg.domain.catalog.routing.contracts import default_prefer
    contract = _suffix_contract({"fr": ["Alpha"]})
    ident, _ = canonical_identity(contract, {"full_name": "Jean Dupont", "domain": domain})
    assert default_prefer(contract, ident) == first
    linkedin, _ = canonical_identity(contract, {"linkedin_url": "https://www.linkedin.com/in/x"})
    assert default_prefer(contract, linkedin) == [], "no domain, no suffix rule"


def test_prefer_by_domain_suffix_takes_the_longest_suffix_then_the_contract_prefer():
    from treg.domain.catalog.routing.contracts import default_prefer, parse_contracts
    doc = {"contracts": {"x.find": {
        "identity": [{"full_name": "str", "domain": "str"}], "output": {"email": {"type": "str"}},
        "prefer": ["tomba", "hunter"], "prefer_by_domain_suffix": {".uk": ["hunter"], "co.uk": ["enrow"]}}}}
    contract = parse_contracts(doc)["x.find"]
    assert default_prefer(contract, {"domain": "acme.co.uk"}) == ["enrow", "tomba", "hunter"]
    assert default_prefer(contract, {"domain": "acme.org.uk"}) == ["hunter", "tomba"]
    assert default_prefer(contract, {"domain": "acme.com"}) == ["tomba", "hunter"]


@pytest.mark.parametrize("rule", [
    {"fr": []}, {"fr": "enrow"}, ["fr"], {"fr": [None]}, {"fr": [""]}, {"fr": ["  "]}, {"fr": [1]},
    {"": ["enrow"]}, {".": ["enrow"]}, {}, [], False, "fr",
])
def test_a_malformed_prefer_by_domain_suffix_fails_the_load(rule):
    from treg.domain.catalog.routing.contracts import parse_contracts
    doc = {"contracts": {"x.find": {"identity": [{"full_name": "str", "domain": "str"}],
                                    "output": {"email": {"type": "str"}}, "prefer_by_domain_suffix": rule}}}
    with pytest.raises(ValueError, match="prefer_by_domain_suffix"):
        parse_contracts(doc)


def test_prefer_by_domain_suffix_needs_a_domain_identity():
    from treg.domain.catalog.routing.contracts import parse_contracts
    doc = {"contracts": {"x.find": {"identity": [{"linkedin_url": "url"}], "output": {"email": {"type": "str"}},
                                    "prefer_by_domain_suffix": {"fr": ["enrow"]}}}}
    with pytest.raises(ValueError, match="domain"):
        parse_contracts(doc)


@pytest.mark.parametrize("domain, header, order", [
    ("stripe.fr", None, ["hunter"]),                   # the suffix rule puts hunter ahead of cheaper tomba
    ("stripe.com", None, ["tomba"]),         # any other domain keeps cost per hit
    ("stripe.fr", "tomba", ["tomba"]),       # the caller's own preference replaces the rule
])
async def test_a_routed_find_asks_the_suffix_provider_first(clients, enrichment_on, monkeypatch, domain, header, order):
    import dataclasses
    cat = catalog_store.load()
    monkeypatch.setitem(cat.contracts, "people.email.find", dataclasses.replace(
        cat.contracts["people.email.find"], prefer_by_domain_suffix={"fr": ("hunter",)}))
    seen = []
    miss_tomba = (200, {"data": {"email": None}})
    hit = (200, {"data": {"email": f"p@{domain}", "score": 90, "verification": {"status": "valid"}}})
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [miss_tomba], "findymail": [(200, {"contact": {"name": "P C", "email": None}})], "hunter": [hit]}, seen))
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": domain},
                           headers={"X-Treg-Route-Prefer": header} if header else {})
    assert r.status_code == 200, r.text
    assert [p for p, *_ in seen][:len(order)] == order and r.json()["_treg"]["served_by"] == "hunter.people.email.find"


@pytest.mark.parametrize("contents", [True, False, None])
def test_a_web_search_that_asks_for_passages_asks_exa_for_the_ones_matching_q(contents):
    """`contents: true` asks exa for the passages that match q, at its search price; `false` or no
    flag leaves Exa's request unchanged. A published route rule puts exa first for it
    (test_route_rules.py)."""
    body = {"q": "top B2B sales influencers on Instagram", **({} if contents is None else {"contents": contents})}
    cat = catalog_store.load()
    ident, _ = canonical_identity(cat.contracts["web.search"], body)
    sent = cat.adapters["exa.web.search"].to_upstream(ident, ("q",))[1]
    if contents:
        assert sent["contents"] == {"highlights": {"query": body["q"], "maxCharacters": 2000}}
    else:
        assert "contents" not in sent


def test_a_flag_that_is_off_leaves_the_provider_sample_where_it_was():
    """An unsent or `false` flag asks for nothing: the sample hash sees the identity it saw before the
    filter existed, so a request that never sends `contents` keeps its provider."""
    from treg.domain.catalog.routing.contracts import flags_off
    from treg.domain.catalog.routing.plan import sampled_provider_allowed

    contract = catalog_store.load().contracts["web.search"]
    before = {"q": "example", "limit": 10}
    for body in ({"q": "example"}, {"q": "example", "contents": False}):
        ident, _ = canonical_identity(contract, body)
        assert flags_off(contract, ident) == before
    asked, _ = canonical_identity(contract, {"q": "example", "contents": True})
    assert flags_off(contract, asked)["contents"] is True
    picks = [sampled_provider_allowed("crawl4ai", {"crawl4ai": 50}, org_id=o, capability="web.search", identity=before)
             for o in range(40)]
    assert picks == [sampled_provider_allowed("crawl4ai", {"crawl4ai": 50}, org_id=o, capability="web.search",
                                              identity=flags_off(contract, canonical_identity(contract, {"q": "example"})[0]))
                     for o in range(40)]


@pytest.mark.parametrize("value", ["false", 0, 1, "true", [True]])
async def test_a_flag_that_is_not_a_bool_is_refused_before_any_provider(clients, platform_on, monkeypatch, value):
    for p in ("CRAWL4AI", "EXA"):
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{p}", f"PLATFORM-{p}-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "crawl4ai,exa")
    get_settings.cache_clear()
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"*": [(200, {"results": []})] * 2}, seen))
    r = await clients.post("/call/treg.web.search", json={"q": "example", "contents": value})
    get_settings.cache_clear()
    assert r.status_code == 422 and r.json()["detail"]["error"] == "invalid_identity" and seen == [], r.text


async def test_passages_asked_for_and_not_served_are_named_in_ignored_filters(clients, platform_on, monkeypatch):
    """Exa fails: the next provider answers without passages, and the answer says it ignored `contents`."""
    for p in ("CRAWL4AI", "EXA"):
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{p}", f"PLATFORM-{p}-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "crawl4ai,exa")
    get_settings.cache_clear()
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"exa": [(500, {"error": "down"})], "*": [(200, {"results": [{"url": "https://example.com", "title": "t"}]})] * 2}, seen))
    r = await clients.post("/call/treg.web.search", json={"q": "example", "contents": True},
                           headers={"X-Treg-Route-Prefer": "exa"})
    get_settings.cache_clear()
    assert r.status_code == 200, r.text
    assert [p for p, *_ in seen][0] == "exa" and r.json()["_treg"]["ignored_filters"] == ["contents"]


async def test_a_request_without_the_flag_samples_providers_as_before(clients, platform_on, monkeypatch):
    """Through build_plan: with crawl4ai sampled at 50%, whether it is in the plan for a request that
    never sends `contents` is decided on the identity the filter did not exist in."""
    from treg.domain.catalog.routing.plan import sampled_provider_allowed

    for p in ("CRAWL4AI", "EXA"):
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{p}", f"PLATFORM-{p}-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "crawl4ai,exa")
    monkeypatch.setenv("TREG_ROUTE_PROVIDER_SAMPLE_PCT", '{"crawl4ai":50}')
    get_settings.cache_clear()
    seen = set()
    for i in range(12):
        q = f"example {i}"
        want = sampled_provider_allowed("crawl4ai", {"crawl4ai": 50}, org_id=1, capability="web.search",
                                        identity={"q": q, "limit": 10})
        seen.add(want)
        for body in ({"q": q}, {"q": q, "contents": False}):
            assert ("crawl4ai" in await _first_providers("treg.web.search", body, n=9)) is want, body
    get_settings.cache_clear()
    assert seen == {True, False}, "the sample split the questions both ways"


def test_only_bool_filters_get_the_strict_flag_check():
    """`ai_overview` is a bool identity key of google.serp.organic: a blank one is still not sent, as before."""
    from treg.domain.catalog.routing.contracts import invalid_identity

    cat = catalog_store.load()
    assert invalid_identity(cat.contracts["google.serp.organic"], {"q": "example", "ai_overview": ""}) == []
    assert invalid_identity(cat.contracts["web.search"], {"q": "example", "contents": ""})[0]["type"] == "bool"


async def test_a_merged_answer_names_a_filter_any_contributing_provider_ignored(clients, platform_on, monkeypatch):
    """contents asked, rows merged from exa (passages) and crawl4ai (snippets): the answer says contents
    was ignored for some rows, on the body and the header, not only in tried[]."""
    for p in ("CRAWL4AI", "EXA"):
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{p}", f"PLATFORM-{p}-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "crawl4ai,exa")
    get_settings.cache_clear()
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"exa": [(200, {"results": [{"url": "https://a.example", "title": "a", "highlights": ["x"]}]})],
         "*": [(200, {"results": [{"url": "https://b.example", "title": "b"}]})] * 2}, seen))
    r = await clients.post("/call/treg.web.search", json={"q": "example", "contents": True},
                           headers={"X-Treg-Route-Merge": "1", "X-Treg-Route-Min-Results": "3"})
    get_settings.cache_clear()
    assert r.status_code == 200, r.text
    t = r.json()["_treg"]
    assert len(t.get("merged_from") or []) == 2, t
    assert t["ignored_filters"] == ["contents"] and r.headers.get("x-treg-ignored-filters") == "contents"


async def test_a_search_contract_advice_rides_on_every_hit(clients, platform_on, monkeypatch):
    """A search contract has no `verified` output, so its `advice_unverified` sentence reaches the
    agent on every hit, where it chooses its next call."""
    import dataclasses
    cat = catalog_store.load()
    monkeypatch.setitem(cat.contracts, "instagram.search.users", dataclasses.replace(
        cat.contracts["instagram.search.users"], advice_unverified="Try the other search."))
    monkeypatch.setenv("TREG_PLATFORM_KEY_SCRAPECREATORS", "PLATFORM-SC-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "scrapecreators")
    get_settings.cache_clear()
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"*": [(200, {"profiles": [{"username": "a", "url": "https://www.instagram.com/a/"}]})] * 2}, seen))
    r = await clients.post("/call/treg.instagram.search.users", json={"q": "productivity coach Brazil"})
    get_settings.cache_clear()
    assert r.status_code == 200, r.text
    assert r.json()["_treg"]["advice"] == "Try the other search."


def test_instagram_handles_reads_the_accounts_a_page_names():
    """Handles from `instagram.com/<handle>` anywhere and `@handle` on a page about Instagram; an
    email address, Instagram's own paths and a repeat are no account."""
    from treg.domain.catalog.routing import paths as P

    results = [
        {"url": "https://example.com/list", "highlights": [
            "Follow @coffee.one (12K followers) and instagram.com/roaster_two/ on Instagram. Mail hi@example.com, see instagram.com/p/abc. Top 10 of @2024: instagram.com/433/"]},
        {"url": "https://example.org/x", "highlights": ["Twitter: @someone_else"]},
        {"url": "https://example.net/y", "highlights": ["Also instagram.com/coffee.one and @third on Instagram"]},
    ]
    rows = P.evaluate("instagram_handles(results)", {"results": results})
    assert [r["username"] for r in rows] == ["coffee.one", "roaster_two", "433", "third"], "a linked all-digit handle is an account"
    assert rows[0]["url"] == "https://www.instagram.com/coffee.one/" and "12K followers" in rows[0]["biography"]
    assert rows[0]["matched_from"] == "example.com", "the site, not the page url: every row is read"
    assert all(len(r["biography"]) <= 175 for r in rows)
    assert P.evaluate("instagram_handles(results)", {"results": None}) is None


def test_fill_rows_fills_every_request_and_needs_a_list_output():
    from treg.domain.catalog.routing import contracts as C

    doc = {"identity": [{"q": "str"}], "output": {"users": {"type": "list", "required": True}}, "miss": "users == []"}
    c = C.parse_contracts({"contracts": {"y": {**doc, "fill_rows": 25}}})["y"]
    assert c.fill_target({"q": "x"}) == 25
    for bad in (1, True, "25", 0):
        with pytest.raises(ValueError, match="fill_rows"):
            C.parse_contracts({"contracts": {"y": {**doc, "fill_rows": bad}}})
    with pytest.raises(ValueError, match="fill_rows"):
        C.parse_contracts({"contracts": {"y": {**doc, "output": {"a": "str"}, "fill_rows": 25}}})


@pytest.mark.parametrize("bio_rows, providers", [(10, ["scrapecreators", "exa"]), (25, ["scrapecreators"])])
async def test_an_instagram_search_adds_directory_accounts_to_a_short_bio_answer(
        clients, platform_on, monkeypatch, bio_rows, providers):
    """Bio search first; under 25 accounts, the directory search adds the accounts its pages name,
    a repeat counted once."""
    for p in ("SCRAPECREATORS", "EXA"):
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{p}", f"PLATFORM-{p}-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "scrapecreators,exa")
    get_settings.cache_clear()
    bios = [{"username": f"bio{i}", "url": f"https://www.instagram.com/bio{i}"} for i in range(bio_rows)]
    page = {"results": [{"url": "https://example.com/list",
                         "highlights": ["Instagram picks: @bio0, @dir_one and instagram.com/dir_two/"]}]}
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"scrapecreators": [(200, {"profiles": bios})], "exa": [(200, page)]}, seen))
    r = await clients.post("/call/treg.instagram.search.users", json={"q": "specialty coffee Melbourne"})
    get_settings.cache_clear()
    assert r.status_code == 200, r.text
    assert [p for p, *_ in seen] == providers
    users = r.json()["output"]["users"]
    if bio_rows < 25:
        assert len(users) == bio_rows + 2 and {"dir_one", "dir_two"} <= {u["username"] for u in users}
        assert seen[1][3]["query"] == "specialty coffee Melbourne Instagram influencers"
    else:
        assert len(users) == 25


async def test_a_merged_instagram_answer_keeps_the_first_providers_details(clients, platform_on, monkeypatch):
    """The directory search names all ten bio accounts and fifteen more, as many rows as the merge:
    the union still leads with the bio rows, follower counts kept; a TikHub row for the same
    account is the same account."""
    for p in ("SCRAPECREATORS", "EXA", "TIKHUB"):
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{p}", f"PLATFORM-{p}-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "scrapecreators,exa,tikhub")
    get_settings.cache_clear()
    bios = [{"username": f"acct{i}", "url": f"https://www.instagram.com/acct{i}", "follower_count": 1000 + i}
            for i in range(10)]
    page = {"results": [{"url": "https://example.com/list", "highlights": [
        "Instagram: " + " ".join(f"@acct{i}" for i in range(25))]}]}
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"scrapecreators": [(200, {"profiles": bios})], "exa": [(200, page)]}, seen))
    r = await clients.post("/call/treg.instagram.search.users", json={"q": "coffee Melbourne"})
    assert r.status_code == 200, r.text
    users = r.json()["output"]["users"]
    assert len(users) == 25 and [u.get("follower_count") for u in users[:10]] == [1000 + i for i in range(10)]
    assert r.json()["_treg"]["merged_from"] == ["scrapecreators.x.v1-instagram-search-profiles", "exa.instagram.search.users"]
    assert [p for p, *_ in seen] == ["scrapecreators", "exa"], "25 accounts reached: no third provider is paid"

    # bio search short, directory empty: TikHub fills, and an account it shares counts once
    seen.clear()
    tik = {"data": {"data": {"items": [{"username": "acct1", "full_name": "A"}, {"username": "new_one"}]}}}
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"scrapecreators": [(200, {"profiles": bios[:3]})], "exa": [(200, {"results": []})],
         "tikhub": [(200, tik)] * 2}, seen))
    r = await clients.post("/call/treg.instagram.search.users", json={"q": "coffee Sydney"})
    get_settings.cache_clear()
    names = [u["username"] for u in r.json()["output"]["users"]]
    assert sorted(names) == ["acct0", "acct1", "acct2", "new_one"], names

    # a later provider that repeats its own rows does not displace an earlier account
    seen.clear()
    dup = {"data": {"data": {"items": [{"username": u} for u in ("acct1", "acct2") * 3]}}}
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"scrapecreators": [(200, {"profiles": bios[:3]})], "exa": [(200, {"results": []})],
         "tikhub": [(200, dup)] * 2}, seen))
    get_settings.cache_clear()
    for p in ("SCRAPECREATORS", "EXA", "TIKHUB"):
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{p}", f"PLATFORM-{p}-KEY")
    r = await clients.post("/call/treg.instagram.search.users", json={"q": "coffee Perth"})
    get_settings.cache_clear()
    assert [u["username"] for u in r.json()["output"]["users"]] == ["acct0", "acct1", "acct2"]


async def test_waterfall_off_miss_after_a_billed_failure_is_incomplete_and_bills_only_the_miss(
    clients: AsyncClient, enrichment_on, monkeypatch,
):
    """With the waterfall off the walk ends on the first miss; a billed answer that failed before it
    (an adapter that could not read it) is refunded there too, and the miss says it is incomplete."""
    patched_cat = _patched_catalog_all_email_find_throw(catalog_store.load())
    monkeypatch.setattr(catalog_store, 'load', lambda: patched_cat)
    seen = []
    monkeypatch.setattr(call_service, 'relay', _relay_by_provider({
        'findymail': [(200, {'contact': None})],
        '*': [(200, {'data': {'email': 'x@test.test'}})] * 5,
    }, seen))
    before = await _balance(clients)
    r = await clients.post(f'/call/{ROUTED}', json={'full_name': 'Example Person', 'domain': 'example.com'},
                           headers={'X-Treg-Route-Waterfall': '0', 'X-Treg-Route-Prefer': 'tomba,findymail'})
    assert r.status_code == 200, r.text
    doc = r.json()['_treg']
    assert [(t['provider'], t['outcome']) for t in doc['tried']] == [('tomba', 'error'), ('findymail', 'miss')]
    assert doc['outcome'] == 'miss' and doc['incomplete'] is True
    assert r.headers['X-Treg-Route-Incomplete'] == 'true'
    assert doc['tried'][0]['charged_micro'] == 0
    assert doc['charged_micro'] == doc['tried'][1]['charged_micro'] == before - await _balance(clients)
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []


def test_carried_by_keeps_a_child_that_sends_a_key_naming_the_same_subject():
    """A name and a domain name one company: with `carried_by` saying so, a child that sends either
    answers a {name, domain} request. A title it never sends is still a different question."""
    from treg.domain.catalog.routing import contracts as C
    from treg.domain.catalog.routing.plan import unscoped

    contract = C.parse_contracts({"contracts": {"x.jobs": {
        "identity": [{"title": "str", "domain": "str"}, {"name": "str"}, {"domain": "str"}],
        "scoping": ["title", "name", "domain"], "carried_by": {"name": ["domain"], "domain": ["name"]},
        "output": {"jobs": {"type": "list", "required": True}}, "miss": "jobs == []"}}})["x.jobs"]
    by_domain = C.parse_adapters({"adapters": {"p.jobs": {
        "accepts": [["domain"]], "in": {"domain": "query.domain"}, "out": {"jobs": "jobs"},
        "miss": "jobs == []"}}})["p.jobs"]
    assert unscoped(by_domain, contract, {"name": "Ramp", "domain": "ramp.com"}) == ()
    assert unscoped(by_domain, contract, {"title": "data engineer", "domain": "ramp.com"}) == ("title",)
    plain = C.Contract(**{**contract.__dict__, "carried_by": {}})
    assert unscoped(by_domain, plain, {"name": "Ramp", "domain": "ramp.com"}) == ("name",)


def test_a_filter_nested_in_a_larger_value_is_unmappable_when_its_call_yields_nothing():
    """`list(obj('country', country_name(country)))` drops a code the provider cannot name: the
    planner must see it as not sent (disclosed, strict-dropped), while a nameable code is sent, a
    field literal named like the key does not count, and a key also sent as given is mappable."""
    from treg.domain.catalog.routing import contracts as C
    from treg.domain.catalog.routing.plan import _unmappable

    def adapter(expr):
        return C.parse_adapters({"adapters": {"p.jobs": {
            "accepts": [["title"]], "in": {"title": "body.title"}, "in_expr": {"body.geo": expr},
            "out": {"jobs": "jobs"}, "miss": "jobs == []"}}})["p.jobs"]

    nested = adapter("list(obj('country', country_name(country)))")
    assert _unmappable(nested, "country", {"title": "x", "country": "UK"})
    assert not _unmappable(nested, "country", {"title": "x", "country": "GB"})
    assert not _unmappable(adapter("list(obj('country', country))"), "country", {"title": "x", "country": "UK"})
    # beside another place key in the same value: a nameable city does not carry an unnameable country
    multi = adapter("list(obj('city', city, 'country', country_name(country)))")
    assert _unmappable(multi, "country", {"title": "x", "city": "London", "country": "UK"})
    assert not _unmappable(multi, "country", {"title": "x", "city": "London", "country": "GB"})


async def test_a_routed_read_stops_at_eight_mib():
    """An anonymous or own-key child streams; the router must not hold an answer past 8 MiB."""
    async def body():
        for _ in range(9):
            yield b"x" * (1024 * 1024)

    closed = []

    async def close():
        closed.append(True)

    with pytest.raises(call_route._ChildTooLarge):
        await call_route._read(UpstreamResponse(200, (), body(), close))
    assert closed == [True]


def test_ignoring_a_sent_filter_outranks_a_contract_prefer_but_not_the_callers():
    """A contract's `prefer` (or a published rule) orders children that ask the real question; a
    preferred child that drops a filter the caller sent answers a looser one, so it goes after. Only
    the caller's own X-Treg-Route-Prefer may put it first."""
    def cand(eid, ignored=()):
        return Candidate(endpoint={"id": eid, "provider": eid.split(".")[0], "cost": {"type": "per_result"}},
                         adapter=None, variant=("title",), tier="platform", price_micro=1_000, hit_rate=None,
                         ok_rate=None, p50_ms=None, last_ok_days=None, ignored=ignored)
    blind, aware = cand("a.jobs.search", ignored=("country",)), cand("b.jobs.search")
    assert [c.endpoint["id"] for c in rank([blind, aware], prefer=["a"], given={"title", "country"})] == [
        "b.jobs.search", "a.jobs.search"]
    assert [c.endpoint["id"] for c in rank([blind, aware], prefer=["a"], given={"title", "country"},
                                           caller_prefer=True)] == ["a.jobs.search", "b.jobs.search"]


async def test_an_oversized_child_answer_is_unbilled_and_the_next_provider_answers(
        clients: AsyncClient, enrichment_on, monkeypatch):
    """Only an unmetered child (own key or anonymous) streams unbuffered, so only it can pass the
    routed read's bound in production. Past it, the answer is that child's error: nothing billed, it
    counts toward the error allowance, and the next candidate answers the call."""
    monkeypatch.setattr(call_route, "ROUTED_BODY_MAX", 512)
    assert (await clients.post("/secrets", json={"name": "tomba", "value": "OWN-TOMBA"})).status_code == 200
    seen, own = [], []
    relay = _relay_by_provider({
        "tomba": [(200, {"data": {"email": "patrick@stripe.com", "score": 90, "first_name": "x" * 600,
                                  "verification": {"status": "valid"}}})],
        "findymail": [(200, {"contact": {"name": "Patrick Collison", "email": "patrick@stripe.com"}})],
    }, seen)

    async def spy(request, upstream_url, tool, secrets, *args, **kwargs):
        if "tomba" in upstream_url:
            own.append(sorted(getattr(x, "name", "") for x in secrets.values()))
        return await relay(request, upstream_url, tool, secrets, *args, **kwargs)
    monkeypatch.setattr(call_service, "relay", spy)
    before = await _balance(clients)
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"},
                           headers={"X-Treg-Route-Prefer": "tomba,findymail"})
    assert r.status_code == 200, r.text
    doc = r.json()["_treg"]
    assert [(t["provider"], t["outcome"]) for t in doc["tried"]] == [("tomba", "error"), ("findymail", "hit")]
    detail = doc["tried"][0]["detail"]
    # the dedicated branch, not the generic read failure: it names the direct call that streams
    assert doc["tried"][0]["charged_micro"] == 0 and "routed read" in detail
    assert "directly, it streams" in detail and not detail.startswith("failed to read"), detail
    assert own == [["tomba"]], own  # asked on the team's own key, so unmetered
    assert doc["served_by"].startswith("findymail.")
    assert doc["charged_micro"] == doc["tried"][1]["charged_micro"] == before - await _balance(clients)
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []


async def test_a_same_named_secret_never_makes_a_public_provider_an_own_key_child(
        clients: AsyncClient, monkeypatch):
    """A public provider has nothing to connect, so the router plans its reader on the anonymous
    tier even when the team saved a secret under the provider's name."""
    import dataclasses
    from treg import oauth_providers
    from treg.models import Org
    monkeypatch.setitem(oauth_providers.REGISTRY, "financialdatasets", dataclasses.replace(
        oauth_providers.REGISTRY["financialdatasets"], auth_kind="public"))
    cat = catalog_store.load()
    monkeypatch.setitem(cat.by_id["financialdatasets.prices.snapshot"], "platform_auth", "anonymous")
    monkeypatch.setitem(cat.by_id["financialdatasets.prices.snapshot"], "cost", {
        "type": "free", "value": 0, "currency": "USD", "unit": "call"})
    monkeypatch.setattr(get_settings(), "platform_providers", "financialdatasets")
    monkeypatch.setattr(get_settings(), "platform_key_financialdatasets", "")
    assert (await clients.post("/secrets", json={"name": "financialdatasets", "value": "k"})).status_code == 200
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        org = await db.get(Org, org_id)
    plan = await call_route.build_plan(cat.by_id["treg.stocks.quote.live"], {"symbol": "AAPL"},
                                       SimpleNamespace(org_id=org_id, org=org),
                                       call_route.RouteOptions.from_headers(lambda key: None))
    tiers = {c.endpoint["id"]: c.tier for c in plan.candidates}
    assert tiers.get("financialdatasets.prices.snapshot") == "anonymous", tiers


async def test_a_keyless_child_not_served_is_dropped_and_quoted_with_the_true_reason(
        clients: AsyncClient, monkeypatch):
    """A keyless row the anonymous tier does not take (provider not allow-listed) is dropped by the
    router and skipped by the quote as "not enabled" — never as a missing key nobody can connect."""
    import dataclasses
    from treg import oauth_providers
    from treg.models import Org
    from treg.routers.catalog import _quote_skip
    monkeypatch.setitem(oauth_providers.REGISTRY, "financialdatasets", dataclasses.replace(
        oauth_providers.REGISTRY["financialdatasets"], auth_kind="public"))
    cat = catalog_store.load()
    row = cat.by_id["financialdatasets.prices.snapshot"]
    monkeypatch.setitem(row, "platform_auth", "anonymous")
    monkeypatch.setitem(row, "cost", {"type": "free", "value": 0, "currency": "USD", "unit": "call"})
    monkeypatch.setattr(get_settings(), "platform_providers", "")
    monkeypatch.setattr(get_settings(), "platform_key_financialdatasets", "")
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        org = await db.get(Org, org_id)
    plan = await call_route.build_plan(cat.by_id["treg.stocks.quote.live"], {"symbol": "AAPL"},
                                       SimpleNamespace(org_id=org_id, org=org),
                                       call_route.RouteOptions.from_headers(lambda key: None))
    why = {d["endpoint_id"]: d["why"] for d in plan.dropped}.get("financialdatasets.prices.snapshot", "")
    assert "not enabled" in why and "key on this deployment" not in why, plan.dropped
    skip = _quote_skip(row, cat) or ""
    assert "not enabled" in skip and "key on this deployment" not in skip, skip


async def test_a_keyless_child_answers_a_routed_call_with_no_credential_hold_or_charge(
        clients: AsyncClient, monkeypatch):
    """A public provider's row is a routed child on the anonymous tier: no credential is sent, nothing
    is held or charged, and the child's own answer comes back unchanged."""
    import dataclasses
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from treg import oauth_providers
    from treg.api import app
    monkeypatch.setitem(oauth_providers.REGISTRY, "financialdatasets", dataclasses.replace(
        oauth_providers.REGISTRY["financialdatasets"], auth_kind="public", base_url="http://upstream"))
    cat = catalog_store.load()
    row = cat.by_id["financialdatasets.prices.snapshot"]
    monkeypatch.setitem(row, "platform_auth", "anonymous")
    monkeypatch.setitem(row, "cost", {"type": "free", "value": 0, "currency": "USD", "unit": "call"})
    monkeypatch.setattr(get_settings(), "platform_providers", "financialdatasets")
    monkeypatch.setattr(get_settings(), "platform_key_financialdatasets", "")
    seen = []
    answer = {"snapshot": {"ticker": "AAPL", "price": 190.5}}

    async def snapshot(request):
        seen.append((request.url.path, request.query_params.get("ticker"),
                     request.headers.get("authorization"), request.headers.get("x-api-key")))
        return JSONResponse(answer)

    before = await _balance(clients)
    upstream_app = Starlette(routes=[Route("/prices/snapshot", snapshot)])
    async with AsyncClient(transport=httpx.ASGITransport(app=upstream_app), base_url="http://upstream") as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        r = await clients.post("/call/treg.stocks.quote.live", json={"symbol": "aapl"},
                               headers={"X-Treg-Route-Prefer": "financialdatasets"})
    assert r.status_code == 200, r.text
    doc = r.json()["_treg"]
    assert doc["served_by"] == "financialdatasets.prices.snapshot" and doc["tier"] == "anonymous", doc
    assert doc["charged_micro"] == 0 and await _balance(clients) == before
    assert seen == [("/prices/snapshot", "AAPL", None, None)], seen
    assert r.json()["raw"] == answer
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []


async def test_a_key_providers_keyless_row_is_never_called_a_keyless_public_api(
        clients: AsyncClient, monkeypatch):
    """Only a public provider has nothing to connect. A key provider's keyless row not served here
    is refused for its key, which a team's own key would satisfy — never as a keyless public API."""
    from treg.models import Org
    from treg.routers.catalog import _quote_skip
    cat = catalog_store.load()
    row = cat.by_id["financialdatasets.prices.snapshot"]
    monkeypatch.setitem(row, "platform_auth", "anonymous")
    monkeypatch.setitem(row, "cost", {"type": "free", "value": 0, "currency": "USD", "unit": "call"})
    monkeypatch.setattr(get_settings(), "platform_providers", "")
    monkeypatch.setattr(get_settings(), "platform_key_financialdatasets", "")
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        org = await db.get(Org, org_id)
    plan = await call_route.build_plan(cat.by_id["treg.stocks.quote.live"], {"symbol": "AAPL"},
                                       SimpleNamespace(org_id=org_id, org=org),
                                       call_route.RouteOptions.from_headers(lambda key: None))
    why = {d["endpoint_id"]: d["why"] for d in plan.dropped}.get("financialdatasets.prices.snapshot", "")
    assert why and "keyless public API" not in why, plan.dropped
    skip = _quote_skip(row, cat) or ""
    assert skip and "keyless public API" not in skip, skip


async def test_a_route_that_fails_only_on_treg_s_read_cap_is_logged_as_treg_s_limit(
        clients: AsyncClient, enrichment_on, monkeypatch, call_error_lines):
    """When every candidate's answer is cut by treg's routed read cap, no provider failed: the call is
    logged as treg's buffer limit (as on a direct call), never as an upstream error."""
    monkeypatch.setattr(call_route, "ROUTED_BODY_MAX", 512)
    big = {"data": {"email": "patrick@stripe.com", "first_name": "x" * 600}}
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"*": [(200, big)] * 30}, []))
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"})
    assert r.status_code == 502 and r.json()["detail"]["error"] == "route_failed", r.text
    assert all("routed read" in t["detail"] for t in r.json()["detail"]["tried"])
    line = call_error_lines()[-1]
    assert line["error_class"] == "treg_error" and line["by"] == "treg", line


async def test_a_route_cut_by_treg_s_read_cap_and_the_caller_s_skips_is_still_treg_s_limit(
        clients: AsyncClient, enrichment_on, monkeypatch, call_error_lines):
    """Candidates the caller's own max cost skipped were never asked: when the only attempts made were
    cut by treg's routed read cap, the failure is still treg's limit, not an upstream error."""
    monkeypatch.setattr(call_route, "ROUTED_BODY_MAX", 512)
    assert (await clients.post("/secrets", json={"name": "tomba", "value": "OWN-TOMBA"})).status_code == 200
    big = {"data": {"email": "patrick@stripe.com", "first_name": "x" * 600}}
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"*": [(200, big)] * 30}, []))
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"},
                           headers={"X-Treg-Route-Prefer": "tomba", "X-Treg-Route-Max-Cost": "0.000001"})
    assert r.status_code == 502, r.text
    outcomes = {t["outcome"] for t in r.json()["detail"]["tried"]}
    assert "skipped" in outcomes and outcomes <= {"skipped", "error"}, r.json()["detail"]["tried"]
    line = call_error_lines()[-1]
    assert line["error_class"] == "treg_error" and line["by"] == "treg", line


async def test_a_team_tool_on_a_row_s_own_host_is_planned_as_the_team_s_tool(clients: AsyncClient, monkeypatch):
    """A row on an approved second host is the team's own tool when the team registered a tool for
    THAT host (resolve calls it there): the router must see it, as a direct call does."""
    from treg.models import Org
    cat = catalog_store.load()
    sid = (await clients.post("/secrets", json={"name": "gate-key", "value": "v"})).json()["id"]
    r = await clients.post("/tools", json={"name": "our-gate", "base_url": "https://gate.crawl4ai.com",
                                           "secret_id": sid})
    assert r.status_code == 200, r.text
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        org = await db.get(Org, org_id)
    plan = await call_route.build_plan(cat.by_id["treg.people.email.find"],
                                       {"full_name": "Patrick Collison", "domain": "stripe.com"},
                                       SimpleNamespace(org_id=org_id, org=org),
                                       call_route.RouteOptions.from_headers(lambda key: None))
    tiers = {c.endpoint["id"]: c.tier for c in plan.candidates}
    assert tiers.get("crawl4ai.people.email.find") == "tool", (tiers, plan.dropped)


async def test_a_main_host_tool_does_not_hide_the_team_s_own_key_on_a_second_host_row(clients: AsyncClient, monkeypatch):
    """A tool for a provider's main host covers only that host: a row on its approved second host must
    still be served on the team's own key (own key always wins), never fall to treg's key or be dropped."""
    from treg.models import Org
    cat = catalog_store.load()
    sid = (await clients.post("/secrets", json={"name": "crawl4ai", "value": "OWN"})).json()["id"]
    r = await clients.post("/tools", json={"name": "our-main", "base_url": "https://api.crawl4ai.com",
                                           "secret_id": sid})
    assert r.status_code == 200, r.text
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        org = await db.get(Org, org_id)
    plan = await call_route.build_plan(cat.by_id["treg.people.email.find"],
                                       {"full_name": "Patrick Collison", "domain": "stripe.com"},
                                       SimpleNamespace(org_id=org_id, org=org),
                                       call_route.RouteOptions.from_headers(lambda key: None))
    tiers = {c.endpoint["id"]: c.tier for c in plan.candidates}
    assert tiers.get("crawl4ai.people.email.find") == "credential", (tiers, plan.dropped)


async def test_a_demo_team_s_routed_keyless_refusal_is_treg_policy_not_capacity(
        clients: AsyncClient, monkeypatch, call_error_lines):
    import dataclasses
    from treg import oauth_providers
    from treg.models import Org
    monkeypatch.setitem(oauth_providers.REGISTRY, "financialdatasets", dataclasses.replace(
        oauth_providers.REGISTRY["financialdatasets"], auth_kind="public"))
    cat = catalog_store.load()
    route_ep = cat.by_id["treg.stocks.quote.live"]
    for e in cat.endpoints:  # only the keyless child answers this capability
        if e.get("capability") == route_ep["capability"] and e["id"] not in (
                route_ep["id"], "financialdatasets.prices.snapshot"):
            monkeypatch.setitem(e, "capability", "test.elsewhere")
    row = cat.by_id["financialdatasets.prices.snapshot"]
    monkeypatch.setitem(row, "platform_auth", "anonymous")
    monkeypatch.setitem(row, "cost", {"type": "free", "value": 0, "currency": "USD", "unit": "call"})
    monkeypatch.setattr(get_settings(), "platform_providers", "financialdatasets")
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        org.public_demo = True
        await db.commit()
    r = await clients.post("/call/treg.stocks.quote.live", json={"symbol": "AAPL"})
    assert r.status_code == 422 and "demo" in r.text, (r.status_code, r.json()["detail"]["dropped"])
    line = call_error_lines()[-1]
    assert line["kind"] == "admission_denied" and line["error_class"] == "treg_policy", line


async def test_a_strict_drop_beside_a_demo_policy_drop_keeps_its_own_message(monkeypatch):
    """Demo policy speaks for the refusal only when it took every candidate."""
    plan = SimpleNamespace(candidates=[], variant=("symbol",), route_rule=None, health=None, dropped=[
        {"endpoint_id": "a.keyless", "why": "a is a keyless public API, but demo and sandbox teams do not call it",
         "policy": True},
        {"endpoint_id": "b.paid", "why": "ignores country", "strict": True}])

    ep = catalog_store.load().by_id["treg.stocks.quote.live"]
    options = call_route.RouteOptions.from_headers(lambda key: None)
    mixed = call_route._no_candidate_refusal(ep, plan, options)
    assert mixed.log_kind is None and "Strict-Filters" in mixed.detail["message"], mixed.detail
    plan.dropped = plan.dropped[:1]
    alone = call_route._no_candidate_refusal(ep, plan, options)
    assert alone.log_kind == "admission_denied" and alone.status_code == 422, alone.detail
    # the caller's own exclude may have taken a provider open to the team: not demo policy alone
    excluded = call_route._no_candidate_refusal(
        ep, plan, call_route.RouteOptions.from_headers(lambda key: "b" if key.lower() == "x-treg-route-exclude" else None))
    assert excluded.log_kind is None, excluded.detail


async def test_a_host_the_provider_does_not_approve_does_not_move_the_router_s_tool_check(clients: AsyncClient, monkeypatch):
    """resolve keeps a row on its provider's main host unless the provider approves the row's host, so
    a main-host tool serves it and the router must plan it as that tool."""
    from treg.models import Org
    cat = catalog_store.load()
    monkeypatch.setitem(cat.by_id["financialdatasets.prices.snapshot"], "host", "other.financialdatasets.example")
    sid = (await clients.post("/secrets", json={"name": "fd-key", "value": "v"})).json()["id"]
    r = await clients.post("/tools", json={"name": "our-fd", "base_url": "https://api.financialdatasets.ai",
                                           "secret_id": sid})
    assert r.status_code == 200, r.text
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        org = await db.get(Org, org_id)
    plan = await call_route.build_plan(cat.by_id["treg.stocks.quote.live"], {"symbol": "AAPL"},
                                       SimpleNamespace(org_id=org_id, org=org),
                                       call_route.RouteOptions.from_headers(lambda key: None))
    tiers = {c.endpoint["id"]: c.tier for c in plan.candidates}
    assert tiers.get("financialdatasets.prices.snapshot") == "tool", (tiers, plan.dropped)
