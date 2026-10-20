"""Native Lens HTTP contracts: real identity, team ownership, durable claims and call billing."""
from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlmodel import select

from treg.application import lens
from treg.application.call import service
from treg.config import get_settings
from treg.infra.db import session_maker
from treg.models import Hold, LedgerEntry, LensProject, LensRun, Membership
from treg.timeutil import utcnow_naive
from lens_upstream import LensUpstream, response as upstream_response
from test_marketplace_call import _balance
from test_orgs_isolation import _make_org, _mint


PROFILE = {
    "brand": "Acme",
    "url": "https://acme.example/",
    "description": "Project planning for small teams.",
    "competitors": [{"name": "Rival", "domain": "rival.example"}],
    "questions": ["Which project planning tools suit small teams?"],
    "engines": ["chatgpt"],
}


@pytest.fixture(autouse=True)
async def lens_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "lens_enabled", True)
    yield
    await lens.shutdown()


@pytest.fixture
def providers(monkeypatch):
    for provider in ("EXA", "CLORO", "DATAFORSEO"):
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{provider}", f"SYNTHETIC-LENS-{provider}")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "exa,cloro,dataforseo")
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "lens_enabled", True)
    upstream = LensUpstream()
    monkeypatch.setattr(service, "relay", upstream)
    yield upstream
    get_settings.cache_clear()


async def create(c, **changes):
    response = await c.post("/lens/projects", json={
        **PROFILE, "request_id": uuid.uuid4().hex, **changes,
    })
    assert response.status_code == 200, response.text
    return response.json()


async def org_id(project_id):
    async with session_maker() as db:
        return (await db.get(LensProject, project_id)).org_id


async def quote(c, project=None, **changes):
    body = ({"kind": "audit", "project_id": project["id"], "project_revision": project["revision"]}
            if project else {"kind": "onboarding", "url": "acme.example"})
    response = await c.post("/lens/quotes", json={**body, **changes})
    assert response.status_code == 200, response.text
    return response.json()


async def finish(c, plan):
    response = await c.post("/lens/runs", json={"quote_id": plan["id"]})
    assert response.status_code == 200, response.text
    task = lens._owners.get(plan["id"])
    if task:
        await task
    response = await c.get(f"/lens/runs/{plan['id']}")
    assert response.status_code == 200, response.text
    return response.json()


async def test_lens_is_disabled_by_default_and_requires_identity(clients, monkeypatch):
    monkeypatch.setattr(get_settings(), "lens_enabled", False)
    assert (await clients.get("/lens/projects")).status_code == 404
    monkeypatch.setattr(get_settings(), "lens_enabled", True)
    saved = clients.headers.pop("X-Treg-Token")
    try:
        assert (await clients.get("/lens/projects")).status_code == 401
    finally:
        clients.headers["X-Treg-Token"] = saved


async def test_free_project_save_is_repeat_safe_and_revision_checked(clients):
    request_id = uuid.uuid4().hex
    first = await create(clients, request_id=request_id)
    replay = await create(clients, request_id=request_id)
    assert replay["id"] == first["id"]
    assert first["revision"] == 1 and first["can_edit"] is True
    assert (await clients.get("/lens/projects")).json()["projects"] == [first]
    assert (await clients.get(f"/lens/projects/{first['id']}")).json()["runs"] == []
    conflict = await clients.post("/lens/projects", json={
        **PROFILE, "brand": "Different", "request_id": request_id,
    })
    assert conflict.status_code == 409
    changed = await clients.put(f"/lens/projects/{first['id']}", json={
        **PROFILE, "brand": "Acme New", "revision": first["revision"],
    })
    assert changed.status_code == 200
    assert changed.json()["revision"] == 2
    stale = await clients.put(f"/lens/projects/{first['id']}", json={
        **PROFILE, "revision": first["revision"],
    })
    assert stale.status_code == 409
    fresh = (await clients.get(f"/lens/projects/{first['id']}")).json()["project"]
    assert fresh["brand"] == "Acme New" and fresh["revision"] == 2
    async with session_maker() as db:
        row = await db.get(LensProject, first["id"])
        assert "Acme" not in row.payload and "acme.example" not in row.payload


