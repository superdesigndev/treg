"""A routed call's audit row names the child whose answer its caller received.

`CallRecord.served_call_ref` on the parent row is that child's `call_ref`, so a later reader (the
review body pilot, a judge) reads the child row, and through it the archived answer, by exact id:
no scan of the team's history, no guess from attempt order. Review credit is a separate decision
and is left alone: with `X-Treg-Route-Confirm` the finder's answer is served while the review is
credited to the confirmer."""

from __future__ import annotations

import json

from sqlmodel import select

from treg import archive, audit
from treg.application.call import route as call_route
from treg.application.call import service as call_service
from treg.config import get_settings
from treg.infra.db import session_maker
from treg.models import CallRecord, CallReview

from test_marketplace_call import platform_on  # noqa: F401
from test_route_confirm import ON as CONFIRM, _hunter
from test_route_quality_upgrade import EMAIL, ENRICH, HARVEST_FIRST, _named, _Reader, enrich_on  # noqa: F401
from test_route_verify import ASK, FIND, TOMBA_HIT, _relay, verifiers_on  # noqa: F401
from test_dropcontact_routing import dropcontact_platform  # noqa: F401
from test_routing import enrichment_on, people_search_on  # noqa: F401

TOMBA_MISS = (200, {"data": {"email": None, "score": None, "verification": {"status": None}}})


async def _row(call_ref: str) -> CallRecord | None:
    await archive.drain()
    await audit.drain()
    async with session_maker() as db:
        return (await db.execute(select(CallRecord).where(CallRecord.call_ref == call_ref))).scalars().first()


async def _archived(row: CallRecord):
    """The answer the archive holds for this row, parsed: what a reader of the link gets."""
    found = await archive.resolve_result(row.archive_key_hash, row.archive_content_hash)
    return json.loads(found["response"]["body_text"])


async def _delivered(parent: CallRecord):
    """The documented reader: the answer this routed call delivered, or None (archive unavailable).

    The ref says where to look: the child row's own archived answer, or for an async provider the
    terminal answer filed under that ref. Either counts only when its `content_hash` (sha256 of the
    bytes) equals the parent's `served_content_hash`."""
    import hashlib
    if not parent.served_call_ref:
        return None
    child = await _row(parent.served_call_ref)
    if child is None:
        return None
    if child.archive_key_hash and child.archive_content_hash == parent.served_content_hash:
        return await _archived(child)
    terminal = (await archive.load_terminal_responses([(child.call_ref, child.endpoint_id)])).get(child.call_ref)
    if terminal is not None and hashlib.sha256(terminal).hexdigest() == parent.served_content_hash:
        return json.loads(terminal)
    return None


async def _served(r) -> tuple[CallRecord, CallRecord | None]:
    """The parent row, and the child row its link names (read by that exact ref)."""
    parent = await _row(r.headers["X-Treg-Call-Id"])
    assert parent is not None and parent.credential_tier == "routed"
    return parent, (await _row(parent.served_call_ref) if parent.served_call_ref else None)


async def test_a_waterfall_hit_links_its_last_attempt(clients, verifiers_on, monkeypatch):
    monkeypatch.setattr(get_settings(), "archive_mode", "shadow")   # recording on
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [TOMBA_MISS], "hunter": [_hunter("patrick@stripe.com")]}, []))
    r = await clients.post(f"/call/{FIND}", json=ASK)
    assert r.status_code == 200, r.text
    assert [t["outcome"] for t in r.json()["_treg"]["tried"]] == ["miss", "hit"]
    parent, child = await _served(r)
    assert parent.served_call_ref == f"{parent.call_ref}:r1"
    assert child.endpoint_id == r.json()["_treg"]["served_by"] == "hunter.people.email.find"
    assert await _delivered(parent) == r.json()["raw"], "the link leads to the delivered answer"
    assert parent.served_content_hash == child.archive_content_hash


