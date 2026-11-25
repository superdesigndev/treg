"""The body pilot: negative reviews coded again with the call's answer in view, compared with the
reason-only codes, writing nothing; and the shortened answer the judge reads."""
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from sqlmodel import select

import treg
from treg import archive, worker
from treg.application import review_body_pilot, review_judge
from treg.config import get_settings
from treg.domain.feedback import body_view, review_codes
from treg.infra.db import session_maker
from treg.infra.judge import Reply
from treg.models import CallRecord, CallReview
from test_marketplace_call import platform_on  # noqa: F401 - pytest fixture
from test_routing import enrichment_on  # noqa: F401 - pytest fixture
from test_route_verify import verifiers_on  # noqa: F401 - pytest fixture

V = review_codes.CODEBOOK_VERSION
CATALOG = Path(treg.__file__).parent / "catalog"


def test_shrink_keeps_structure_and_counts_cut_items():
    body = json.dumps({"results": [{"name": "x" * 500, "email": None} for _ in range(40)], "total": 40})
    out = body_view.shrink(body, 1000)
    assert len(out) <= 1000
    value = json.loads(out)
    assert value["total"] == 40 and value["results"][-1][body_view.MORE] > 0
    assert value["results"][0]["email"] is None and value["results"][0]["name"].endswith(body_view.CUT)


def test_shrink_strips_html_and_cuts_text():
    out = body_view.shrink("<html><script>var a=1</script><p>Access   denied</p></html>", 100)
    assert out == "Access denied"
    assert len(body_view.shrink("word " * 1000, 50)) == 50
    assert body_view.shrink(None, 50) == ""


REASON = "Half of the requested fields came back empty for these people."
PEOPLE = json.dumps({"people": [{"email": None}] * 30})


def test_shrink_never_merges_keys_that_share_a_prefix():
    # The repository's CrustData example at the default budget: two long keys with one prefix.
    example = json.loads((CATALOG / "examples/crustdata.people.search.json").read_text())
    out = body_view.shrink(json.dumps(example), 4000)
    assert len(out) <= 4000

    def keys(value):
        if isinstance(value, dict):
            yield from value
            for v in value.values():
                yield from keys(v)
        elif isinstance(value, list):
            for v in value:
                yield from keys(v)
    original = set(keys(example)) | set(body_view.MARKERS)
    assert set(keys(json.loads(out))) <= original
    pair = {"company_professional_network_industry": "Software", "company_professional_network_profile_url": "u" * 90}
    assert json.loads(body_view.shrink(json.dumps({"rows": [pair] * 20}), 600))["rows"][0].keys() == pair.keys()


MARK = re.compile(r'"(_treg_[a-z_]+)"')


@pytest.mark.parametrize("example,budget", [("dataforseo.google.serp.organic.json", 500),
                                            ("crustdata.people.search.json", 4000),
                                            ("crustdata.people.search.json", 300)])
def test_every_mark_a_cut_leaves_is_explained_to_the_judge(example, budget):
    text = (CATALOG / "examples" / example).read_text()
    out = body_view.shrink(text, budget)
    marks = set(MARK.findall(out))
    assert marks, "the example should need cutting at this budget"
    prompt = review_codes.body_code_question(0)["instructions"]
    assert marks <= set(body_view.MARKERS) and all(f'"{m}"' in prompt for m in marks)
    assert body_view.CUT not in out or body_view.CUT in prompt


def test_a_vendor_ellipsis_is_not_read_as_a_cut():
    preview = {"snippet": "Founded in 2010, the company builds…", "items": ["x" * 400] * 3}
    out = json.loads(body_view.shrink(json.dumps(preview), 300))
    assert out["snippet"] == preview["snippet"] and out["items"][0].endswith(body_view.CUT)
    assert "'[treg:cut]'" in review_codes.body_code_question(0)["instructions"]


@pytest.mark.parametrize("budget", [22, 60, 500, 4000])
def test_a_lone_surrogate_still_encodes_as_utf8(budget):
    from test_jsontext import UPSTREAM
    text = json.dumps({"posts": [json.loads(UPSTREAM)] * 40 + [{"text": "中文"}]})
    out = body_view.shrink(text, budget)
    assert len(out) <= budget
    out.encode("utf-8")
    json.loads(out)


