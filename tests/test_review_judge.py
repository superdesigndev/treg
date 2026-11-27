"""Jev's judgements of review reasons: privacy for quotable reasons, codebook codes for negative
reviews, each pass written once and failing on its own."""
from datetime import timedelta

from sqlalchemy import update
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
    assert rows["j-0"].code_basis == "reason_final"  # routed: its answer is never read
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


async def test_the_judge_runs_from_reasons_when_object_storage_is_unavailable(clients, monkeypatch, capsys):
    import argparse

    from treg import bootstrap, worker

    def broken():
        raise RuntimeError("Archive R2 is enabled but its endpoint, bucket or credentials are missing/invalid")

    async def run(*, max_seconds):
        return {"codes": {"judged": 0}}
    monkeypatch.setattr(bootstrap, "archive_object_store", broken)
    monkeypatch.setattr(review_judge, "run", run)
    assert await worker._reviews_judge(argparse.Namespace(max_seconds=5)) == 0
    assert '"judged": 0' in capsys.readouterr().out


async def _negatives(clients, *names, routed=()):
    """Direct (or routed) partly reviews coded from their reason this codebook, each with a call row
    whose archive key is its name."""
    from test_review_body_pilot import _add, _org, _record, _review

    org = await _org(clients)
    rows = []
    for name in names:
        rows += [_review(org, name, "WRONG", routed=name in routed),
                 _record(org, name, None if name in routed else f"k-{name}")]
    await _add(*rows)
    async with session_maker() as db:
        for r in (await db.execute(select(CallReview))).scalars():
            r.code_basis = "reason_final" if r.routed_via else "reason"
        await db.commit()


def _answers(monkeypatch, states: dict, clock=None):
    """Mock `answer_of`: review call_id -> "read" | "absent" | "unreadable" (default "read")."""
    from treg.application import review_answers
    from test_review_body_pilot import PEOPLE

    found = {"request": {"method": "POST", "url": "u", "body_text": "{}"},
             "response": {"status_code": 200, "body_text": PEOPLE}}
    reads = []

    async def answer_of(r):
        reads.append(r.call_id)
        state = states.get(r.call_id, "read")
        if clock is not None:
            clock(r)
        return review_answers.Answer(state, found if state == "read" else None)
    monkeypatch.setattr(review_judge, "answer_of", answer_of)
    return reads


async def test_reason_codes_come_first_and_answers_upgrade_them(clients, monkeypatch):
    """Every review is coded from its reason first; a direct one is then upgraded with its answer,
    a few per request. A routed review is never read, and one whose answer is gone keeps its
    reason codes for good."""
    await _negatives(clients, "d-0", "d-1", "d-2", "gone", "rt", routed=("rt",))
    reads = _answers(monkeypatch, {"gone": "absent"})
    monkeypatch.setattr(get_settings(), "review_judge_answer_batch", 2)
    seen = _fake(monkeypatch, lambda q, spec, state: {"choice": "EMPTY"})

    out = await review_judge.judge_answers(deadline=float("inf"))
    assert out["judged"] == 3 and out["final"] == 1 and out["retry_later"] == 0
    assert "rt" not in reads and [len(state["reviews"]) for state, _ in seen] == [2, 1]
    rows = await _all()
    assert (rows["d-0"].primary_code, rows["d-0"].code_basis) == ("EMPTY", "answer")
    assert (rows["gone"].primary_code, rows["gone"].code_basis) == ("WRONG", "reason_final")
    assert (rows["rt"].primary_code, rows["rt"].code_basis) == ("WRONG", "reason_final")
    again = await review_judge.judge_answers(deadline=float("inf"))
    assert again["pending"] == 0


async def test_an_unreadable_answer_backs_off_and_never_holds_up_the_rest(clients, monkeypatch):
    """Answers that cannot be read now are tried again later, not at the next run; reviews after
    them are upgraded in the same run, and after the last try the reason codes stand."""
    await _negatives(clients, *[f"u-{i}" for i in range(20)], "late", "no-link")
    reads = _answers(monkeypatch, {**{f"u-{i}": "unreadable" for i in range(20)}, "no-link": "absent"})
    _fake(monkeypatch, lambda q, spec, state: {"choice": "EMPTY"})

    out = await review_judge.judge_answers(deadline=float("inf"))
    rows = await _all()
    assert out["retry_later"] == 20 and rows["late"].code_basis == "answer"
    assert rows["no-link"].code_basis == "reason_final"
    assert rows["u-0"].answer_attempts == 1 and rows["u-0"].answer_retry_at is not None

    reads.clear()
    assert (await review_judge.judge_answers(deadline=float("inf")))["pending"] == 0 and reads == []

    monkeypatch.setattr(get_settings(), "review_judge_answer_attempts", 2)
    async with session_maker() as db:  # their time has come
        for r in (await db.execute(select(CallReview))).scalars():
            r.answer_retry_at = None if r.answer_retry_at else r.answer_retry_at
        await db.commit()
    last = await review_judge.judge_answers(deadline=float("inf"))
    rows = await _all()
    assert last["final"] == 20 and rows["u-0"].code_basis == "reason_final" and rows["u-0"].primary_code == "WRONG"


