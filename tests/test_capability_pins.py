"""Team policy on WHICH provider serves a job.

The agent chooses (see docs/CAPABILITY-CHOICE-PLAN.md) — a pin is where the team's decision stops
that freedom. These pin the parts that are judgement, not plumbing: that a pin is a GATE rather than
a hint, that a typo cannot silently block a real job, and that a refusal tells the caller what to do
instead.
"""

from __future__ import annotations

from httpx import AsyncClient

CAP = "tiktok.user.profile"
PINNED = "tikhub"
OTHER_EP = "justoneapi.tiktok.user.profile"


async def _org_id(c: AsyncClient) -> int:
    return (await c.get("/orgs")).json()[0]["org_id"]


async def test_a_pin_is_a_gate_not_a_hint(clients: AsyncClient):
    """A hint is honoured by a well-behaved agent and ignored by the one you needed to stop. The
    refusal also names the endpoint the team DOES use — told only "no", an agent tries the next
    provider and is refused again."""
    org = await _org_id(clients)
    r = await clients.post(f"/orgs/{org}/pins", json={"capability": CAP, "provider": PINNED})
    assert r.status_code == 200, r.text

    blocked = await clients.get(f"/call/{OTHER_EP}?uniqueId=tiktok")
    assert blocked.status_code == 403
    detail = blocked.json()["detail"]
    assert detail["error"] == "capability_pinned"
    assert detail["pinned_provider"] == PINNED
    assert detail["use_endpoint"].startswith(PINNED)      # "use this instead", not just "no"


async def test_the_suggested_endpoint_is_the_curated_one(clients: AsyncClient):
    """`core` is the curated route for a job; `extended` is the bulk-ingested long tail. Suggesting
    an obscure extended id when a core one exists reads as a broken suggestion."""
    org = await _org_id(clients)
    await clients.post(f"/orgs/{org}/pins", json={"capability": CAP, "provider": PINNED})
    detail = (await clients.get(f"/call/{OTHER_EP}?uniqueId=tiktok")).json()["detail"]
    assert detail["use_endpoint"] == "tikhub.tiktok.user.profile"


async def test_a_typo_fails_loudly_at_pin_time(clients: AsyncClient):
    """A pin naming a capability nobody serves, or a provider that does not serve it, would block
    every call to a job the team really uses — and it would be discovered at 3am in an agent's log."""
    org = await _org_id(clients)
    bad_cap = await clients.post(f"/orgs/{org}/pins",
                                 json={"capability": "not.a.capability", "provider": PINNED})
    assert bad_cap.status_code == 422

    bad_prov = await clients.post(f"/orgs/{org}/pins",
                                  json={"capability": CAP, "provider": "stripe"})
    assert bad_prov.status_code == 422
    assert "does not serve" in bad_prov.json()["detail"]
    assert PINNED in bad_prov.json()["detail"]          # ...and says who does


async def test_repinning_replaces_rather_than_stacking(clients: AsyncClient):
    org = await _org_id(clients)
    await clients.post(f"/orgs/{org}/pins", json={"capability": CAP, "provider": PINNED})
    await clients.post(f"/orgs/{org}/pins", json={"capability": CAP, "provider": "justoneapi"})
    pins = (await clients.get(f"/orgs/{org}/pins")).json()
    assert [p["provider"] for p in pins if p["capability"] == CAP] == ["justoneapi"]


async def test_unpinning_returns_the_choice_to_the_caller(clients: AsyncClient):
    org = await _org_id(clients)
    await clients.post(f"/orgs/{org}/pins", json={"capability": CAP, "provider": PINNED})
    assert (await clients.delete(f"/orgs/{org}/pins?capability={CAP}")).status_code == 200
    # the previously-blocked provider is no longer refused BY THE PIN (it may still need a credential)
    r = await clients.get(f"/call/{OTHER_EP}?uniqueId=tiktok")
    assert r.status_code != 403 or (r.json().get("detail") or {}).get("error") != "capability_pinned"


async def test_members_can_read_the_pins_they_must_obey(clients: AsyncClient):
    """An agent has to know what it may call; learning it by being refused is a wasted round-trip."""
    org = await _org_id(clients)
    await clients.post(f"/orgs/{org}/pins", json={"capability": CAP, "provider": PINNED})
    r = await clients.get(f"/orgs/{org}/pins")
    assert r.status_code == 200 and r.json()[0]["capability"] == CAP