async def test_a_miss_everywhere_links_no_child(clients, verifiers_on, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [TOMBA_MISS], "hunter": [_hunter(None)]}, []))
    r = await clients.post(f"/call/{FIND}", json=ASK)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["_treg"]["served_by"] is None and d["raw"] is None
    parent, child = await _served(r)
    assert parent.served_call_ref is None and parent.served_content_hash is None and child is None


async def test_a_route_failure_links_no_child(clients, verifiers_on, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [(503, {})], "hunter": [(503, {})]}, []))
    r = await clients.post(f"/call/{FIND}", json=ASK)
    assert r.status_code == 502, r.text
    parent = await _row(r.headers["X-Treg-Call-Id"])
    assert parent.status_code == 502 and parent.served_call_ref is None


async def test_an_upgrade_links_the_upgraded_vendors_answer(clients, enrich_on, monkeypatch):
    from test_routing import _relay_by_provider

    monkeypatch.setattr(call_route, "_route_quality_reader", _Reader(HARVEST_FIRST))
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({
        "blitz": [(200, _named("blitzapi.people.email.reverse", "Alex", "Example"))],
        "harvest": [(200, _named("harvestapi.linkedin.user.profile", "alex", "EXAMPLE"))]}, []))
    r = await clients.post(ENRICH, json=EMAIL, headers={"X-Treg-Route-Upgrade": "linkedin_url"})
    assert r.status_code == 200, r.text
    assert r.json()["_treg"]["upgrade"]["upgraded"] is True
    parent, child = await _served(r)
    assert parent.served_call_ref.startswith(f"{parent.call_ref}:u:r"), "the leaf the upgrade served, not `:u`"
    assert child.endpoint_id == r.json()["_treg"]["served_by"] == "harvestapi.linkedin.user.profile"
    upgrade = await _row(f"{parent.call_ref}:u")
    assert upgrade.served_call_ref == parent.served_call_ref, "the nested routed row links the same child"


async def test_an_upgrade_about_another_person_links_the_first_answer(clients, enrich_on, monkeypatch):
    from test_routing import _relay_by_provider

    monkeypatch.setattr(call_route, "_route_quality_reader", _Reader(HARVEST_FIRST))
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({
        "blitz": [(200, _named("blitzapi.people.email.reverse", "Alex", "Example"))],
        "harvest": [(200, _named("harvestapi.linkedin.user.profile", "Sam", "Other"))]}, []))
    r = await clients.post(ENRICH, json=EMAIL, headers={"X-Treg-Route-Upgrade": "linkedin_url"})
    assert r.status_code == 200, r.text
    assert r.json()["_treg"]["upgrade"]["upgraded"] is False
    parent, child = await _served(r)
    assert parent.served_call_ref == f"{parent.call_ref}:r0"
    assert child.endpoint_id == "blitzapi.people.email.reverse"


async def test_a_confirmed_find_links_the_finder_and_credits_the_confirmer(clients, verifiers_on, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [TOMBA_HIT], "hunter": [_hunter("Patrick@Stripe.COM")]}, []))
    r = await clients.post(f"/call/{FIND}", json=ASK, headers=CONFIRM)
    assert r.status_code == 200, r.text
    assert r.json()["_treg"]["confirmation"]["confirmed"] is True
    parent, child = await _served(r)
    assert parent.served_call_ref == f"{parent.call_ref}:r0", "the finder's answer is the one returned"
    assert child.endpoint_id == r.json()["_treg"]["served_by"] == "tomba.people.email.find"
    review = await clients.post("/reviews", json={"call_id": parent.call_ref, "usefulness": "useful"})
    assert review.status_code == 201, review.text
    async with session_maker() as db:
        row, = (await db.execute(select(CallReview))).scalars().all()
    assert row.endpoint_id == "hunter.people.email.find", "review credit is unchanged: the confirmer"


