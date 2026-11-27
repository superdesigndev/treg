"""Jev's blind verdicts on stored comparisons: asked in both orders, written apart from the agent's
vote, split when the orders disagree, and stepping aside when it cannot judge."""
import asyncio
import time
from datetime import timedelta

from sqlmodel import select

from treg.application import compare_judge, review_judge
from treg.config import get_settings
from treg.domain.feedback import compare_rubric
from treg.infra.db import session_maker
from treg.infra.judge import Reply
from treg.models import CallComparison
from treg.timeutil import utcnow_naive


async def _pairs(clients, rows):
    org = (await clients.post("/orgs", json={"name": "Compared"})).json()["org_id"]
    async with session_maker() as db:
        for i, extra in enumerate(rows):
            values = dict(org_id=org, user_email="agent@example.dev", call_ref=f"c-{i}",
                          capability="web.search", a_endpoint="x.search", a_provider="x",
                          b_endpoint="y.search", b_provider="y", input='{"query": "q"}',
                          a_body=f'{{"rows": ["A{i}"]}}', b_body=f'{{"rows": ["B{i}"]}}', status="ready",
                          created_at=utcnow_naive() - timedelta(minutes=i))
            db.add(CallComparison(**(values | extra)))
        await db.commit()


async def _all():
    async with session_maker() as db:
        return {r.call_ref: r for r in (await db.execute(select(CallComparison))).scalars()}


def _fake(monkeypatch, answer):
    """Jev stand-in: `answer(pair)` per question gives the shown choice, or a whole Reply."""
    seen = []

    async def ask(state, questions, *, timeout_s):
        seen.append((state, questions))
        if callable(getattr(answer, "reply", None)):
            return await answer.reply(state, timeout_s)
        if isinstance(answer, Reply):
            return answer
        return Reply(answers={q: answer(state["pairs"][int(q[1:])]) for q in questions}, ms=1, tokens_in=7)
    monkeypatch.setattr(compare_judge, "_ask", ask)
    monkeypatch.setattr(get_settings(), "typesafe_api_key", "test-key", raising=False)
    return seen


def _prefers(letter):
    """A judge that always picks the answer whose rows start with `letter`, whatever its place."""
    return lambda pair: {"choice": "1" if f'"{letter}' in pair["answer_1"] else "2"}


async def test_both_orders_are_asked_and_the_verdict_is_kept_apart_from_the_agents(clients, monkeypatch):
    await _pairs(clients, [{"status": "decided", "choice": "b", "reason": "agent", "first": "b"}, {}])
    seen = _fake(monkeypatch, _prefers("A"))
    out = await compare_judge.judge_pairs(deadline=float("inf"))
    assert (out["pending"], out["selected"], out["judged"], out["split"], out["tokens_in"]) == (2, 2, 2, 0, 14)
    assert len(seen) == 2  # one request per order
    (first, _), (second, _) = seen
    assert '"A0"' in first["pairs"][0]["answer_1"] and '"A0"' in second["pairs"][0]["answer_2"]
    assert "x" not in first["pairs"][0] and set(first["pairs"][0]) >= {"capability", "input", "truncated"}
    rows = await _all()
    assert (rows["c-0"].jev_choice, rows["c-0"].jev_orders) == ("a", "a,a")
    assert rows["c-0"].jev_version == compare_rubric.VERSION and rows["c-0"].jev_judged_at is not None
    # The agent's vote is never touched.
    assert (rows["c-0"].choice, rows["c-0"].status, rows["c-0"].reason) == ("b", "decided", "agent")
    assert (rows["c-1"].choice, rows["c-1"].status) == (None, "ready")
    assert (await compare_judge.judge_pairs(deadline=float("inf")))["pending"] == 0


async def test_orders_that_disagree_are_split_not_tie(clients, monkeypatch):
    await _pairs(clients, [{}])
    _fake(monkeypatch, lambda pair: {"choice": "1"})  # always the first shown: an order effect
    out = await compare_judge.judge_pairs(deadline=float("inf"))
    assert out["split"] == 1
    row = (await _all())["c-0"]
    assert (row.jev_choice, row.jev_orders) == ("split", "a,b")


async def test_decided_pairs_come_first_and_only_judgeable_ones_are_read(clients, monkeypatch):
    await _pairs(clients, [{}, {"status": "decided", "choice": "a"}, {"capability": "people.search"},
                           {"status": "failed", "a_body": "", "b_body": ""}, {"status": "pending"}])
    monkeypatch.setattr(get_settings(), "compare_judge_per_run", 1, raising=False)
    _fake(monkeypatch, lambda pair: {"choice": "tie"})
    out = await compare_judge.judge_pairs(deadline=float("inf"))
    assert (out["pending"], out["judged"]) == (2, 1)
    rows = await _all()
    assert rows["c-1"].jev_choice == "tie" and rows["c-0"].jev_choice is None  # calibration first
    assert rows["c-2"].jev_version is None  # people.search is not judged by reading


