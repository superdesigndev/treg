"""Jev's judgements of review reasons: privacy for quotable reasons, codebook codes for negative
reviews, each pass written once and failing on its own."""
from datetime import timedelta

from sqlmodel import select

from treg.application import review_judge
from treg.config import get_settings
from treg.domain.feedback import review_codes
from treg.infra.db import session_maker
from treg.infra.judge import Reply
from treg.models import CallReview
from treg.timeutil import utcnow_naive

LONG = "Returned the requested records with the fields the task needed, nothing missing."


async def _reviews(clients, rows):
    org = (await clients.post("/orgs", json={"name": "Judged"})).json()["org_id"]
    async with session_maker() as db:
        for i, (usefulness, reason, extra) in enumerate(rows):
            db.add(CallReview(org_id=org, user_email="agent@example.dev", call_id=f"j-{i}",
                              endpoint_id="apollo.people.search", usefulness=usefulness, reason=reason,
                              **extra))
        await db.commit()


async def _all():
    async with session_maker() as db:
        return {r.call_id: r for r in (await db.execute(select(CallReview))).scalars()}


def _fake(monkeypatch, answer):
    """Jev stand-in: `answer(question_id, question, state)` per question, or a whole Reply."""
    seen = []

    async def ask(state, questions):
        seen.append((state, questions))
        if isinstance(answer, Reply):
            return answer
        return Reply(answers={q: answer(q, spec, state) for q, spec in questions.items()}, ms=1,
                     tokens_in=10)
    monkeypatch.setattr(review_judge, "_ask", ask)
    monkeypatch.setattr(get_settings(), "typesafe_api_key", "test-key", raising=False)
    return seen


async def test_privacy_judges_only_quotable_reasons(clients, monkeypatch):
    await _reviews(clients, [("useful", LONG, {}), ("useful", "Short.", {}),
                             ("partly", LONG + " Call 732-433-0667.", {}),
                             ("useful", LONG + " Jane Doe at Acme was right.", {})])
    seen = _fake(monkeypatch, lambda q, spec, state: (
        {"noul": 0.97} if "Jane" in state["reviews"][int(q[1:])]["reason"] else {"noul": 0.02}))
    out = await review_judge.judge_privacy(deadline=float("inf"))
    assert out["pending"] == 2 and out["judged"] == 2
    assert all(spec["type"] == "noul" for _, questions in seen for spec in questions.values())
    rows = await _all()
    assert rows["j-0"].person_p == 0.02 and rows["j-0"].privacy_judged_at is not None
    assert rows["j-3"].person_p == 0.97
    # Never quotable anyway: not worth a question.
    assert rows["j-1"].privacy_judged_at is None and rows["j-2"].privacy_judged_at is None
    # Judged once.
    assert (await review_judge.judge_privacy(deadline=float("inf")))["pending"] == 0


async def test_a_failed_or_unreadable_privacy_answer_leaves_the_review_unjudged(clients, monkeypatch):
    await _reviews(clients, [("useful", LONG, {}), ("useful", LONG + " again", {})])
    _fake(monkeypatch, Reply(answers=None, ms=1, error="timeout"))
    assert (await review_judge.judge_privacy(deadline=float("inf")))["failed_requests"] == 1
    _fake(monkeypatch, lambda q, spec, state: {"noul": "n/a"} if q == "p0" else {"noul": 1.4})
    out = await review_judge.judge_privacy(deadline=float("inf"))
    assert out["unreadable"] == 2 and out["judged"] == 0
    # A boolean is not a probability: `false` must not read as 0.0 and clear a quote.
    _fake(monkeypatch, lambda q, spec, state: {"noul": False} if q == "p0" else None)
    out = await review_judge.judge_privacy(deadline=float("inf"))
    assert out["unreadable"] == 2 and out["judged"] == 0
    assert all(r.privacy_judged_at is None for r in (await _all()).values())