async def test_a_lost_earlier_attempt_row_does_not_move_the_link(clients, verifiers_on, monkeypatch):
    """The link is written from what the router knew, never inferred from the rows that landed."""
    record = audit.record_call

    def shed_first_attempt(**kwargs):
        if (kwargs.get("telemetry") or {}).get("call_ref", "").endswith(":r0"):
            return  # the audit writer may shed a row under load
        record(**kwargs)
    monkeypatch.setattr(audit, "record_call", shed_first_attempt)
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [TOMBA_MISS], "hunter": [_hunter("patrick@stripe.com")]}, []))
    r = await clients.post(f"/call/{FIND}", json=ASK)
    assert r.status_code == 200, r.text
    parent, child = await _served(r)
    assert await _row(f"{parent.call_ref}:r0") is None
    assert parent.served_call_ref == f"{parent.call_ref}:r1" and child.endpoint_id == "hunter.people.email.find"


async def test_a_lost_served_row_still_names_the_child(clients, verifiers_on, monkeypatch):
    """When the served child's own row is the one shed, the parent still names it: the reader
    knows the answer is missing rather than taking another attempt's for it."""
    record = audit.record_call

    def shed_served(**kwargs):
        if (kwargs.get("telemetry") or {}).get("call_ref", "").endswith(":r1"):
            return
        record(**kwargs)
    monkeypatch.setattr(audit, "record_call", shed_served)
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [TOMBA_MISS], "hunter": [_hunter("patrick@stripe.com")]}, []))
    r = await clients.post(f"/call/{FIND}", json=ASK)
    assert r.status_code == 200, r.text
    parent, child = await _served(r)
    assert parent.served_call_ref == f"{parent.call_ref}:r1" and child is None


async def test_an_async_find_links_the_submission_whose_terminal_answer_is_raw(clients, dropcontact_platform,
                                                                                 monkeypatch):
    from test_dropcontact_routing import _relay as dropcontact_relay

    dropcontact_relay(monkeypatch, {'success': True, 'data': [{
        'first_name': 'Peter', 'last_name': 'Jackson',
        'email': [{'email': 'peter.jackson@company.com', 'qualification': 'nominative@pro'}]}]}, [])
    r = await clients.post("/call/treg.people.email.find",
                           json={"first_name": "Peter", "last_name": "Jackson", "domain": "company.com"},
                           headers={"X-Treg-Route-Prefer": "dropcontact", "X-Treg-Route-Waterfall": "0"})
    assert r.status_code == 200, r.text
    parent, child = await _served(r)
    assert parent.served_call_ref == f"{parent.call_ref}:r0", "the submission the task is filed under"
    assert child.endpoint_id == r.json()["_treg"]["served_by"] == "dropcontact.people.email.find"
    # The answer is the terminal snapshot archived under the submission's ref, not the row's hashes
    # (the task ticket) nor a poll's row (no archive link).
    assert await _delivered(parent) == r.json()["raw"], "read from the terminal answer filed under the ref"


