"""Agent verdicts on catalog endpoints: one vote per team, a threshold, quotes in proportion."""
from datetime import datetime, timedelta

from treg.application import feedback
from treg.domain.feedback.verdicts import Review, quotable, summarize
from treg.infra.db import session_maker
from treg.models import CallReview

T0 = datetime(2026, 9, 1)
LONG = "Returned the requested records with the fields the task needed, nothing missing."


def review(org, usefulness="useful", reason=LONG, endpoint="a.b.c", day=0, person_p=0.01):
    return Review(endpoint, org, usefulness, reason, "codex", T0 + timedelta(days=day), person_p)


def test_endpoint_below_threshold_publishes_early_quotes_and_no_score():
    early = summarize([review(org, day=org) for org in range(4)])["a.b.c"]
    assert early["teams"] == 0 and "share" not in early
    assert len(early["samples"]) == 3
    assert summarize([review(org) for org in range(5)])["a.b.c"]["teams"] == 5


def test_early_reviews_without_a_quotable_reason_publish_nothing():
    assert summarize([review(org, reason="Short.") for org in range(4)]) == {}
    assert summarize([review(org, reason=None) for org in range(4)]) == {}


def test_early_quotes_are_the_newest_one_per_team():
    rows = ([review(1, "not_useful", day=d, reason=f"{LONG} Run {d}.") for d in range(3)]
            + [review(2, "partly", day=9), review(3, reason="Short.", day=20)])
    samples = summarize(rows)["a.b.c"]["samples"]
    assert [(q["usefulness"], q["reason"]) for q in samples] == [("partly", LONG), ("not_useful", f"{LONG} Run 2.")]


def test_the_team_count_is_published_as_its_band():
    for n, band in ((5, 5), (9, 5), (10, 10), (24, 10), (25, 25), (60, 50)):
        assert summarize([review(org) for org in range(n)])["a.b.c"]["teams"] == band


def test_a_team_is_one_vote_however_many_times_its_agent_rated():
    flood = [review(1, "not_useful", day=d) for d in range(40)]
    out = summarize(flood + [review(org) for org in range(2, 6)])["a.b.c"]
    assert out["teams"] == 5
    assert out["share"] == {"useful": 0.8, "partly": 0.0, "not_useful": 0.2}
    # A team's mixed verdicts split its single vote.
    split = [review(1, "useful"), review(1, "not_useful")] + [review(org) for org in range(2, 6)]
    assert summarize(split)["a.b.c"]["share"] == {"useful": 0.9, "partly": 0.0, "not_useful": 0.1}


def test_quotes_follow_the_vote_newest_first():
    rows = ([review(org, "not_useful", day=org) for org in range(6)]
            + [review(org, "useful", day=org) for org in range(6, 9)]
            + [review(10, "useful", reason="Short.")])
    out = summarize(rows)["a.b.c"]
    kinds = [q["usefulness"] for q in out["samples"]]
    assert sorted(kinds) == ["not_useful", "not_useful", "useful"]
    assert [q["month"] for q in out["samples"]] == sorted((q["month"] for q in out["samples"]), reverse=True)
    assert out["samples"][0] == {"usefulness": "useful", "reason": LONG, "client": "codex", "month": "2026-09"}


def test_one_quote_per_team_its_latest():
    rows = [review(1, day=d, reason=f"{LONG} Run {d}.") for d in range(5)] + [review(org, reason=None) for org in range(2, 6)]
    samples = summarize(rows)["a.b.c"]["samples"]
    assert [q["reason"] for q in samples] == [f"{LONG} Run 4."]


def test_quotable():
    assert quotable(LONG)
    assert quotable(LONG + " Ran on 2026-11-22 for $0.0025, 120 rows.")  # dates and prices are fine
    assert quotable(LONG + " Ran at 2026-11-22 14:00 UTC.")
    assert quotable(LONG + " Covered 2026-11-01 - 2026-11-22.")
    assert quotable(LONG + " Ran at 2026-11-22T14:00:05 and 09:30.")
    assert quotable(LONG + " Covered 01.11.2026 \u2013 22.11.2026.")
    assert quotable(LONG + " Covered 11/01/2026 - 11/22/2026 at 14:00.")
    assert quotable(LONG + " Ran at 2026-11-22T14:00:05.123Z.")
    assert quotable(LONG + " Ran at 11/22/2026 14:00:05.")
    # Year-last dates need a real month in one of the first two places: 19.19.2026 is not a date,
    # so its run is counted whole.
    assert quotable(LONG + " Ran at 31.12.2026 14:00 and 12/31/2026 14:00.")
    assert not quotable(LONG + " Ran at 19.19.2026 14:00.")
    assert not quotable("   too short   ")
    assert not quotable(None)


def test_a_reason_carrying_contact_data_is_never_quoted():
    for leaked in (" Found jane@acme.io at https://acme.io, as asked.",
                   " Confirmed 732-433-0667 as a live mobile line.",
                   " Confirmed +44 20 7946 0958 as the direct line.",
                   " Confirmed 732\u2011433\u20110667 as a live mobile line.",
                   " Confirmed 732\u2013433\u20130667 as a live mobile line.",
                   " Confirmed (732)\u00a0433\u22120667 as a live mobile line.",
                   " Confirmed 0612.34.56.78 as a live mobile line.",
                   " Confirmed 2012-03-04-56 as a live mobile line.",
                   " Confirmed 2026-11-22 732 433 0667 as a live mobile line.",
                   " Confirmed 11/22/2026 732/433/0667 as a live mobile line.",
                   " Matched https://www.LinkedIn.com/in/someone as the person."):
        assert not quotable(LONG + leaked), leaked
    rows = [review(org, reason=LONG + " Reach them at 732-433-0667.", day=org) for org in range(4)]
    assert summarize(rows) == {}
    # A general web search whose reason links a Sales Navigator lead is not quoted, whatever Jev said.
    lead = LONG + " Top hit was linkedin.com/sales/lead/ACwAAA for the founder."
    assert summarize([review(org, reason=lead, endpoint="exa.search") for org in range(4)]) == {}


