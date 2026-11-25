"""The body pilot: negative reviews coded again with the call's answer in view, compared with the
reason-only codes, writing nothing; and the shortened answer the judge reads."""
import json

import pytest

from sqlmodel import select

from treg import archive, worker
from treg.application import review_body_pilot, review_judge
from treg.config import get_settings
from treg.domain.feedback import body_view, review_codes
from treg.infra.db import session_maker
from treg.infra.judge import Reply
from treg.models import CallRecord, CallReview

V = review_codes.CODEBOOK_VERSION


def test_shrink_keeps_structure_and_counts_cut_items():
    body = json.dumps({"results": [{"name": "x" * 500, "email": None} for _ in range(40)], "total": 40})
    out = body_view.shrink(body, 1000)
    assert len(out) <= 1000
    value = json.loads(out)
    assert value["total"] == 40 and value["results"][-1]["_more"] > 0
    assert value["results"][0]["email"] is None and value["results"][0]["name"].endswith("…")


def test_shrink_strips_html_and_cuts_text():
    out = body_view.shrink("<html><script>var a=1</script><p>Access   denied</p></html>", 100)
    assert out == "Access denied"
    assert len(body_view.shrink("word " * 1000, 50)) == 50
    assert body_view.shrink(None, 50) == ""


REASON = "Half of the requested fields came back empty for these people."
PEOPLE = json.dumps({"people": [{"email": None}] * 30})


@pytest.mark.parametrize("budget", [0, -5])
def test_shrink_refuses_a_non_positive_budget(budget):
    with pytest.raises(ValueError):
        body_view.shrink("word " * 2000, budget)


@pytest.mark.parametrize("flag", ["--budget", "--sample", "--batch"])
@pytest.mark.parametrize("value", ["0", "-5"])
def test_cli_refuses_non_positive_numbers(flag, value, capsys):
    with pytest.raises(SystemExit):
        worker.main(["reviews", "body-pilot", flag, value])
    assert "must be >= 1" in capsys.readouterr().err


async def _org(clients) -> int:
    return (await clients.post("/orgs", json={"name": "Pilot"})).json()["org_id"]


def _review(org, ref, code, *, reason=REASON, secondary=None, routed=False):
    return CallReview(org_id=org, user_email="a@example.dev", call_id=ref, endpoint_id="apollo.people.search",
                      usefulness="partly", reason=reason, primary_code=code, secondary_code=secondary,
                      codebook_version=V, routed_via="people.search" if routed else None)


def _record(org, ref, key=None, *, status=200, tier="platform"):
    return CallRecord(org_id=org, user_email="a@example.dev", tool_name="apollo.people.search",
                      method="POST", path="/call/apollo.people.search", status_code=status, call_ref=ref,
                      endpoint_id="apollo.people.search", credential_tier=tier,
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


async def test_routed_review_reads_its_successful_attempt(clients, monkeypatch):
    # The parent has no archive link; the answer is on the successful attempt, not on the failed
    # one or the async poll, and another team's same-named attempt is never read.
    org, other = await _org(clients), await _org(clients)
    await _add(_review(org, "rt", "EMPTY", routed=True),
               _record(org, "rt", tier="routed"),
               _record(org, "rt:r1", "failed", status=502),
               _record(org, "rt:r2", "child"),
               _record(org, "rt:r2:p1", "poll"),
               _record(other, "rt:r3", "foreign"))
    _archive(monkeypatch, {"failed": "{}", "child": json.dumps({"results": []}), "poll": "{}", "foreign": "{}"})
    seen = _jev(monkeypatch, lambda q, item: {"choice": "EMPTY"})

    out = await review_body_pilot.run(sample=5, batch=5, budget=500, max_seconds=60)
    assert out["coded"] == 1 and out["same_primary"] == 1
    item = seen[0]["reviews"][0]
    assert item["routed"] is True and json.loads(item["response"]["body"]) == {"results": []}


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