async def test_concurrent_profile_edits_have_one_winner(clients):
    first = await create(clients)
    replies = await asyncio.gather(*[
        clients.put(f"/lens/projects/{first['id']}", json={
            **PROFILE, "brand": brand, "revision": 1,
        }) for brand in ("First", "Second")
    ])
    assert sorted(response.status_code for response in replies) == [200, 409]
    assert (await clients.get(f"/lens/projects/{first['id']}")).json()["project"]["revision"] == 2


async def test_team_sharing_and_creator_admin_edit_permissions(clients):
    project = await create(clients)
    team = await org_id(project["id"])
    member = {"X-Treg-Token": await _mint("lens-member@example.com", team, "member")}
    viewer = {"X-Treg-Token": await _mint("lens-viewer@example.com", team, "viewer")}
    admin = {"X-Treg-Token": await _mint("lens-admin@example.com", team, "admin")}
    for headers in (member, viewer):
        read = await clients.get(f"/lens/projects/{project['id']}", headers=headers)
        assert read.status_code == 200 and read.json()["project"]["can_edit"] is False
        assert (await clients.put(f"/lens/projects/{project['id']}", headers=headers,
                                  json={**PROFILE, "revision": 1})).status_code == 403
    assert (await clients.post("/lens/projects", headers=viewer, json={
        **PROFILE, "request_id": uuid.uuid4().hex,
    })).status_code == 403
    assert (await clients.post("/lens/quotes", headers=viewer, json={
        "kind": "audit", "project_id": project["id"], "project_revision": 1,
    })).status_code == 403
    edited = await clients.put(f"/lens/projects/{project['id']}", headers=admin,
                               json={**PROFILE, "brand": "Admin updated", "revision": 1})
    assert edited.status_code == 200 and edited.json()["revision"] == 2


async def test_cross_team_project_ids_are_not_disclosed(clients):
    project = await create(clients)
    other = await _make_org("Other Lens team", "other-lens-team")
    headers = {"X-Treg-Token": await _mint("lens-other@example.com", other, "owner")}
    assert (await clients.get("/lens/projects", headers=headers)).json() == {"projects": []}
    assert (await clients.get(f"/lens/projects/{project['id']}", headers=headers)).status_code == 404
    assert (await clients.put(f"/lens/projects/{project['id']}", headers=headers,
                              json={**PROFILE, "revision": 1})).status_code == 404
    assert (await clients.post("/lens/quotes", headers=headers, json={
        "kind": "audit", "project_id": project["id"], "project_revision": 1,
    })).status_code == 404


@pytest.mark.parametrize("scope,value", [
    ("tool_access", []), ("project_access", []), ("pinned_tags", {"agent": "restricted"}),
])
async def test_restricted_memberships_cannot_read_team_lens_data(clients, scope, value):
    project = await create(clients)
    team = await org_id(project["id"])
    token = await _mint("lens-restricted@example.com", team, "member")
    async with session_maker() as db:
        membership = (await db.execute(select(Membership).where(
            Membership.org_id == team, Membership.role == "member"))).scalar_one()
        setattr(membership, scope, value)
        db.add(membership)
        await db.commit()
    response = await clients.get("/lens/projects", headers={"X-Treg-Token": token})
    assert response.status_code == 403


@pytest.mark.parametrize("url", [
    "http://localhost", "http://127.0.0.1", "http://169.254.169.254/latest",
    "http://10.1.1.1", "https://user:password@example.com", "file:///etc/passwd",
    "https://[::1]/", "https://service.local/", "https://example.com:8443",
])
async def test_private_or_credential_urls_are_rejected_before_research(clients, url):
    assert (await clients.post("/lens/projects", json={
        **PROFILE, "url": url, "request_id": uuid.uuid4().hex,
    })).status_code == 400
    assert (await clients.post("/lens/quotes", json={
        "kind": "onboarding", "url": url,
    })).status_code == 400
    async with session_maker() as db:
        assert (await db.execute(select(LensRun))).scalars().all() == []


async def test_cross_origin_mutations_are_rejected(clients):
    response = await clients.post("/lens/projects", headers={"Origin": "https://attacker.example"},
                                  json={**PROFILE, "request_id": uuid.uuid4().hex})
    assert response.status_code == 403
    assert (await clients.get("/lens/projects")).json() == {"projects": []}