async def test_a_new_codebook_version_codes_from_the_reason_and_upgrades_again(clients, monkeypatch):
    await _negatives(clients, "v")
    _answers(monkeypatch, {})
    _fake(monkeypatch, lambda q, spec, state: {"choice": "EMPTY" if "response" in state["reviews"][0] else "PARAM"})
    await review_judge.judge_answers(deadline=float("inf"))
    assert (await _all())["v"].code_basis == "answer"
    monkeypatch.setattr(review_codes, "CODEBOOK_VERSION", "2099-01-01.1")
    await review_judge.judge_codes(deadline=float("inf"))
    row = (await _all())["v"]
    assert (row.primary_code, row.code_basis) == ("PARAM", "reason")
    await review_judge.judge_answers(deadline=float("inf"))
    assert (await _all())["v"].code_basis == "answer"


async def test_a_slow_last_read_does_not_drop_what_was_read(clients, monkeypatch):
    """1 s to run: two reads of 0.1 s, then one of 0.9 s, cut off by its time limit. That review
    is left for the next run, and the two read are sent before the deadline."""
    from types import SimpleNamespace

    await _negatives(clients, "s-0", "s-1", "s-2")
    now, limit = [0.0], [None]
    monkeypatch.setattr(review_judge, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def clock(r):
        took = 0.9 if r.call_id == "s-0" else 0.1  # the oldest is read last, and slowly
        if took > limit[0]:  # what asyncio.wait_for does: cut off at its timeout
            now[0] += limit[0]
            raise TimeoutError
        now[0] += took
    _answers(monkeypatch, {}, clock=clock)

    async def wait_for(coro, timeout):
        limit[0] = timeout
        return await coro
    monkeypatch.setattr(review_judge, "_wait_for", wait_for)
    sent_at = []
    _fake(monkeypatch, lambda q, spec, state: sent_at.append(now[0]) or {"choice": "EMPTY"})

    out = await review_judge.judge_answers(deadline=1.0)
    assert out["judged"] == 2 and out["stopped_by_deadline"] is True and max(sent_at) < 1.0
    cut = (await _all())["s-0"]
    assert cut.code_basis == "reason" and cut.answer_retry_at is not None and not cut.answer_attempts
    # It waits a few hours (spending no try), so the next run is not cut on it again.
    now[0] = 0.0
    assert (await review_judge.judge_answers(deadline=10.0))["pending"] == 0


async def test_a_slow_read_cut_by_the_budget_lets_the_next_review_through_next_run(clients, monkeypatch):
    """The newest review's read is slow and is cut by the run's budget every time; the one after it
    is upgraded on the next run instead of never being reached."""
    from types import SimpleNamespace

    await _negatives(clients, "fast", "slow")  # "slow" is newer, so read first
    now = [0.0]
    monkeypatch.setattr(review_judge, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def clock(r):
        if r.call_id == "slow":
            raise TimeoutError
    _answers(monkeypatch, {}, clock=clock)
    _fake(monkeypatch, lambda q, spec, state: {"choice": "EMPTY"})

    first = await review_judge.judge_answers(deadline=1.0)
    assert first["judged"] == 0 and first["stopped_by_deadline"] is True
    second = await review_judge.judge_answers(deadline=1.0)
    assert second["judged"] == 1 and (await _all())["fast"].code_basis == "answer"


async def test_an_answer_read_wins_over_a_concurrent_give_up(clients, monkeypatch):
    """Run A has read the answer and waits on Jev; run B, meanwhile, spends the last try and makes
    the reason codes final. A's judgement is still written; a newer codebook still is not touched."""
    from treg.domain.feedback import reviews as reviews_domain

    await _negatives(clients, "race")
    review_id = (await _all())["race"].id
    v = review_codes.CODEBOOK_VERSION
    async with session_maker() as db:
        assert await reviews_domain.record_answer_final(db, review_id, v)
        await db.commit()
    async with session_maker() as db:
        assert await reviews_domain.record_answer_codes(db, review_id, "EMPTY", None, v, utcnow_naive())
        await db.commit()
    row = (await _all())["race"]
    assert (row.primary_code, row.code_basis) == ("EMPTY", "answer")
    async with session_maker() as db:  # never over another answer judgement
        assert not await reviews_domain.record_answer_codes(db, review_id, "WRONG", None, v, utcnow_naive())


async def test_the_answer_pass_can_be_switched_off(clients, monkeypatch):
    calls = []

    async def answers(*, deadline):
        calls.append(1)
        return {}
    monkeypatch.setattr(review_judge, "judge_answers", answers)
    _fake(monkeypatch, lambda q, spec, state: {"choice": "WRONG"})
    monkeypatch.setattr(get_settings(), "review_judge_answers", False, raising=False)
    out = await review_judge.run(max_seconds=5)
    assert "answers" not in out and calls == []
    monkeypatch.setattr(get_settings(), "review_judge_answers", True, raising=False)
    assert "answers" in await review_judge.run(max_seconds=5)


async def test_a_budget_cut_never_moves_back_a_failure_another_run_recorded(clients, monkeypatch):
    """Run A read a review at 4 failed tries; run B meanwhile records the 5th with its back-off. A's
    read is then cut by its budget: B's count and back-off stand. Two runs failing together count
    once."""
    from datetime import timedelta

    from treg.domain.feedback import reviews as reviews_domain

    await _negatives(clients, "busy")
    review_id = (await _all())["busy"].id
    v = review_codes.CODEBOOK_VERSION
    later = utcnow_naive() + timedelta(hours=16)
    async with session_maker() as db:
        assert await reviews_domain.record_answer_retry(db, review_id, v, None, utcnow_naive())
        await db.commit()
    for _ in range(3):
        seen = (await _all())["busy"].answer_attempts
        async with session_maker() as db:
            assert await reviews_domain.record_answer_retry(db, review_id, v, seen, utcnow_naive())
            await db.commit()
    assert (await _all())["busy"].answer_attempts == 4
    async with session_maker() as db:  # B: the 5th failure
        assert await reviews_domain.record_answer_retry(db, review_id, v, 4, later)
        await db.commit()
    async with session_maker() as db:  # A, which read 4: its budget wait and its failure are refused
        assert not await reviews_domain.record_answer_wait(db, review_id, v, 4, utcnow_naive())
        assert not await reviews_domain.record_answer_retry(db, review_id, v, 4, utcnow_naive())
        await db.commit()
    row = (await _all())["busy"]
    assert row.answer_attempts == 5 and row.answer_retry_at == later


async def test_tries_running_out_never_ends_a_back_off_another_run_moved_on(clients, monkeypatch):
    """A worker allowed fewer tries read 4 and gives up; one allowed more recorded the 5th meanwhile:
    its back-off stands. An answer gone for good still ends it whatever the count."""
    from datetime import timedelta

    from treg.domain.feedback import reviews as reviews_domain

    await _negatives(clients, "limits")
    review_id = (await _all())["limits"].id
    v = review_codes.CODEBOOK_VERSION
    later = utcnow_naive() + timedelta(hours=16)
    async with session_maker() as db:
        await db.execute(update(CallReview).where(CallReview.id == review_id).values(answer_attempts=5,
                                                                                      answer_retry_at=later))
        await db.commit()
    async with session_maker() as db:
        assert not await reviews_domain.record_answer_final(db, review_id, v, 4)
        await db.commit()
    row = (await _all())["limits"]
    assert (row.code_basis, row.answer_attempts, row.answer_retry_at) == ("reason", 5, later)
    async with session_maker() as db:
        assert await reviews_domain.record_answer_final(db, review_id, v)
        await db.commit()
    assert (await _all())["limits"].code_basis == "reason_final"


async def test_praise_codes_useful_reviews_from_their_reasons_only(clients, monkeypatch):
    await _reviews(clients, [
        ("useful", "Clean miss with no charge for a domain not in the index.", {}),
        ("useful", None, {}),
        ("partly", LONG, {}),
        ("useful", "Returned the full profile and the email verified.", {"routed_via": "email.find"}),
        ("useful", "It was fine, really, nothing more to say about it.", {}),
    ])

    def answer(q, spec, state):
        review = state["reviews"][int(q[1:])]
        if review["usefulness"] != "useful":
            return None
        if "Clean miss" in review["reason"]:
            return {"choice": "HONEST", "probabilities": {"HONEST": 0.6, "CHEAP": 0.35}}
        if "full profile" in review["reason"]:
            return {"choice": "COMPLETE", "probabilities": {"COMPLETE": 0.9, "ACCURATE": 0.1}}
        return {"choice": "EMPTY"}  # a negative code is not a praise code
    seen = _fake(monkeypatch, answer)
    out = await review_judge.judge_praise(deadline=float("inf"))
    assert out == {"pending": 4, "judged": 2, "no_reason": 1, "already_judged": 0, "failed_requests": 0,
                   "unreadable": 1, "tokens_in": 10}
    assert set(seen[0][1]["a0"]["criteria"]) == set(review_codes.PRAISE_CODES)
    rows = await _all()
    assert (rows["j-0"].primary_code, rows["j-0"].secondary_code) == ("HONEST", "CHEAP")
    assert (rows["j-3"].primary_code, rows["j-3"].secondary_code) == ("COMPLETE", None)
    assert rows["j-1"].primary_code == "NOREASON"
    for k in ("j-0", "j-1", "j-3"):  # never upgraded with the answer, direct or routed
        assert rows[k].code_basis == "reason_final"
        assert rows[k].codebook_version == review_codes.PRAISE_CODEBOOK_VERSION
    assert rows["j-2"].code_judged_at is None  # negative reviews are the other pass's
    assert rows["j-4"].code_judged_at is None  # unreadable: next run tries again
    # The negative pass never takes useful reviews, and the upgrade never reads their answers.
    assert (await review_judge.judge_codes(deadline=float("inf")))["pending"] == 1
    assert (await _all())["j-0"].primary_code == "HONEST"
    assert (await review_judge.judge_answers(deadline=float("inf")))["pending"] == 0


async def test_praise_is_bounded_per_run_and_by_its_window(clients, monkeypatch):
    old = {"created_at": utcnow_naive() - timedelta(days=29)}
    await _reviews(clients, [("useful", f"{LONG} {i}", {}) for i in range(5)] + [("useful", LONG, old)])
    monkeypatch.setattr(get_settings(), "review_judge_praise_per_run", 3, raising=False)
    monkeypatch.setattr(get_settings(), "review_judge_praise_days", 28, raising=False)
    _fake(monkeypatch, lambda q, spec, state: {"choice": "COMPLETE"})
    assert (await review_judge.judge_praise(deadline=float("inf")))["judged"] == 3
    assert (await review_judge.judge_praise(deadline=float("inf")))["judged"] == 2
    assert (await review_judge.judge_praise(deadline=float("inf")))["pending"] == 0
    assert (await _all())["j-5"].code_judged_at is None  # older than the praise window


async def test_a_new_praise_codebook_version_judges_old_praise_again(clients, monkeypatch):
    old = {"primary_code": "GENERIC", "codebook_version": "2000-01-01", "code_judged_at": utcnow_naive()}
    current = old | {"codebook_version": review_codes.PRAISE_CODEBOOK_VERSION}
    await _reviews(clients, [("useful", LONG, old), ("useful", LONG, current)])
    _fake(monkeypatch, lambda q, spec, state: {"choice": "COMPLETE"})
    out = await review_judge.judge_praise(deadline=float("inf"))
    assert out["pending"] == 1 and out["judged"] == 1
    assert (await _all())["j-0"].primary_code == "COMPLETE"


async def test_the_praise_pass_can_be_switched_off(clients, monkeypatch):
    await _reviews(clients, [("useful", LONG, {})])
    _fake(monkeypatch, lambda q, spec, state: {"choice": "COMPLETE"})
    monkeypatch.setattr(get_settings(), "review_judge_answers", False, raising=False)
    monkeypatch.setattr(get_settings(), "review_judge_praise", False, raising=False)
    assert "praise" not in await review_judge.run(max_seconds=5)
    assert (await _all())["j-0"].code_judged_at is None
    monkeypatch.setattr(get_settings(), "review_judge_praise", True, raising=False)
    assert (await review_judge.run(max_seconds=5))["praise"]["judged"] == 1


def test_praise_codes_never_share_a_name_with_a_negative_code():
    assert not set(review_codes.PRAISE_CODES) & set(review_codes.CODES)
    assert review_codes.codes_from({"choice": "ACCURATE"}) is None
    assert review_codes.codes_from({"choice": "ACCURATE"}, review_codes.PRAISE_CODES) == ("ACCURATE", None)