async def test_a_pin_is_scoped_to_one_org(clients: AsyncClient):
    """Another team's decision must never refuse your call."""
    org = await _org_id(clients)
    await clients.post(f"/orgs/{org}/pins", json={"capability": CAP, "provider": PINNED})
    r = await clients.post("/users", json={"email": "stranger@elsewhere.dev"})
    other = {"X-Treg-Token": r.json()["token"]}
    assert (await clients.get(f"/call/{OTHER_EP}?uniqueId=tiktok", headers=other)).status_code != 403 \
        or "capability_pinned" not in (await clients.get(f"/call/{OTHER_EP}", headers=other)).text


async def test_a_pin_cannot_be_sidestepped_to_spend_TREGS_money(clients: AsyncClient):
    """The boundary, pinned so nobody later assumes a pin blocks a vendor everywhere.

    A pin gates the catalog ID, and a catalog id is the ONLY route to treg's own key — so the money
    we pay for cannot be reached around it. A raw upstream URL resolves against the org's own tools
    instead and 404s without one; if a team does hold its own key there, that is their credential and
    their bill, and `org deny --host` is the tool for that.
    """
    org = await _org_id(clients)
    await clients.post(f"/orgs/{org}/pins", json={"capability": CAP, "provider": PINNED})

    by_id = await clients.get(f"/call/{OTHER_EP}?uniqueId=x")
    assert (by_id.json()["detail"] or {}).get("error") == "capability_pinned"

    by_url = await clients.get("/call/https://api.justoneapi.com/api/tiktok/get-user-detail/v1")
    assert by_url.status_code == 404                      # no org tool → treg's key is unreachable
    assert "no registered tool" in by_url.text


async def test_a_capability_can_never_restructure_a_url(clients: AsyncClient):
    """The bug this route was born with, and why the value is a query parameter now.

    As `/orgs/{id}/pins/{capability}`, `treg org unpin ..` DELETED THE TEAM: every normalizing HTTP
    client (httpx included) rewrites `/orgs/1/pins/..` to `/orgs/1` — the delete-org route — before
    the request leaves the process. Server-side validation cannot see it, because the rewrite happens
    in the client. Two independent defences now hold: the value travels as a query parameter, and
    delete-org itself refuses to run without the slug it is about to destroy.
    """
    org = await _org_id(clients)
    slug = next(o["slug"] for o in (await clients.get("/orgs")).json() if o["org_id"] == org)

    hit = await clients.delete(f"/orgs/{org}/pins/..")     # normalizes to DELETE /orgs/{org}
    assert hit.status_code == 422, "delete-org must refuse a request that does not name the team"
    assert (await clients.get("/orgs")).status_code == 200, "the team must still exist"

    assert (await clients.delete(f"/orgs/{org}?confirm=not-the-slug")).status_code == 422
    assert (await clients.get("/orgs")).json()                      # still there


async def test_two_admins_pinning_at_once_leave_one_row(clients: AsyncClient):
    """Same shape as the double-credit bug found in #45: SELECT-then-INSERT with no unique index.
    Two admins (or one admin against two web workers) would write two rows, and enforcement reading
    them with scalar_one_or_none() would raise MultipleResultsFound — a 500 on EVERY call to that
    capability, caused by a policy row. The database is what says no."""
    import asyncio
    from sqlmodel import select
    from treg.infra.db import session_maker
    from treg.models import CapabilityPin

    org = await _org_id(clients)

    async def pin(provider):
        return await clients.post(f"/orgs/{org}/pins",
                                  json={"capability": CAP, "provider": provider})

    results = await asyncio.gather(*[pin(PINNED) for _ in range(5)], return_exceptions=True)
    assert all(getattr(r, "status_code", None) == 200 for r in results), results

    async with session_maker() as db:
        rows = (await db.execute(select(CapabilityPin).where(
            CapabilityPin.org_id == org, CapabilityPin.capability == CAP))).scalars().all()
    assert len(rows) == 1, f"{len(rows)} rows — the unique index did not hold"

    # and enforcement still answers rather than 500ing
    blocked = await clients.get(f"/call/{OTHER_EP}?uniqueId=x")
    assert blocked.status_code == 403


# NOTE: there is no test for "unpin clears a legacy duplicate", because the unique index now makes
# that state unreachable — an attempt to forge one is rejected by the database, which is the point.
# `clear_capability_pin` still deletes ALL matching rows rather than one, as cheap insurance for a
# database written before the index existed.
