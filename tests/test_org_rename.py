"""PATCH /orgs/{id}: change a team's name and slug; the retired slug stays an alias."""
from httpx import AsyncClient

from tests.test_orgs_mgmt import _h, _register, _team_with_member, c  # noqa: F401  (fixture)


async def test_admin_renames_name_and_slug_member_cannot(c: AsyncClient):
    org_id, otok, mtok, *_ = await _team_with_member(c)
    r = await c.patch(f"/orgs/{org_id}", headers=_h(mtok), json={"name": "Nope"})
    assert r.status_code == 403
    r = await c.patch(f"/orgs/{org_id}", headers=_h(otok), json={"name": "  Team B  "})
    assert r.status_code == 200, r.text
    assert r.json() == {"org_id": org_id, "org": "team-a", "previous_slug": None, "name": "Team B"}
    r = await c.patch(f"/orgs/{org_id}", headers=_h(otok), json={"slug": "team-b"})
    assert r.status_code == 200, r.text
    assert r.json() == {"org_id": org_id, "org": "team-b", "previous_slug": "team-a", "name": "Team B"}
    orgs = (await c.get("/orgs", headers=_h(otok))).json()
    assert [o["slug"] for o in orgs if o["org_id"] == org_id] == ["team-b"]


async def test_old_slug_keeps_resolving_for_pinned_credentials(c: AsyncClient):
    org_id, otok, mtok, *_ = await _team_with_member(c)
    # otok was minted by POST /orgs with org=team-a baked into its claim
    await c.patch(f"/orgs/{org_id}", headers=_h(otok), json={"slug": "team-b"})
    for slug in ("team-a", "team-b"):
        r = await c.post("/agents/checkin", headers={**_h(otok), "X-Treg-Org": slug})
        assert r.status_code == 200, (slug, r.text)
        assert r.json()["org"] == "team-b"
    assert (await c.get("/tools", headers=_h(mtok))).status_code == 200


async def test_retired_slug_cannot_be_taken_by_another_team(c: AsyncClient):
    org_id, otok, *_ = await _team_with_member(c)
    await c.patch(f"/orgs/{org_id}", headers=_h(otok), json={"slug": "team-b"})
    other = await _register(c, "other@x.dev")
    r = await c.post("/orgs", headers=_h(other), json={"name": "Team A"})
    assert r.json()["org"] == "team-a-2"
    other_id, other_tok = r.json()["org_id"], r.json()["token"]
    for taken in ("team-a", "team-b"):
        r = await c.patch(f"/orgs/{other_id}", headers=_h(other_tok), json={"slug": taken})
        assert r.status_code == 409, (taken, r.text)


async def test_bad_slugs_rejected(c: AsyncClient):
    org_id, otok, *_ = await _team_with_member(c)
    for bad in ("Bad Slug!", "ab", "sbx-0123456789ab", "x" * 41):
        r = await c.patch(f"/orgs/{org_id}", headers=_h(otok), json={"slug": bad})
        assert r.status_code == 400, (bad, r.text)
    assert (await c.patch(f"/orgs/{org_id}", headers=_h(otok), json={"name": " "})).status_code == 400
    assert (await c.patch(f"/orgs/{org_id}", headers=_h(otok), json={})).status_code == 422


async def test_agent_named_before_rename_keeps_its_short_name(c: AsyncClient):
    org_id, otok, *_ = await _team_with_member(c)
    r = await c.post(f"/orgs/{org_id}/agents", headers=_h(otok), json={"name": "deploy"})
    assert r.status_code == 200, r.text
    await c.patch(f"/orgs/{org_id}", headers=_h(otok), json={"slug": "team-b"})
    names = {a["name"] for a in (await c.get(f"/orgs/{org_id}/agents", headers=_h(otok))).json()}
    assert "deploy" in names
    keys = (await c.get(f"/orgs/{org_id}/api-keys", headers=_h(otok))).json()
    assert any(k.get("assigned_name") == "deploy" or k.get("name") == "deploy" for k in keys), keys