@pytest.mark.parametrize("budget", [0, -5, body_view.MIN_BUDGET - 1])
def test_shrink_refuses_a_budget_below_its_last_resort(budget):
    with pytest.raises(ValueError):
        body_view.shrink("word " * 2000, budget)


@pytest.mark.parametrize("text", ['{"results":[1]}', json.dumps({"人": ["数据" * 40] * 30}, ensure_ascii=False),
                                  "中文正文" * 500, json.dumps({"a": {"b": {"c": [{"d": "e" * 80}] * 9}}})])
def test_shrink_always_fits_the_budget(text):
    for budget in range(body_view.MIN_BUDGET, 160):
        out = body_view.shrink(text, budget)
        assert len(out) <= budget, (budget, out)
        if text.startswith("{"):
            json.loads(out)


@pytest.mark.parametrize("flag,value", [("--budget", "0"), ("--budget", "-5"), ("--budget", "1"),
                                        ("--sample", "0"), ("--sample", "-5"), ("--batch", "0")])
def test_cli_refuses_numbers_below_their_floor(flag, value, capsys):
    with pytest.raises(SystemExit):
        worker.main(["reviews", "body-pilot", flag, value])
    assert "must be >= " in capsys.readouterr().err


async def _org(clients) -> int:
    return (await clients.post("/orgs", json={"name": "Pilot"})).json()["org_id"]


def _review(org, ref, code, *, reason=REASON, secondary=None, routed=False):
    return CallReview(org_id=org, user_email="a@example.dev", call_id=ref, endpoint_id="apollo.people.search",
                      usefulness="partly", reason=reason, primary_code=code, secondary_code=secondary,
                      codebook_version=V, routed_via="people.search" if routed else None)


def _record(org, ref, key=None, *, status=200, tier="platform", hit=None):
    return CallRecord(org_id=org, user_email="a@example.dev", tool_name="apollo.people.search",
                      method="POST", path="/call/apollo.people.search", status_code=status, call_ref=ref,
                      endpoint_id="apollo.people.search", credential_tier=tier, hit=hit,
                      archive_key_hash=key, archive_content_hash=key and f"c-{key}")


async def _add(*rows):
    async with session_maker() as db:
        for row in rows:
            db.add(row)
        await db.commit()


def _archive(monkeypatch, bodies: dict):
    """Synthetic archive: key hash -> answer text; a key it lacks is no longer on file."""
    async def resolve(key_hash, content_hash):
        if key_hash not in bodies:
            return None
        return {"stored": True, "request": {"method": "POST", "url": "https://x.test/s", "body_text": "{}"},
                "response": {"status_code": 200, "media_type": "application/json", "size_bytes": 99,
                             "body_text": bodies[key_hash]}}
    monkeypatch.setattr(archive, "resolve_result", resolve)


def _jev(monkeypatch, answer=lambda q, item: {"choice": "PARTIAL", "probabilities": {"PARTIAL": 0.8}}):
    """Mock Jev: `answer(question_id, review_item)` per question, or a whole Reply. Returns the
    states it was sent."""
    seen = []

    async def ask(state, questions):
        seen.append(state)
        if isinstance(answer, Reply):
            return answer
        return Reply(answers={q: answer(q, state["reviews"][int(q[1:])]) for q in questions}, ms=1,
                     tokens_in=50)
    monkeypatch.setattr(review_body_pilot, "_ask", ask)
    monkeypatch.setattr(get_settings(), "typesafe_api_key", "test-key", raising=False)
    return seen


async def _codes():
    async with session_maker() as db:
        return {r.call_id: (r.primary_code, r.secondary_code)
                for r in (await db.execute(select(CallReview))).scalars()}