async def test_codes_judge_negative_reviews_with_their_context(clients, monkeypatch):
    await _reviews(clients, [
        ("not_useful", "Returned a guessed address that bounced when we sent to it.", {"routed_via": "email.find"}),
        ("partly", None, {}),
        ("useful", LONG, {}),
        ("partly", "Some answer the judge cannot code properly at all, really.", {}),
    ])

    def answer(q, spec, state):
        review = state["reviews"][int(q[1:])]
        if "bounced" in review["reason"]:
            assert review["routed"] is True and review["capability"] == "people.search"
            return {"choice": "ACCURACY", "confidence": 0.7,
                    "probabilities": {"ACCURACY": 0.6, "RISKY": 0.3, "EMPTY": 0.1}}
        return {"choice": "SOMETHING_ELSE"}
    seen = _fake(monkeypatch, answer)
    out = await review_judge.judge_codes(deadline=float("inf"))
    assert out == {"pending": 3, "judged": 1, "no_reason": 1, "already_judged": 0, "failed_requests": 0,
                   "unreadable": 1, "tokens_in": 10}
    assert set(seen[0][1]["a0"]["criteria"]) == set(review_codes.CODES)
    rows = await _all()
    assert (rows["j-0"].primary_code, rows["j-0"].secondary_code) == ("ACCURACY", "RISKY")
    assert rows["j-0"].codebook_version == review_codes.CODEBOOK_VERSION
    assert rows["j-1"].primary_code == "NOREASON"   # no reason: coded without asking
    assert rows["j-2"].code_judged_at is None       # useful reviews are not attributed
    assert rows["j-3"].code_judged_at is None       # unreadable: next run tries again


async def test_a_new_codebook_version_judges_old_codes_again(clients, monkeypatch):
    old = {"primary_code": "EMPTY", "codebook_version": "2000-01-01", "code_judged_at": utcnow_naive()}
    current = old | {"codebook_version": review_codes.CODEBOOK_VERSION}
    await _reviews(clients, [("partly", "Nothing came back for this domain at all, empty list.", old),
                             ("partly", "Nothing came back for this domain at all, empty list.", current)])
    _fake(monkeypatch, lambda q, spec, state: {"choice": "EMPTY", "probabilities": {"EMPTY": 0.9}})
    out = await review_judge.judge_codes(deadline=float("inf"))
    assert out["pending"] == 1 and out["judged"] == 1
    assert (await _all())["j-0"].codebook_version == review_codes.CODEBOOK_VERSION


async def _set(call_id, **values):
    from sqlalchemy import update
    async with session_maker() as db:
        await db.execute(update(CallReview).where(CallReview.call_id == call_id).values(**values))
        await db.commit()


async def test_an_overlapping_run_never_replaces_the_first_privacy_answer(clients, monkeypatch):
    await _reviews(clients, [("useful", LONG, {})])

    async def ask(state, questions):
        # Another run judged the review while this one waited for Jev.
        await _set("j-0", person_p=0.02, privacy_judged_at=utcnow_naive())
        return Reply(answers={q: {"noul": 0.9} for q in questions}, ms=1)
    monkeypatch.setattr(review_judge, "_ask", ask)
    out = await review_judge.judge_privacy(deadline=float("inf"))
    assert out["judged"] == 0 and out["already_judged"] == 1
    assert (await _all())["j-0"].person_p == 0.02


async def test_an_older_build_never_downgrades_a_newer_codebook(clients, monkeypatch):
    newer = {"primary_code": "COVER", "codebook_version": "2099-01-01", "code_judged_at": utcnow_naive()}
    await _reviews(clients, [("partly", "Nothing came back for this domain at all, empty list.", newer),
                             ("partly", "Nothing came back for that domain at all, empty list.", {})])

    async def ask(state, questions):
        await _set("j-1", primary_code="COVER", codebook_version="2099-01-01")
        return Reply(answers={q: {"choice": "EMPTY", "probabilities": {"EMPTY": 1.0}} for q in questions}, ms=1)
    monkeypatch.setattr(review_judge, "_ask", ask)
    out = await review_judge.judge_codes(deadline=float("inf"))
    assert out["pending"] == 1 and out["judged"] == 0 and out["already_judged"] == 1
    rows = await _all()
    assert [(rows[k].primary_code, rows[k].codebook_version) for k in ("j-0", "j-1")] == [("COVER", "2099-01-01")] * 2