async def test_profiles_remain_owned_by_team_after_membership_token_rotation(clients):
    project = await create(clients)
    team = await org_id(project["id"])
    keys = (await clients.get(f"/orgs/{team}/api-keys")).json()
    rotated = await clients.post(f"/orgs/{team}/api-keys/{keys[0]['id']}/rotate")
    assert rotated.status_code == 200, rotated.text
    assert (await clients.get("/lens/projects")).status_code == 401
    response = await clients.get(f"/lens/projects/{project['id']}",
                                headers={"X-Treg-Token": rotated.json()["secret"]})
    assert response.status_code == 200 and response.json()["project"]["id"] == project["id"]


async def test_url_research_requires_explicit_start_and_recovers_by_quote_id(clients, providers):
    before = await _balance(clients)
    plan = await quote(clients, url="https://acme.example/?utm_source=private#fragment")
    assert plan["id"] == plan["run_id"]
    assert plan["input"]["url"] == "https://acme.example/"
    assert type(plan["total_cost_micro"]) is int and plan["total_cost_micro"] > 0
    assert providers.seen == [] and await _balance(clients) == before
    pending = (await clients.get(f"/lens/runs/{plan['id']}")).json()
    assert pending["state"] == "quoted" and pending["result"] is None
    done = await finish(clients, plan)
    assert done["state"] == "completed", done
    result = done["result"]
    assert result["research"]["brand"] == "Acme"
    assert result["research"]["competitors"][0]["name"] == "Rival"
    assert result["research"]["questions"] == PROFILE["questions"]
    assert result["cost_known"] is True and result["total_cost_micro"] > 0
    assert before - await _balance(clients) == result["total_cost_micro"]
    assert len(providers.seen) == 1
    replay = await finish(clients, plan)
    assert replay == done and len(providers.seen) == 1
    assert (await clients.get("/lens/projects")).json() == {"projects": []}
    async with session_maker() as db:
        row = await db.get(LensRun, plan["id"])
        assert "Acme" not in row.payload and "SYNTHETIC" not in row.payload


async def test_four_engine_audit_saves_results_and_native_billing(clients, providers):
    project = await create(clients, engines=["chatgpt", "perplexity", "gemini", "google"])
    plan = await quote(clients, project)
    before = await _balance(clients)
    run = await finish(clients, plan)
    assert run["state"] == "completed", run
    result = run["result"]
    assert result["errors"] == [], result
    assert {row["engine"] for row in result["results"]} == set(project["engines"])
    assert all(row["brand_mentioned"] and row["position"] == 1 for row in result["results"])
    assert all(row["citations"][0]["domain"] == "acme.example" for row in result["results"])
    assert all(row["charged_micro"] >= 0 for row in result["results"])
    assert result["cost_known"] is True
    assert before - await _balance(clients) == result["total_cost_micro"]
    assert len(providers.seen) == 4
    assert all(call["method"] == "POST" for call in providers.seen)
    detail = (await clients.get(f"/lens/projects/{project['id']}")).json()
    assert detail["runs"][0]["id"] == run["id"]
    assert detail["runs"][0]["result"] == result
    async with session_maker() as db:
        holds = (await db.execute(select(Hold))).scalars().all()
        # Successful native settlement consumes its hold; no stranded reservation remains.
        assert holds == []
        assert (await db.execute(select(LedgerEntry))).scalars().all()


async def test_double_start_claims_one_run_and_replays_reads_only(clients, providers):
    plan = await quote(clients)
    replies = await asyncio.gather(*[
        clients.post("/lens/runs", json={"quote_id": plan["id"]}) for _ in range(3)
    ])
    assert all(reply.status_code == 200 for reply in replies)
    run = await finish(clients, plan)
    assert run["state"] == "completed" and len(providers.seen) == 1


