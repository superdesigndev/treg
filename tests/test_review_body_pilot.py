"""The body pilot: negative reviews coded again with the call's answer in view, compared with the
reason-only codes, writing nothing; and the shortened answer the judge reads."""
import json

from sqlmodel import select

from treg import archive
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


async def _seed(clients, rows):
    org = (await clients.post("/orgs", json={"name": "Pilot"})).json()["org_id"]
    async with session_maker() as db:
        for i, (code, stored) in enumerate(rows):
            db.add(CallReview(org_id=org, user_email="a@example.dev", call_id=f"p-{i}",
                              endpoint_id="apollo.people.search", usefulness="partly",
                              reason="Half of the requested fields came back empty for these people.",
                              primary_code=code, codebook_version=V))
            db.add(CallRecord(org_id=org, user_email="a@example.dev", tool_name="apollo.people.search",
                              method="POST", path="/call/apollo.people.search", status_code=200,
                              call_ref=f"p-{i}", archive_key_hash=f"k{i}",
                              archive_content_hash=f"c{i}" if stored is not None else None))
        await db.commit()
    return org


async def test_pilot_compares_codes_and_writes_nothing(clients, monkeypatch):
    # p-0 and p-1 have answers on file, p-2's answer is gone, p-3 has no archive link.
    await _seed(clients, [("PARTIAL", True), ("WRONG", True), ("EMPTY", False), ("ERROR", None)])

    async def resolve(key_hash, content_hash):
        if key_hash == "k2":
            return None
        return {"stored": True, "request": {"method": "POST", "url": "https://x.test/s", "body_text": "{}"},
                "response": {"status_code": 200, "media_type": "application/json", "size_bytes": 99,
                             "body_text": json.dumps({"people": [{"email": None}] * 30})}}
    seen = []

    async def ask(state, questions):
        seen.append(state)
        return Reply(answers={q: {"choice": "PARTIAL", "probabilities": {"PARTIAL": 0.8}} for q in questions},
                     ms=1, tokens_in=50)
    monkeypatch.setattr(archive, "resolve_result", resolve)
    monkeypatch.setattr(review_body_pilot, "_ask", ask)
    monkeypatch.setattr(get_settings(), "typesafe_api_key", "test-key", raising=False)

    out = await review_body_pilot.run(sample=10, batch=5, budget=500, max_seconds=60)
    assert out["candidates"] == 3 and out["no_body"] == 1 and out["coded"] == 2
    assert out["same_primary"] == 1 and out["pairs"] == {"WRONG->PARTIAL": 1}
    assert out["tokens_in"] == 50 and len(seen) == 1
    sent = seen[0]["reviews"]
    assert [r["i"] for r in sent] == [0, 1] and all(len(r["response"]["body"]) <= 500 for r in sent)
    assert [d["review_id"] for d in out["diffs"]] and "fields came back" not in json.dumps(out)
    async with session_maker() as db:
        codes = {r.call_id: r.primary_code for r in (await db.execute(select(CallReview))).scalars()}
    assert codes == {"p-0": "PARTIAL", "p-1": "WRONG", "p-2": "EMPTY", "p-3": "ERROR"}


async def test_pilot_is_off_without_a_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "typesafe_api_key", "", raising=False)
    assert await review_body_pilot.run(sample=5, batch=5, budget=500, max_seconds=5) == {
        "skipped": "no_judge_key"}
    assert review_judge.configured() is False