async def test_a_merged_search_links_the_answer_whose_body_is_raw(clients, people_search_on, monkeypatch):
    """`served_by` names the first contributor (exa), but `raw` is the last answer (parallel's
    `entities`): the link follows `raw`, so reading it gives back exactly what the caller got."""
    from test_routing import _person, _relay_by_provider

    monkeypatch.setattr(get_settings(), "archive_mode", "shadow")
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"exa": [(200, {"results": [_person(0), _person(1)]})],
         "parallel": [(200, {"entities": [_person(1), _person(2), _person(3), _person(4)]})]}, []))
    r = await clients.post("/call/treg.people.search", json={"q": "head of tax in phoenix", "limit": 5},
                           headers={"X-Treg-Route-Prefer": "exa,parallel"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["_treg"]["merged_from"] == ["exa.people.search", "parallel.people.search"]
    assert d["_treg"]["served_by"] == "exa.people.search" and "entities" in d["raw"]
    parent, child = await _served(r)
    assert parent.served_call_ref == f"{parent.call_ref}:r1" and child.endpoint_id == "parallel.people.search"
    assert await _delivered(parent) == d["raw"]


async def _news_through_overflow(clients, monkeypatch, *, shed_overflow_row: bool = False):
    """Akta answers treg's account out of credits, then Monid serves the same request: two rows,
    one child ref."""
    import httpx

    from treg.application.call import overflow as call_overflow
    from treg.models import OverflowRoute

    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "akta,predictleads")
    monkeypatch.setenv("TREG_PLATFORM_KEY_AKTA", "AKTA-KEY")
    monkeypatch.setenv("TREG_PLATFORM_KEY_PREDICTLEADS", "PREDICTLEADS-KEY")
    monkeypatch.setenv("TREG_OVERFLOW_MODE", "on")
    monkeypatch.setenv("TREG_OVERFLOW_KEY_MONID", "MONID-KEY")
    monkeypatch.setenv("TREG_OVERFLOW_DAILY_BUDGET_USD", "10")
    get_settings.cache_clear()
    async with session_maker() as db:
        db.add(OverflowRoute(endpoint_id="akta.companies.news", aggregator="monid", provider="akta",
                             method="GET", path="/v1/news/", agg_slug="akta", agg_path="/v1/news",
                             agg_price_micro=10_000, agg_unit="call", ratio=1, enabled=True))
        await db.commit()
    call_route.overflow_routes_view.invalidate()

    async def overflow_send(client, req):
        return httpx.Response(200, json={
            "runId": "run-akta-news", "status": "COMPLETED",
            "output": {"data": [{"title": "served through Monid"}], "total": 1, "count": 1, "limit": 1, "offset": 0},
            "providerResponse": {"httpStatus": 200},
            "billing": {"reportedCost": {"value": 5_500, "unit": "MICRO_DOLLAR"}}})
    monkeypatch.setattr(call_overflow, "_send", overflow_send)
    from test_routing import _relay_by_provider
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"akta": [(402, {"error": "Insufficient credits"})]}, []))
    if shed_overflow_row:
        record = audit.record_call

        def shed(**kwargs):
            if kwargs.get("method") == "OVERFLOW":
                return
            record(**kwargs)
        monkeypatch.setattr(audit, "record_call", shed)
    r = await clients.post("/call/treg.companies.news", json={"domain": "canva.com", "limit": 1})
    assert r.status_code == 200, r.text
    assert r.json()["output"]["articles"] == [{"title": "served through Monid"}]
    return r


async def test_an_overflow_answer_is_not_linked(clients, platform_on, monkeypatch):
    r = await _news_through_overflow(clients, monkeypatch)
    parent = await _row(r.headers["X-Treg-Call-Id"])
    async with session_maker() as db:
        shared = (await db.execute(select(CallRecord.method, CallRecord.status_code)
                                   .where(CallRecord.call_ref == f"{parent.call_ref}:r0"))).all()
    assert sorted(shared) == [("GET", 402), ("OVERFLOW", 200)], "one ref, two rows: it names no one answer"
    assert parent.served_call_ref is None


async def test_an_overflow_answer_whose_row_was_shed_is_not_linked_to_the_direct_failure(clients, platform_on, monkeypatch):
    r = await _news_through_overflow(clients, monkeypatch, shed_overflow_row=True)
    parent = await _row(r.headers["X-Treg-Call-Id"])
    left = await _row(f"{parent.call_ref}:r0")
    assert left.status_code == 402, "only the direct attempt's row is left"
    assert parent.served_call_ref is None, "never read the direct failure as the answer"


async def test_a_raw_the_router_wrote_itself_is_not_linked(clients, enrichment_on, monkeypatch):
    """With the waterfall off, aviato's declared 404 miss comes back as a 200 with `raw: {}`: the
    caller never received the provider's `Not Found` body, so the link names no child."""
    from test_routing import _relay_by_provider

    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"aviato": [(404, {"message": "Not Found"})]}, []))
    r = await clients.post("/call/treg.people.phone.find", json={"linkedin_url": "https://www.linkedin.com/in/nobody-here"},
                           headers={"X-Treg-Route-Prefer": "aviato", "X-Treg-Route-Waterfall": "0"})
    assert r.status_code == 200 and r.json()["_treg"]["outcome"] == "miss", r.text
    assert r.json()["raw"] == {} and r.json()["_treg"]["served_by"] == "aviato.people.phone.find"
    parent = await _row(r.headers["X-Treg-Call-Id"])
    assert (await _row(f"{parent.call_ref}:r0")).status_code == 404
    assert parent.served_call_ref is None