async def test_changed_project_and_expired_quote_cannot_spend(clients, providers):
    project = await create(clients)
    plan = await quote(clients, project)
    assert (await clients.put(f"/lens/projects/{project['id']}", json={
        **PROFILE, "brand": "Changed", "revision": 1,
    })).status_code == 200
    stale = await clients.post("/lens/runs", json={"quote_id": plan["id"]})
    assert stale.status_code == 409 and providers.seen == []
    expired = await quote(clients)
    async with session_maker() as db:
        row = await db.get(LensRun, expired["id"])
        row.expires_at = utcnow_naive() - timedelta(seconds=1)
        db.add(row)
        await db.commit()
    result = await finish(clients, expired)
    assert result["state"] == "expired" and providers.seen == []


async def test_quoted_run_requires_its_creator_and_is_hidden_from_other_teams(clients, providers):
    project = await create(clients)
    plan = await quote(clients, project)
    team = await org_id(project["id"])
    member = {"X-Treg-Token": await _mint("lens-colleague@example.com", team, "member")}
    assert (await clients.get(f"/lens/runs/{plan['id']}", headers=member)).status_code == 200
    assert (await clients.post("/lens/runs", headers=member,
                              json={"quote_id": plan["id"]})).status_code == 403
    other = await _make_org("Other quote team", "other-quote-team")
    headers = {"X-Treg-Token": await _mint("lens-stranger@example.com", other, "owner")}
    assert (await clients.get(f"/lens/runs/{plan['id']}", headers=headers)).status_code == 404
    assert (await clients.post("/lens/runs", headers=headers,
                              json={"quote_id": plan["id"]})).status_code == 404
    assert providers.seen == []


async def test_lost_worker_run_is_interrupted_after_deadline_and_never_resubmitted(clients, providers):
    plan = await quote(clients)
    async with session_maker() as db:
        row = await db.get(LensRun, plan["id"])
        row.state = "running"
        row.deadline_at = utcnow_naive() + timedelta(seconds=60)
        db.add(row)
        await db.commit()
    # A different worker may still own the task: process-local absence is not proof of loss.
    assert (await clients.get(f"/lens/runs/{plan['id']}")).json()["state"] == "running"
    async with session_maker() as db:
        row = await db.get(LensRun, plan["id"])
        row.deadline_at = utcnow_naive() - timedelta(seconds=1)
        db.add(row)
        await db.commit()
    result = await finish(clients, plan)
    assert result["state"] == "interrupted"
    assert "interrupted" in result["error"] and providers.seen == []


async def test_failed_engine_preserves_other_answers_and_never_retries(clients, providers, monkeypatch):
    original = providers.__call__
    failed = []

    async def relay(request, upstream_url, *args, **kwargs):
        if "gemini" in upstream_url:
            failed.append(upstream_url)
            return upstream_response({"error": "SYNTHETIC-LENS-CLORO must not be exposed"}, status=503)
        return await original(request, upstream_url, *args, **kwargs)

    monkeypatch.setattr(service, "relay", relay)
    project = await create(clients, engines=["chatgpt", "gemini"])
    run = await finish(clients, await quote(clients, project))
    result = run["result"]
    assert [r["engine"] for r in result["results"]] == ["chatgpt"], run
    assert len(result["errors"]) == 1 and result["errors"][0]["engine"] == "gemini"
    assert "SYNTHETIC" not in str(run)
    assert len(failed) == 1 and len(providers.seen) == 1


async def test_quotes_never_use_daily_call_slots_or_money(clients, providers):
    project = await create(clients)
    team = await org_id(project["id"])
    async with session_maker() as db:
        member = (await db.execute(select(Membership).where(Membership.org_id == team))).scalar_one()
        member.daily_call_cap = 1
        member_id = member.id
        db.add(member)
        await db.commit()
    before = await _balance(clients)
    plans = [await quote(clients) for _ in range(3)]
    assert providers.seen == [] and await _balance(clients) == before
    async with session_maker() as db:
        assert (await db.get(Membership, member_id)).calls_today == 0
        assert (await db.execute(select(Hold))).scalars().all() == []
    first = await finish(clients, plans[0])
    assert first["state"] == "completed" and first["result"]["research"]["brand"] == "Acme"
    async with session_maker() as db:
        assert (await db.get(Membership, member_id)).calls_today == 1
    second = await finish(clients, plans[1])
    assert len(providers.seen) == 1
    assert second["result"]["errors"]