async def test_reviews_outside_the_window_are_left_alone(clients, monkeypatch):
    await _reviews(clients, [("partly", LONG, {"created_at": utcnow_naive() - timedelta(days=120)})])
    _fake(monkeypatch, lambda q, spec, state: {"noul": 0.0})
    assert (await review_judge.judge_privacy(deadline=float("inf")))["pending"] == 0
    assert (await review_judge.judge_codes(deadline=float("inf")))["pending"] == 0


async def test_one_pass_failing_never_stops_the_other(clients, monkeypatch):
    await _reviews(clients, [("partly", LONG, {})])
    _fake(monkeypatch, lambda q, spec, state: {"choice": "PARTIAL", "probabilities": {"PARTIAL": 1.0}})

    async def broken(*, deadline):
        raise RuntimeError("boom")
    monkeypatch.setattr(review_judge, "judge_privacy", broken)
    out = await review_judge.run(max_seconds=60)
    assert out["privacy"] == {"error": "RuntimeError"} and out["codes"]["judged"] == 1


async def test_without_a_judge_key_nothing_runs(monkeypatch):
    monkeypatch.setattr(get_settings(), "typesafe_api_key", "", raising=False)
    assert await review_judge.run(max_seconds=60) == {"skipped": "no_judge_key"}


async def test_one_malformed_code_answer_never_costs_its_batch(clients, monkeypatch):
    await _reviews(clients, [("partly", "Nothing came back for this domain at all, empty list.", {}),
                             ("partly", "Nothing came back for that domain at all, empty list.", {})])
    _fake(monkeypatch, lambda q, spec, state: (
        {"choice": "EMPTY", "probabilities": {"EMPTY": 0.7, "COVER": None}} if q == "a0"
        else {"choice": "COVER", "probabilities": "oops"}))
    out = await review_judge.judge_codes(deadline=float("inf"))
    assert out["judged"] == 2 and out["unreadable"] == 0
    rows = await _all()
    assert {(r.primary_code, r.secondary_code) for r in rows.values()} == {("EMPTY", None), ("COVER", None)}


def test_only_a_number_in_range_is_a_probability():
    assert [review_codes.person_p_from(a) for a in (
        {"noul": 0.2}, {"noul": 0}, {"noul": False}, {"noul": True}, {"noul": "0.1"}, {"noul": float("nan")},
        {"noul": -0.1}, {}, None, 0.1)] == [0.2, 0.0, None, None, None, None, None, None, None, None]


def test_a_second_code_needs_real_weight():
    assert review_codes.codes_from({"choice": "EMPTY", "probabilities": {"EMPTY": 0.8, "COVER": 0.2}}) == ("EMPTY", None)
    assert review_codes.codes_from({"choice": "EMPTY", "probabilities": {"EMPTY": 0.6, "COVER": 0.3}}) == ("EMPTY", "COVER")
    assert review_codes.codes_from({"choice": "maybe"}) is None


async def test_the_worker_command(clients, monkeypatch, capsys):
    import argparse
    import json

    import treg.infra.db as infra_db
    from treg import worker

    async def verified():
        return None
    monkeypatch.setattr(infra_db, "verify_db", verified)
    await _reviews(clients, [("partly", LONG, {})])
    _fake(monkeypatch, lambda q, spec, state: (
        {"noul": 0.01} if q.startswith("p") else {"choice": "PARTIAL", "probabilities": {"PARTIAL": 1.0}}))
    assert await worker._reviews_judge(argparse.Namespace(max_seconds=60)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["privacy"]["judged"] == 1 and out["codes"]["judged"] == 1


def test_the_codebook_sends_quota_on_treg_s_account_to_treg_and_caller_scope_to_context():
    # A choice is decided mostly by its criteria, so the rules live there, not only in the instructions.
    assert "treg's own account" in review_codes.CODES["TREG"]
    assert "which is TREG" in review_codes.CODES["ERROR"]
    assert "unfiltered" in review_codes.CODES["CONTEXT"] and "target segment" in review_codes.CODES["CONTEXT"]
    assert review_codes.CODES["RISKY"].startswith("The vendor's own output")