async def test_a_providers_own_miss_body_handed_over_stays_linked(clients, verifiers_on, monkeypatch):
    """The control: a 200 miss whose body is the `raw` the caller gets keeps its link."""
    monkeypatch.setattr(get_settings(), "archive_mode", "shadow")
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [TOMBA_MISS]}, []))
    r = await clients.post(f"/call/{FIND}", json=ASK, headers={"X-Treg-Route-Waterfall": "0"})
    assert r.status_code == 200 and r.json()["_treg"]["outcome"] == "miss", r.text
    assert r.json()["raw"] == TOMBA_MISS[1]
    parent, child = await _served(r)
    assert parent.served_call_ref == f"{parent.call_ref}:r0" and child.status_code == 200
    assert await _delivered(parent) == r.json()["raw"]


async def test_a_pending_call_links_no_child_even_after_its_task_finishes(clients, dropcontact_platform, monkeypatch):
    """A 202 hands the caller the submission ticket. The answer filed under the submission's ref
    once the task finishes was never delivered by this call, so the parent links nothing."""
    from test_dropcontact_routing import _relay as dropcontact_relay

    dropcontact_relay(monkeypatch, {"success": True, "data": [{"email": [
        {"email": "person@company.com", "qualification": "nominatif@pro"}]}]}, [])
    monkeypatch.setattr(call_route, "ROUTED_ASYNC_WAIT_SECONDS", 0.01)
    r = await clients.post("/call/treg.people.email.find",
                           json={"first_name": "Peter", "last_name": "Jackson", "domain": "company.com"},
                           headers={"X-Treg-Route-Prefer": "dropcontact", "X-Treg-Route-Waterfall": "0"})
    assert r.status_code == 202 and "request_id" in r.json()["raw"], r.text
    task = r.json()["_treg"]["async"]["task_id"]
    done = await clients.get("/call/dropcontact.jobs.get", params={"request_id": task})
    assert done.status_code == 200 and "data" in done.json(), done.text
    parent = await _row(r.headers["X-Treg-Call-Id"])
    assert parent.status_code == 202 and parent.served_call_ref is None


def _dropcontact_found(email: str) -> dict:
    return {"success": True, "data": [{"first_name": "Peter", "last_name": "Jackson",
                                       "email": [{"email": email, "qualification": "nominative@pro"}]}]}


async def test_an_async_answer_another_poller_archived_first_is_not_taken_for_the_delivered_one(
        clients, dropcontact_platform, monkeypatch):
    """While the router's poll is in flight the team polls the same task itself and gets terminal A,
    which settles the task and is archived under the submission's ref. The router's own poll then
    answers B, and B is what the routed caller receives. The ref leads to A; the hash says it is not
    the delivered answer, so the reader reports the archive unavailable rather than A."""
    import asyncio

    from treg.application.call.types import UpstreamResponse

    gets = []

    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        if request.method == "POST":
            doc = {"request_id": "dropcontact-job", "success": True, "credits_left": 3999}
        else:
            gets.append(upstream_url)
            if len(gets) == 1:   # the router's poll: someone else polls first and wins
                other = await asyncio.wait_for(clients.get("/call/dropcontact.jobs.get",
                                                           params={"request_id": "dropcontact-job"}), 10)
                assert other.status_code == 200, other.text
                doc = _dropcontact_found("b.answer@company.com")
            else:
                doc = _dropcontact_found("a.answer@company.com")
        payload = json.dumps(doc).encode()

        async def stream():
            yield payload

        async def close():
            return None
        return UpstreamResponse(200, ((b"content-type", b"application/json"),), stream(), close)
    monkeypatch.setattr(call_service, "relay", relay)
    r = await clients.post("/call/treg.people.email.find",
                           json={"first_name": "Peter", "last_name": "Jackson", "domain": "company.com"},
                           headers={"X-Treg-Route-Prefer": "dropcontact", "X-Treg-Route-Waterfall": "0"})
    assert r.status_code == 200, r.text
    assert r.json()["output"]["email"] == "b.answer@company.com" and len(gets) == 2
    parent = await _row(r.headers["X-Treg-Call-Id"])
    assert parent.served_call_ref == f"{parent.call_ref}:r0"
    terminal = await archive.load_terminal_responses([(parent.served_call_ref, "dropcontact.people.email.find")])
    assert "a.answer@company.com" in terminal[parent.served_call_ref].decode(), "the ref leads to A"
    assert await _delivered(parent) is None, "A is not what the caller got: archive unavailable"


