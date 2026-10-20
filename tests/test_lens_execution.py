"""Lens execution contracts with synthetic transport; never contact paid providers."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlmodel import select

from treg.application import lens_execution as app
from treg.application.call import service
from treg.application.call.types import CallerSnapshot, UpstreamResponse, ReservationFailed
from treg.config import get_settings
from treg.domain import lens as rules
from treg.domain.governance.teams import delete_membership
from treg.domain.identity.access import Caller
from treg.infra.db import session_maker
from treg.models import Membership, Org, User

PROFILE = {"brand": "Acme", "url": "https://acme.example/", "competitors": [{"name": "Rival"}],
           "questions": ["Best tools for teams?"], "engines": list(rules.ENGINES)}
RESEARCH = {"answer": {"brand": "Acme", "description": "Work planning", "competitors": [
    {"name": "Rival", "domain": "rival.example", "reason": "Planning for teams"}],
    "questions": ["Best tools for teams?"]}, "citations": [{"url": "https://acme.example/", "title": "Acme"}],
    "costDollars": {"total": 0.005}}


def test_profile_normalization_preserves_only_reviewed_fields():
    profile = rules.normalize_project({**PROFILE, "url": "Acme.Example/products?token=private#top",
        "competitors": [{"name": "Acme"}, {"name": "Rival", "domain": "https://rival.example/x"}, {"name": "RIVAL"}],
        "questions": ["A question?", "a QUESTION?"], "token": "not-stored", "endpoint_id": "unsafe"})
    assert profile == {"brand": "Acme", "url": "https://acme.example/products", "description": "",
        "competitors": [{"name": "Rival", "domain": "rival.example"}], "questions": ["A question?"],
        "engines": list(rules.ENGINES)}
    assert rules.normalize_project({**PROFILE, "questions": []})["questions"] == []
    with pytest.raises(rules.LensError):
        rules.normalize_input("audit", {**PROFILE, "questions": []})


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://localhost", "http://127.0.0.1", "http://127.1",
    "http://2130706433", "http://[::1]", "https://user:pass@acme.example", "https://acme.example:22", "https://foo.local",
    "https://10.0.0.1", "https://169.254.169.254", "https://acme.example\\@localhost", "https://bad%2eexample.com"])
def test_research_rejects_private_or_ambiguous_addresses(url):
    with pytest.raises(rules.LensError):
        rules.normalize_input("onboarding", {"url": url})


@pytest.mark.parametrize("change", [{"brand": "x" * 101}, {"questions": ["q"] * 9}, {"questions": ["x" * 501]},
    {"engines": ["unknown"]}, {"competitors": [{"name": "x"}] * 9}, {"description": "x" * 1201}])
def test_profile_bounds(change):
    with pytest.raises(rules.LensError):
        rules.normalize_project({**PROFILE, **change})


def test_research_normalizes_evidence_without_inventing_missing_fields():
    result = rules.normalize_research({"answer": json.dumps({"brand": "<b>Acme</b>", "description": "A\x00 description",
        "competitors": [{"name": "Acme"}, {"name": "Rival", "domain": "http://127.0.0.1", "reason": "<b>Choice</b>"},
            {"name": "RIVAL"}, {"name": "Own", "domain": "acme.example"}],
        "questions": ["Question?", "question?", "", 123]}),
        "citations": [{"url": "javascript:alert(1)"}, {"url": "http://localhost/"},
            {"url": "https://rival.example/a?utm_source=abc#top", "title": "<b>Source</b>"}]}, "https://acme.example/")
    assert result["brand"] == "Acme"
    assert result["competitors"] == [{"name": "Rival", "reason": "Choice"}]
    assert result["questions"] == ["Question?"]
    assert result["sources"] == [{"url": "https://rival.example/a", "title": "Source"}]
    assert rules.normalize_research({"answer": "Unstructured guess"}, "https://acme.example/")["brand"] == ""


def test_audit_mentions_use_word_boundaries_and_explicit_ranks():
    project = rules.normalize_project(PROFILE)
    result = rules.normalize_answer({"result": {"markdown": "1. Rival\n5. **Acme** is another option. Acmeology is different.",
        "sources": [{"url": "https://acme.example/?utm_source=test"}, {"url": "javascript:alert(1)"}]}},
        project, "Best?", "chatgpt")
    assert result["brand_mentioned"] and result["position"] == 5
    assert result["mentioned_brands"] == ["Rival", "Acme"]
    assert result["citations"][0]["url"] == "https://acme.example/"
    result = rules.normalize_answer({"answer": "Acmeology and a linked [site](https://acme.example/)"}, project, "Best?", "gemini")
    assert result["brand_mentioned"] is False and result["position"] is None
    with pytest.raises(rules.LensError, match="no answer"):
        rules.normalize_answer({"answer": ""}, project, "Best?", "google")


def test_dataforseo_sections_preserve_answer_and_citations():
    result = rules.normalize_answer({"tasks": [{"status_code": 20000, "result": [{"items": [{"sections": [
        {"text": "Acme is a choice.", "annotations": [{"url": "https://acme.example/"}]}, {"text": "Compare Rival too."}]}]}]}]},
        rules.normalize_project(PROFILE), "Best?", "perplexity")
    assert result["answer"] == "Acme is a choice.\n\nCompare Rival too."
    assert result["source_kind"] == "model-api" and result["provider"] == "dataforseo"
    assert len(result["citations"]) == 1


def test_malformed_optional_provider_fields_do_not_destroy_a_paid_answer():
    result = rules.normalize_answer({"answer": "Acme is useful.", "citations": {"untrusted": "shape"},
        "sources": 42, "entities": [{"name": "No", "type": 7}, None]}, rules.normalize_project(PROFILE), "Best?", "chatgpt")
    assert result["brand_mentioned"] and not result["citations"]
    with pytest.raises(rules.LensError, match="no answer"):
        rules.normalize_answer({"tasks": [{"status_code": 20000, "result": [{"items": {"bad": "shape"}}]}]},
            rules.normalize_project(PROFILE), "Best?", "perplexity")


def test_large_answers_remain_bounded_and_label_truncation():
    answer = rules.normalize_answer({"answer": "Acme " + "🧑" * 100_000}, rules.normalize_project(PROFILE), "Best?", "chatgpt")
    answer = app._bound_answer(answer)
    assert answer["truncated"] is True
    assert len(json.dumps(answer).encode()) <= 40 * 1024


async def caller_for(clients):
    async with session_maker() as db:
        user = (await db.execute(select(User).where(User.email == "tim@superdesign.dev"))).scalar_one()
        member = (await db.execute(select(Membership).where(Membership.user_id == user.id))).scalar_one()
        org = await db.get(Org, member.org_id)
        return CallerSnapshot.capture(Caller(member, user, org, None))


@pytest.fixture
async def isolated(clients, monkeypatch):
    caller = await caller_for(clients)
    async def fresh(value):
        return app._snapshot(value)
    async def priced(value, task, cat):
        ep = cat.by_id[task["endpoint_id"]]
        return {**task, "method": "POST", "tier": "platform", "estimate_micro": 5000,
                "endpoint_hash": app._hash(ep), "price_hash": "synthetic"}
    monkeypatch.setattr(app, "_fresh_caller", fresh)
    monkeypatch.setattr(app, "_price_task", priced)
    return caller


def response_for(doc, *, status=200, cost=5000):
    raw = json.dumps(doc).encode()
    async def stream():
        yield raw
    async def close():
        pass
    return UpstreamResponse(status=status, raw_headers=(() if cost is None else ((b"x-treg-cost-micro", str(cost).encode()),)),
                            body_stream=stream(), close=close)


class JsonStream(httpx.AsyncByteStream):
    def __init__(self, document):
        self.content = json.dumps(document).encode()

    async def __aiter__(self):
        yield self.content


async def test_quote_freezes_exact_tasks_and_makes_no_paid_calls(isolated, monkeypatch):
    async def forbidden(*args):
        pytest.fail("Quote must not execute a call")
    monkeypatch.setattr(service, "execute_call", forbidden)
    plan = await app.quote(isolated, "audit", PROFILE)
    assert plan["estimate_micro"] == 20_000
    assert len(plan["tasks"]) == 4
    assert plan["input"]["brand"] == "Acme"
    dfs = next(t for t in plan["tasks"] if t["engine"] == "perplexity")
    assert dfs["body"] == [{"user_prompt": "Best tools for teams?", "model_name": "sonar", "web_search_country_iso_code": "US"}]
    assert len(json.dumps(plan)) < 20_000


async def test_execute_persists_before_submit_and_uses_stable_native_call_ids(isolated, monkeypatch):
    plan = await app.quote(isolated, "onboarding", {"url": "acme.example"})
    saved, headers = [], []
    async def persist(value):
        saved.append(value)
    async def fake(context, http):
        assert saved[-1]["attempts"][0]["state"] == "running"
        assert saved[-1]["attempts"][0]["call_ref"] == context.call_ref
        assert context.input.catalog_only
        assert context.input.caller.org.platform_overflow_disabled
        headers.append(dict(context.input.raw_headers))
        return response_for(RESEARCH)
    monkeypatch.setattr(service, "execute_call", fake)
    result = await app.execute(isolated, plan, "claimed-id", None, "127.0.0.1", persist=persist)
    assert result["research"]["brand"] == "Acme"
    assert result["total_cost_micro"] == 5000 and result["cost_known"]
    assert saved[-1]["attempts"][0]["state"] == "completed"
    await app.execute(isolated, plan, "claimed-id", None, "127.0.0.1", persist=persist)
    assert headers[0][b"idempotency-key"] == headers[1][b"idempotency-key"]
    assert headers[0][b"x-treg-route-max-cost"] == b"0.005000"
    assert not any(b"token" in k or b"authorization" in k for k in headers[0])


@pytest.mark.parametrize("change", ["expired", "body", "endpoint", "price", "org"])
async def test_changed_or_expired_plan_never_submits(isolated, monkeypatch, change):
    plan = await app.quote(isolated, "onboarding", {"url": "acme.example"})
    if change == "expired":
        plan["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    elif change == "body":
        plan["tasks"][0]["body"]["stream"] = True
    elif change == "endpoint":
        plan["tasks"][0]["endpoint_id"] = "unapproved.endpoint"
    elif change == "price":
        plan["estimate_micro"] = 1
    else:
        plan["org_id"] += 1
    async def forbidden(*args):
        pytest.fail("Invalid plan must not submit")
    monkeypatch.setattr(service, "execute_call", forbidden)
    with pytest.raises(rules.LensError) as exc:
        await app.execute(isolated, plan, "claimed-id", None, "")
    assert exc.value.status_code == 409


@pytest.mark.parametrize("status,cost,doc", [(202, 5000, RESEARCH), (200, None, RESEARCH), (200, 5000, {})])
async def test_unknown_and_partial_research_billing_is_honest(isolated, monkeypatch, status, cost, doc):
    plan = await app.quote(isolated, "onboarding", {"url": "acme.example"})
    async def fake(context, http):
        return response_for(doc, status=status, cost=cost)
    monkeypatch.setattr(service, "execute_call", fake)
    result = await app.execute(isolated, plan, "claimed-id", None, "")
    known = status != 202 and cost is not None
    assert result["cost_known"] is known
    assert result["total_cost_micro"] == (cost if known else None)
    if status == 202 or not doc:
        assert result["errors"] and not result["results"]
    if not doc:
        assert result["research"]["brand"] == ""


async def test_unknown_charge_stops_later_waves_without_retry(isolated, monkeypatch):
    plan = await app.quote(isolated, "audit", {**PROFILE, "questions": ["One?", "Two?"]})
    calls = []
    async def fake(context, http):
        calls.append(context.call_ref)
        if context.input.raw_rest == rules.ENDPOINTS["chatgpt"]:
            raise TimeoutError
        return response_for({"answer": "Acme is useful."})
    monkeypatch.setattr(service, "execute_call", fake)
    result = await app.execute(isolated, plan, "claimed-id", None, "")
    assert 1 <= len(calls) <= 4
    assert result["total_cost_micro"] is None
    assert all(a["state"] == "not_attempted" for a in result["attempts"][4:])
    assert sum(a["state"] == "unknown" for a in result["attempts"]) == 1


async def test_usable_answer_with_unknown_charge_stops_later_waves(isolated, monkeypatch):
    plan = await app.quote(isolated, "audit", {**PROFILE, "questions": ["One?", "Two?"]})
    calls = []
    async def fake(context, http):
        calls.append(context.call_ref)
        return response_for({"answer": "Acme is useful."}, cost=None)
    monkeypatch.setattr(service, "execute_call", fake)
    result = await app.execute(isolated, plan, "claimed-id", None, "")
    assert result["results"] and result["total_cost_micro"] is None
    assert len(calls) <= 4
    assert all(a["state"] == "not_attempted" for a in result["attempts"][4:])


async def test_persistence_refusal_never_submits(isolated, monkeypatch):
    plan = await app.quote(isolated, "onboarding", {"url": "acme.example"})
    async def refuse(value):
        return False
    async def forbidden(*args):
        pytest.fail("Unacknowledged running marker must not submit")
    monkeypatch.setattr(service, "execute_call", forbidden)
    result = await app.execute(isolated, plan, "claimed-id", None, "", persist=refuse)
    assert result["attempts"][0]["state"] == "not_attempted"
    assert result["total_cost_micro"] == 0


async def test_call_reservation_failure_reports_known_zero_without_exception_details(isolated, monkeypatch):
    plan = await app.quote(isolated, "onboarding", {"url": "acme.example"})
    async def refuse(context, http):
        raise ReservationFailed("insufficient_balance", status_code=402, detail="private-account-secret")
    monkeypatch.setattr(service, "execute_call", refuse)
    result = await app.execute(isolated, plan, "claimed-id", None, "")
    assert result["total_cost_micro"] == 0
    assert "private-account-secret" not in json.dumps(result)
    assert "billing" in result["errors"][0]["detail"]


async def test_real_quote_and_execution_use_native_platform_settlement(clients, monkeypatch):
    # A quote must not spend the sole daily-call slot; only the native execution consumes it.
    async with session_maker() as db:
        member = (await db.execute(select(Membership))).scalars().one()
        member.daily_call_cap = 1
        db.add(member)
        await db.commit()
    caller = await caller_for(clients)
    settings = get_settings()
    monkeypatch.setattr(settings, "platform_key_exa", "SYNTHETIC-EXA-KEY")
    monkeypatch.setattr(settings, "platform_providers", "exa")
    monkeypatch.setattr(settings, "platform_margin", 0)
    seen = []
    async def transport(request):
        seen.append(request)
        assert request.url.host == "api.exa.ai"
        assert request.headers["x-api-key"] == "SYNTHETIC-EXA-KEY"
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=JsonStream(RESEARCH))
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        plan = await app.quote(caller, "onboarding", {"url": "acme.example"})
        assert plan["estimate_micro"] == 5000 and not seen
        async with session_maker() as db:
            assert (await db.get(Membership, caller.membership.id)).calls_today == 0
        result = await app.execute(caller, plan, "native-settle", http, "127.0.0.1")
        async with session_maker() as db:
            member = await db.get(Membership, caller.membership.id)
            assert member.calls_today == 1
            # Replay still obeys current member caps, so lift this test's one-call cap before
            # verifying the native idempotent replay never reaches the synthetic provider twice.
            member.daily_call_cap = -1
            db.add(member)
            await db.commit()
        replay = await app.execute(caller, plan, "native-settle", http, "127.0.0.1")
    assert len(seen) == 1
    assert result["total_cost_micro"] == 5000, result
    assert result["research"]["brand"] == "Acme"
    assert not replay["errors"], replay
    assert replay["research"]["brand"] == "Acme"
    async with session_maker() as db:
        assert (await db.get(Membership, caller.membership.id)).calls_today == 1


async def test_native_own_key_wins_and_is_unmetered(clients, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "platform_key_exa", "SYNTHETIC-PLATFORM-KEY")
    monkeypatch.setattr(settings, "platform_providers", "exa")
    secret = await clients.post("/secrets", json={"name": "exa", "value": "SYNTHETIC-OWN-KEY"})
    assert secret.status_code == 200
    caller = await caller_for(clients)
    async def transport(request):
        assert request.headers["x-api-key"] == "SYNTHETIC-OWN-KEY"
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=JsonStream(RESEARCH))
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        plan = await app.quote(caller, "onboarding", {"url": "acme.example"})
        assert plan["estimate_micro"] == 0
        assert plan["tasks"][0]["tier"] == "credential"
        result = await app.execute(caller, plan, "native-byok", http, "")
    assert result["total_cost_micro"] == 0
    assert result["research"]["brand"] == "Acme"


async def test_revoked_membership_and_scoped_accounts_refuse_execution(clients):
    caller = await caller_for(clients)
    for membership in (replace(caller.membership, role="viewer"), replace(caller.membership, tool_access=[]),
                       replace(caller.membership, project_access=[]), replace(caller.membership, pinned_tags={"agent": "one"})):
        with pytest.raises(rules.LensError) as exc:
            app._require_access(replace(caller, membership=membership))
        assert exc.value.status_code == 403
    async with session_maker() as db:
        await delete_membership(db, await db.get(Membership, caller.membership.id),
                                actor_email=caller.email)
        await db.commit()
    with pytest.raises(rules.LensError, match="access"):
        await app._fresh_caller(caller)