async def test_pilot_compares_codes_and_writes_nothing(clients, monkeypatch):
    # p-0 and p-1 have answers on file, p-2's answer is gone, p-3 has no archive link.
    org = await _org(clients)
    codes = ["PARTIAL", "WRONG", "EMPTY", "ERROR"]
    await _add(*[_review(org, f"p-{i}", c) for i, c in enumerate(codes)],
               _record(org, "p-0", "k0"), _record(org, "p-1", "k1"), _record(org, "p-2", "k2"),
               _record(org, "p-3"))
    _archive(monkeypatch, {"k0": PEOPLE, "k1": PEOPLE})
    seen = _jev(monkeypatch)
    before = await _codes()

    out = await review_body_pilot.run(sample=10, batch=5, budget=500, max_seconds=60)
    assert out["candidates"] == 4 and out["no_body"] == 2 and out["coded"] == 2
    assert out["same_primary"] == 1 and out["pairs"] == {"WRONG->PARTIAL": 1}
    assert out["tokens_in"] == 50 and len(seen) == 1
    sent = seen[0]["reviews"]
    assert [r["i"] for r in sent] == [0, 1] and all(len(r["response"]["body"]) <= 500 for r in sent)
    assert [d["review_id"] for d in out["diffs"]] and "fields came back" not in json.dumps(out)
    assert await _codes() == before


async def test_a_routed_review_is_counted_and_never_compared(clients, monkeypatch):
    # Whatever its attempts' rows say (here a waterfall that moved on from a miss), they cannot
    # prove which answer the caller got (a confirm, an upgrade, a miss), so no routed answer is read.
    org = await _org(clients)
    await _add(_review(org, "wf", "EMPTY", routed=True), _record(org, "wf", tier="routed"),
               _record(org, "wf:r1", "wf-miss", hit=False), _record(org, "wf:r2", "wf-hit", hit=True),
               _review(org, "direct", "EMPTY"), _record(org, "direct", "kd"))
    _archive(monkeypatch, {"wf-miss": "{}", "wf-hit": "{}", "kd": json.dumps({"results": []})})
    read = []
    real = archive.resolve_result

    async def watched(key_hash, content_hash):
        read.append(key_hash)
        return await real(key_hash, content_hash)
    monkeypatch.setattr(archive, "resolve_result", watched)
    seen = _jev(monkeypatch, lambda q, item: {"choice": "EMPTY"})

    out = await review_body_pilot.run(sample=5, batch=5, budget=500, max_seconds=60)
    assert out["answer_unconfirmed"] == 1 and out["coded"] == 1 and read == ["kd"]
    assert [r["routed"] for r in seen[0]["reviews"]] == [False]


async def test_a_confirmed_find_through_the_router_is_never_compared(clients, verifiers_on, monkeypatch):
    """End to end: Tomba finds the email and is returned, Hunter answers no email and is credited.
    The pilot must not hand Jev Hunter's empty answer for a review of Tomba's."""
    import asyncio

    from treg.application.call import service as call_service
    from test_route_confirm import ON, _hunter
    from test_route_verify import ASK, FIND, TOMBA_HIT, _relay

    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [TOMBA_HIT], "hunter": [_hunter(None)]}, []))
    first = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    assert first.status_code == 200, first.text
    assert first.json()["_treg"]["served_by"] == "tomba.people.email.find"
    for _ in range(100):  # the call record is written after the answer
        review = await clients.post("/reviews", json={"call_id": first.headers["X-Treg-Call-Id"],
                                                      "usefulness": "not_useful", "reason": REASON})
        if review.status_code != 404:
            break
        await asyncio.sleep(0.02)
    assert review.status_code == 201, review.text
    async with session_maker() as db:
        row = (await db.execute(select(CallReview))).scalar_one()
        assert row.endpoint_id == "hunter.people.email.find"
        row.primary_code, row.codebook_version = "EMPTY", V
        await db.commit()
    _archive(monkeypatch, {})
    seen = _jev(monkeypatch)

    out = await review_body_pilot.run(sample=5, batch=5, budget=500, max_seconds=60)
    assert out["answer_unconfirmed"] == 1 and out["no_body"] == 0 and seen == []