async def _enrich_by_harvest(clients, monkeypatch, headers: dict | None = None):
    from test_routing import _example, _relay_by_provider

    monkeypatch.setattr(get_settings(), "archive_mode", "shadow")
    monkeypatch.setattr(get_settings(), "strip_reported_charge", True)
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"harvest": [(200, _example("harvestapi.linkedin.user.profile"))]}, []))
    r = await clients.post(ENRICH, json={"linkedin_url": "https://www.linkedin.com/in/alex-example"},
                           headers={"X-Treg-Route-Prefer": "harvestapi", **(headers or {})})
    assert r.status_code == 200, r.text
    assert r.json()["_treg"]["served_by"] == "harvestapi.linkedin.user.profile"
    return r, await _row(r.headers["X-Treg-Call-Id"])


async def test_a_body_changed_before_delivery_is_not_taken_for_the_archived_one(clients, enrich_on, monkeypatch):
    """With `strip_reported_charge` on, the caller's `raw` lacks the provider's `cost`, which the
    archive keeps verbatim: the hashes differ, so the archived answer is not the delivered one."""
    r, parent = await _enrich_by_harvest(clients, monkeypatch)
    assert "cost" not in json.dumps(r.json()["raw"])
    child = await _row(parent.served_call_ref)
    assert '"cost"' in json.dumps(await _archived(child)), "the archive keeps the provider's bytes"
    assert await _delivered(parent) is None


async def test_a_body_delivered_unchanged_reads_back_from_the_archive(clients, enrich_on, monkeypatch):
    """The control: the caller keeps the reported charge, so `raw` is the archived body itself."""
    r, parent = await _enrich_by_harvest(clients, monkeypatch, {"X-Treg-Keep-Reported-Charge": "true"})
    assert '"cost"' in json.dumps(r.json()["raw"])
    assert await _delivered(parent) == r.json()["raw"]


async def test_an_async_provider_finished_on_submission_reads_its_own_archive(clients, verifiers_on, monkeypatch):
    """BounceBan can answer on submission (`terminal_on_submission`): no task is filed, so there is
    no terminal snapshot, and the child row's own archived answer is the delivered one."""
    from test_route_verify import _bb

    monkeypatch.setattr(get_settings(), "archive_mode", "shadow")
    monkeypatch.setattr(call_service, "relay", _relay({"bounceban": [_bb("deliverable")]}, []))
    r = await clients.post("/call/treg.people.email.verify", json={"email": "patrick@stripe.com"},
                           headers={"X-Treg-Route-Prefer": "bounceban", "X-Treg-Route-Waterfall": "0"})
    assert r.status_code == 200, r.text
    assert r.json()["_treg"]["served_by"] == "bounceban.people.email.verify"
    parent = await _row(r.headers["X-Treg-Call-Id"])
    child = await _row(parent.served_call_ref)
    assert await archive.load_terminal_responses([(child.call_ref, child.endpoint_id)]) == {}
    assert await _delivered(parent) == r.json()["raw"]
