"""`GET /calls/{id}/result` on a routed call names the child calls that hold its answer.

A routed parent wraps a child's answer and has no archive link of its own; each child row keeps one.
A caller whose client gave up on a long route reads the answer it paid for through the children."""

from __future__ import annotations

from httpx import AsyncClient

from treg import audit
from treg.application.call import service as call_service

from test_marketplace_call import platform_on  # noqa: F401
from test_routing import ROUTED, _relay_by_provider, enrichment_on  # noqa: F401


async def test_a_routed_call_result_names_its_child_calls(clients: AsyncClient, enrichment_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [(503, {"error": "down"})],
         "findymail": [(200, {"contact": {"name": "Ada Example", "email": "ada@example.com"}})]}, seen))
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Ada Example", "domain": "example.com"})
    assert r.status_code == 200, r.text
    await audit.drain()
    ref = r.headers["X-Treg-Call-Id"]
    got = await clients.get(f"/calls/{ref}/result")
    assert got.status_code == 200, got.text
    d = got.json()
    assert d["credential_tier"] == "routed" and d["stored"] is False
    assert "child" in d["note"] and "own tools" not in d["note"]
    kids = d["children"]
    assert [k["call_ref"] for k in kids] == [f"{ref}:r0", f"{ref}:r1"]
    assert [(k["endpoint_id"], k["status_code"]) for k in kids] == [
        (t["endpoint_id"], t["status"]) for t in r.json()["_treg"]["tried"]]
    # Each child is readable through the same endpoint.
    child = await clients.get(f"/calls/{kids[1]['call_ref']}/result")
    assert child.status_code == 200 and child.json()["endpoint_id"] == r.json()["_treg"]["served_by"]