async def test_stored_cuts_are_named_in_the_order_shown(clients, monkeypatch):
    await _pairs(clients, [{"truncated": "input,b"}])
    seen = _fake(monkeypatch, lambda pair: {"choice": "tie"})
    await compare_judge.judge_pairs(deadline=float("inf"))
    assert [s["pairs"][0]["truncated"] for s, _ in seen] == [["input", "answer_2"], ["input", "answer_1"]]


async def test_a_pair_jev_cannot_judge_steps_aside(clients, monkeypatch):
    await _pairs(clients, [{"call_ref": "bad"}, {}])
    _fake(monkeypatch, lambda pair: {"choice": "maybe"} if '"A0"' in pair["answer_1"] + pair["answer_2"]
          else {"choice": "both_bad"})
    out = await compare_judge.judge_pairs(deadline=float("inf"))
    assert (out["unreadable"], out["retry_later"], out["judged"]) == (1, 1, 1)
    rows = await _all()
    assert rows["bad"].jev_attempts == 1 and rows["bad"].jev_retry_at > utcnow_naive()
    assert rows["c-1"].jev_choice == "both_bad"
    assert (await compare_judge.judge_pairs(deadline=float("inf")))["selected"] == 0  # waiting its turn


async def test_a_failed_request_waits_an_hour_without_counting_a_try(clients, monkeypatch):
    await _pairs(clients, [{}])
    _fake(monkeypatch, Reply(answers=None, ms=1, error="timeout"))
    out = await compare_judge.judge_pairs(deadline=float("inf"))
    assert (out["failed_requests"], out["retry_later"]) == (1, 1)
    row = (await _all())["c-0"]
    assert not row.jev_attempts and row.jev_retry_at is not None and row.jev_choice is None


class _OneOrderFails:
    """A-first answers, B-first fails: the answered side's tokens still count."""
    async def reply(self, state, timeout_s):
        if '"A' in state["pairs"][0]["answer_1"]:
            return Reply(answers={"c0": {"choice": "1"}}, ms=1, tokens_in=7)
        return Reply(answers=None, ms=1, error="http_500")


async def test_tokens_of_the_order_that_answered_are_counted(clients, monkeypatch):
    await _pairs(clients, [{}])
    _fake(monkeypatch, _OneOrderFails())
    out = await compare_judge.judge_pairs(deadline=float("inf"))
    assert (out["failed_requests"], out["tokens_in"], out["judged"]) == (1, 7, 0)


class _SlowerThanTheRun:
    """Each request runs past the timeout it was given, like an HTTP timeout at the deadline."""
    def __init__(self):
        self.timeouts = []

    async def reply(self, state, timeout_s):
        self.timeouts.append(timeout_s)
        await asyncio.sleep(timeout_s + 0.01)
        return Reply(answers=None, ms=1, error="timeout")


async def test_the_deadline_bounds_each_request_and_stops_without_a_failure(clients, monkeypatch):
    await _pairs(clients, [{}, {}])
    monkeypatch.setattr(get_settings(), "compare_judge_batch", 1)
    slow = _SlowerThanTheRun()
    _fake(monkeypatch, slow)
    out = await compare_judge.judge_pairs(deadline=time.monotonic() + 0.05)
    assert out["stopped_by_deadline"] and (out["failed_requests"], out["retry_later"]) == (0, 0)
    assert len(slow.timeouts) == 2 and max(slow.timeouts) <= 0.05  # one batch, both orders
    assert all(r.jev_retry_at is None and not r.jev_attempts for r in (await _all()).values())


async def test_a_new_rubric_judges_again_and_never_over_a_newer_one(clients, monkeypatch):
    await _pairs(clients, [{"jev_choice": "b", "jev_version": "2000-01-01"},
                           {"jev_choice": "b", "jev_version": "2099-01-01"}])
    _fake(monkeypatch, _prefers("A"))
    assert (await compare_judge.judge_pairs(deadline=float("inf")))["judged"] == 1
    rows = await _all()
    assert rows["c-0"].jev_choice == "a" and rows["c-1"].jev_choice == "b"


async def test_the_comparison_pass_can_be_switched_off(clients, monkeypatch):
    await _pairs(clients, [{}])
    _fake(monkeypatch, _prefers("A"))
    for name in ("review_judge_answers", "review_judge_praise"):
        monkeypatch.setattr(get_settings(), name, False, raising=False)
    monkeypatch.setattr(get_settings(), "compare_judge", False, raising=False)
    assert "comparisons" not in await review_judge.run(max_seconds=5)
    monkeypatch.setattr(get_settings(), "compare_judge", True, raising=False)
    assert (await review_judge.run(max_seconds=5))["comparisons"]["judged"] == 1


def test_shown_choices_map_back_to_a_and_b():
    assert compare_rubric.combine("1", "2") == ("a", "a,a")
    assert compare_rubric.combine("2", "1") == ("b", "b,b")
    assert compare_rubric.combine("tie", "tie") == ("tie", "tie,tie")
    assert compare_rubric.combine("both_bad", "1") == ("split", "both_bad,b")
    assert compare_rubric.choice_from({"choice": "a"}) is None