def test_a_reason_naming_a_person_or_not_yet_judged_is_never_quoted():
    for person_p in (None, 0.1, 0.97):
        rows = [review(org, person_p=person_p) for org in range(5)]
        out = summarize(rows)["a.b.c"]
        assert out["share"]["useful"] == 1.0 and out["samples"] == [], person_p
        assert summarize(rows[:4]) == {}, person_p
    assert len(summarize([review(org, person_p=0.09) for org in range(4)])["a.b.c"]["samples"]) == 3


async def test_platform_payload_carries_verdicts_past_the_threshold(clients):
    body = (await clients.get("/catalog/platforms/tiktok")).json()
    # Two providers of one capability, so the detail's siblings carry the other's reviews too.
    target, other = [e["id"] for e in next(c for c in body["capabilities"] if len(c["endpoints"]) >= 2)["endpoints"]][:2]
    assert all("reviews" not in e for cap in body["capabilities"] for e in cap["endpoints"])

    orgs = [(await clients.post("/orgs", json={"name": f"Verdicts {i}"})).json()["org_id"] for i in range(5)]
    async with session_maker() as db:
        for i, org in enumerate(orgs):
            db.add(CallReview(org_id=org, user_email="agent@example.dev", call_id=f"v-{i}",
                              endpoint_id=target, client="claude-code",
                              usefulness="useful" if i else "not_useful", reason=LONG, person_p=0.0))
        for i, org in enumerate(orgs[:4]):
            db.add(CallReview(org_id=org, user_email="agent@example.dev", call_id=f"w-{i}",
                              endpoint_id=other, usefulness="useful", reason=LONG, person_p=0.0))
        db.add(CallReview(org_id=orgs[4], user_email="agent@example.dev", call_id="w-4",
                          endpoint_id=other, usefulness="not_sure", reason=LONG, person_p=0.0))
        await db.commit()
    feedback.forget_endpoint_verdicts()

    body = (await clients.get("/catalog/platforms/tiktok")).json()
    views = {e["id"]: e for cap in body["capabilities"] for e in cap["endpoints"]}
    assert views[target]["reviews"]["teams"] == 5
    assert views[target]["reviews"]["share"]["useful"] == 0.8
    assert {q["client"] for q in views[target]["reviews"]["samples"]} == {"claude-code"}
    # Four verdicts and a `not_sure` are not five teams: quoted as early, with no share.
    assert views[other]["reviews"]["teams"] == 0 and "share" not in views[other]["reviews"]
    assert len(views[other]["reviews"]["samples"]) == 3
    ledger = [e for d in body["domains"] for r in d["rows"] for e in r["endpoints"]]
    assert next(e for e in ledger if e["id"] == target)["reviews"]["teams"] == 5

    # The endpoint's own detail (what `catalog_get` relays) carries the same object, siblings too.
    detail = (await clients.get(f"/catalog/endpoints/{target}")).json()
    assert detail["endpoint"]["reviews"]["teams"] == 5
    assert next(s for s in detail["siblings"] if s["id"] == other)["reviews"]["teams"] == 0


async def test_a_failed_fold_never_takes_the_catalog_down(clients, monkeypatch):
    async def broken():
        raise RuntimeError("replica unavailable")
    monkeypatch.setattr(feedback, "endpoint_verdicts", broken)
    assert (await clients.get("/catalog/platforms/tiktok")).status_code == 200


async def _review(org_ids, endpoint_id, person_p, reason=LONG):
    async with session_maker() as db:
        for org in org_ids:
            db.add(CallReview(org_id=org, user_email="agent@example.dev", call_id=f"{endpoint_id}-{org}",
                              endpoint_id=endpoint_id, client="claude-code", usefulness="useful",
                              reason=reason, person_p=person_p))
        await db.commit()
    feedback.forget_endpoint_verdicts()


async def test_public_detail_quotes_only_reasons_judged_to_name_no_one(clients):
    """Through the public detail `catalog_get` relays: a people endpoint's clean reasons are quoted
    like any other's; a reason judged to name someone, or not judged yet, is never quoted (one team
    then publishes nothing, five teams publish the score alone)."""
    orgs = [(await clients.post("/orgs", json={"name": f"People {i}"})).json()["org_id"] for i in range(5)]
    cases = {"apollo.people.search": 0.02, "aviato.companies.founders": 0.9, "anyapi.facebook.followers": None}
    for endpoint_id, person_p in cases.items():
        await _review(orgs[:1], endpoint_id, person_p)
    for endpoint_id, person_p in cases.items():
        endpoint = (await clients.get(f"/catalog/endpoints/{endpoint_id}")).json()["endpoint"]
        assert ("reviews" in endpoint) == (person_p == 0.02), endpoint_id

    for endpoint_id, person_p in cases.items():
        await _review(orgs[1:], endpoint_id, person_p)
    for endpoint_id, person_p in cases.items():
        reviews = (await clients.get(f"/catalog/endpoints/{endpoint_id}")).json()["endpoint"]["reviews"]
        assert reviews["teams"] == 5 and reviews["share"]["useful"] == 1.0, endpoint_id
        assert bool(reviews["samples"]) == (person_p == 0.02), endpoint_id