async def test_vague_noreason_review_is_asked_and_a_blank_one_is_not(clients, monkeypatch):
    org = await _org(clients)
    await _add(_review(org, "vague", "NOREASON", reason="The result was not useful."),
               _review(org, "blank", "NOREASON", reason="   "),
               _record(org, "vague", "kv"), _record(org, "blank", "kb"))
    _archive(monkeypatch, {"kv": json.dumps({"results": []}), "kb": "{}"})
    seen = _jev(monkeypatch, lambda q, item: {"choice": "EMPTY"})

    out = await review_body_pilot.run(sample=5, batch=5, budget=500, max_seconds=60)
    assert out["no_reason"] == 1 and out["coded"] == 1 and len(seen[0]["reviews"]) == 1
    assert out["pairs"] == {"NOREASON->EMPTY": 1} and out["body_codes"] == {"EMPTY": 1}


async def test_one_malformed_answer_is_unreadable_and_the_rest_count(clients, monkeypatch):
    org = await _org(clients)
    await _add(_review(org, "good", "PARTIAL"), _review(org, "bad", "PARTIAL", reason=REASON + " Bad."),
               _record(org, "good", "kg"), _record(org, "bad", "kb"))
    _archive(monkeypatch, {"kg": PEOPLE, "kb": PEOPLE})
    _jev(monkeypatch, lambda q, item: {"choice": ["PARTIAL"]} if item["reason"].endswith("Bad.")
         else {"choice": "PARTIAL"})

    out = await review_body_pilot.run(sample=5, batch=5, budget=500, max_seconds=60)
    assert out["coded"] == 1 and out["unreadable"] == 1 and out["same_primary"] == 1
    assert review_codes.codes_from({"choice": {"code": "EMPTY"}}) is None


async def test_sample_counts_readable_answers_across_pages(clients, monkeypatch):
    # The three newest answers are gone; the fourth is on file, on the second page.
    org = await _org(clients)
    await _add(*[_review(org, f"s-{i}", "PARTIAL") for i in range(4)],
               *[_record(org, f"s-{i}", f"k{i}") for i in range(4)])
    _archive(monkeypatch, {"k0": PEOPLE})
    monkeypatch.setattr(review_body_pilot, "PAGE", 2)
    _jev(monkeypatch)

    out = await review_body_pilot.run(sample=1, batch=5, budget=500, max_seconds=60)
    assert out["coded"] == 1 and out["no_body"] == 3 and out["candidates"] == 4


async def test_a_secondary_only_change_is_a_diff(clients, monkeypatch):
    org = await _org(clients)
    await _add(_review(org, "e", "EMPTY"), _record(org, "e", "ke"))
    _archive(monkeypatch, {"ke": PEOPLE})
    _jev(monkeypatch, lambda q, item: {"choice": "EMPTY", "probabilities": {"EMPTY": 0.6, "PARTIAL": 0.4}})

    out = await review_body_pilot.run(sample=5, batch=5, budget=500, max_seconds=60)
    assert out["same_primary"] == 1 and out["pairs"] == {} and out["changed"] == 1
    assert out["diffs"] == [{"review_id": out["diffs"][0]["review_id"], "reason_codes": ["EMPTY", None],
                             "body_codes": ["EMPTY", "PARTIAL"]}]


def _slow_archive(monkeypatch, slow_key: str):
    """Mock clock: reading `slow_key`'s answer takes 100 s, past a 60 s deadline."""
    now = [0.0]
    monkeypatch.setattr(review_body_pilot, "time", SimpleNamespace(monotonic=lambda: now[0]))

    async def resolve(key_hash, content_hash):
        if key_hash == slow_key:
            now[0] += 100
        return {"stored": True, "request": {"method": "POST", "url": "https://x.test/s", "body_text": "{}"},
                "response": {"status_code": 200, "media_type": "application/json", "size_bytes": 99,
                             "body_text": PEOPLE}}
    monkeypatch.setattr(archive, "resolve_result", resolve)


async def test_a_full_batch_read_past_the_deadline_is_not_asked(clients, monkeypatch):
    org = await _org(clients)
    await _add(_review(org, "a", "PARTIAL"), _review(org, "b", "PARTIAL"),
               _record(org, "a", "ka"), _record(org, "b", "kb"))
    _slow_archive(monkeypatch, "kb")  # "b" is the newest, read first
    seen = _jev(monkeypatch)

    out = await review_body_pilot.run(sample=5, batch=1, budget=500, max_seconds=60)
    assert seen == [] and out["asked"] == 0 and out["read_not_asked"] == 1
    assert out["stopped_by_deadline"] is True


async def test_a_tail_batch_read_past_the_deadline_says_so(clients, monkeypatch):
    org = await _org(clients)
    await _add(_review(org, "a", "PARTIAL"), _record(org, "a", "ka"))
    _slow_archive(monkeypatch, "ka")
    seen = _jev(monkeypatch)

    out = await review_body_pilot.run(sample=5, batch=5, budget=500, max_seconds=60)
    assert seen == [] and out["read_not_asked"] == 1 and out["stopped_by_deadline"] is True


@pytest.mark.parametrize("reviews,page,sample", [(1, 200, 1), (2, 1, 1), (1, 200, 5)])
async def test_the_tail_batch_goes_out_without_another_page(clients, monkeypatch, reviews, page, sample):
    # A short page is the window's last, and a full sample needs no more rows: either way the
    # ready tail is sent before any further query, which here would cross the deadline.
    org = await _org(clients)
    await _add(*[_review(org, f"t-{i}", "PARTIAL") for i in range(reviews)],
               *[_record(org, f"t-{i}", f"k{i}") for i in range(reviews)])
    _archive(monkeypatch, {f"k{i}": PEOPLE for i in range(reviews)})
    monkeypatch.setattr(review_body_pilot, "PAGE", page)
    now = [0.0]
    monkeypatch.setattr(review_body_pilot, "time", SimpleNamespace(monotonic=lambda: now[0]))
    real, pages = review_body_pilot._candidates, []

    async def candidates(*args):
        pages.append(args)
        if len(pages) > 1:
            now[0] += 100
        return await real(*args)
    monkeypatch.setattr(review_body_pilot, "_candidates", candidates)
    _jev(monkeypatch)

    out = await review_body_pilot.run(sample=sample, batch=5, budget=500, max_seconds=60)
    assert len(pages) == 1 and out["asked"] == min(reviews, sample) and out["read_not_asked"] == 0
    assert out["stopped_by_deadline"] is False


async def test_pilot_is_off_without_a_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "typesafe_api_key", "", raising=False)
    assert await review_body_pilot.run(sample=5, batch=5, budget=500, max_seconds=5) == {
        "skipped": "no_judge_key"}
    assert review_judge.configured() is False


def test_shrink_keeps_json_valid_when_cuts_are_not_enough():
    nested = {"data": {"results": [{"person": {"name": "n" * 50, "work": {"title": "t" * 50,
                                    "company": {"name": "c" * 50, "domain": "d" * 50}}}}] * 10}}
    for budget in (40, 120, 300, 500):
        out = body_view.shrink(json.dumps(nested), budget)
        assert len(out) <= max(budget, len('{"_omitted_bytes":9999}'))
        json.loads(out)


async def test_the_judge_sees_the_status_and_body_the_caller_got(clients, monkeypatch):
    # One stored body answered this call with 206; the provider's reported charge is stripped
    # before delivery, so the judge never sees it either.
    from treg.application import review_answers
    from treg.application.call import service as call_service

    org = await _org(clients)
    record = _record(org, "s", "ks", status=206)
    record.cost_charged_micro = 100
    await _add(_review(org, "s", "PARTIAL"), record)

    async def resolve(key_hash, content_hash):
        return {"stored": True, "request": {"method": "POST", "url": "u", "body_text": "{}"},
                "response": {"status_code": 200, "media_type": "application/json", "size_bytes": 9,
                             "body_text": '{"people":[],"credits_charged":2}'}}
    monkeypatch.setattr(archive, "resolve_result", resolve)
    monkeypatch.setattr(call_service, "answer_for_caller",
                        lambda endpoint_id, body, headers: body.replace(b',"credits_charged":2', b""))
    async with session_maker() as db:
        review = (await db.execute(select(CallReview))).scalar_one()
    found = await review_answers.answer_of(review)
    assert found["response"]["status_code"] == 206 and "credits_charged" not in found["response"]["body_text"]
