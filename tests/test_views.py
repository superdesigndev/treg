"""Server-rendered views: `X-Treg-View: 1` answers `{data, text}` with the route's own body under
`data`, and `text` is what the CLI prints for it."""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient
from toon_format import decode

from treg import crypto
from treg.api import app
from treg.domain.catalog import store as catalog_store
from treg.infra.db import session_maker
from treg.models import Secret
from treg.routers.views import account as account_views

VIEW = {"X-Treg-View": "1"}
ANONYMOUS = {"X-Treg-Token": ""}


async def _org_id(c: AsyncClient) -> int:
    return (await c.get("/orgs")).json()[0]["org_id"]


async def _view(c: AsyncClient, path: str, *, status: int = 200, method: str = "GET", **kw) -> dict:
    r = await c.request(method, path, headers={**VIEW, **kw.pop("headers", {})}, **kw)
    assert r.status_code == status, r.text
    assert r.headers["X-Treg-View"] == "1"
    return r.json()


@pytest.mark.parametrize("path", ["/catalog/platforms", "/catalog/platforms/tiktok", "/catalog/search?q=tiktok+comments",
                                  "/catalog/endpoints/tikhub.tiktok.video.comments", "/home", "/feedback/404"])
async def test_data_is_the_routes_own_answer_byte_for_byte(clients: AsyncClient, path):
    # `observed_pending` follows the observation cache's refresh, so let it settle before comparing.
    await clients.get(path)
    await app.state.endpoint_observation_reader.wait_for_idle()
    plain = await clients.get(path)
    viewed = await clients.get(path, headers=VIEW)
    assert "X-Treg-View" not in plain.headers
    assert viewed.status_code == plain.status_code
    assert viewed.content.startswith(b'{"data":' + plain.content + b',"text":')
    kept = {k: v for k, v in plain.headers.items() if k not in ("content-length", "content-type")}
    assert kept.items() <= dict(viewed.headers).items()


async def test_routes_without_a_view_and_head_requests_ignore_the_header(clients: AsyncClient):
    orgs = await clients.get("/orgs", headers=VIEW)
    assert "X-Treg-View" not in orgs.headers and isinstance(orgs.json(), list)
    head = await clients.head("/catalog/platforms", headers=VIEW)
    assert head.status_code == 200 and "X-Treg-View" not in head.headers


async def test_a_view_that_fails_to_render_leaves_the_answer_as_it_was(clients: AsyncClient, monkeypatch):
    def broken(status, body):
        raise KeyError("feedback_id")
    monkeypatch.setattr(account_views, "feedback", broken)
    plain = await clients.get("/feedback/404")
    viewed = await clients.get("/feedback/404", headers=VIEW)
    assert (viewed.status_code, viewed.content) == (plain.status_code, plain.content)
    assert "X-Treg-View" not in viewed.headers


async def test_a_refusal_before_the_route_runs_is_rendered_with_its_status(clients: AsyncClient):
    org = await _org_id(clients)
    env = await _view(clients, f"/orgs/{org}/balance", status=401, headers={"X-Treg-Token": "not-a-key"})
    assert env["data"]["detail"] == "invalid token" and "treg login" in env["data"]["hint"]
    assert env["text"] == json.dumps(env["data"], indent=2) + "\n", "as `treg balance` prints a refusal"


async def test_feedback_get_never_echoes_the_body_of_a_refusal(clients: AsyncClient):
    missing = await _view(clients, "/feedback/404", status=404)
    assert json.loads(missing["text"]) == {"error": "not_found", "http_status": 404,
                                           "message": account_views.FEEDBACK_REFUSALS[404][1]}
    filed = (await clients.post("/feedback", json={"category": "other", "message": "café ✓"})).json()
    found = await _view(clients, f"/feedback/{filed['feedback_id']}")
    assert found["text"] == json.dumps(found["data"], indent=2) + "\n"
    assert "\\u00e9" in found["text"], "escaped as the CLI has always printed it"


async def test_home_says_who_is_signed_in_and_never_the_clients_own_lines(clients: AsyncClient):
    home = decode((await _view(clients, "/home"))["text"])
    assert home["account"] == "tim@superdesign.dev" and home["team"].endswith("(owner)")
    assert "treg" not in home and "registry" not in home
    assert home["about"].startswith("The tool catalog for your agent. Search and call ")

    nobody = decode((await _view(clients, "/home", status=401, headers=ANONYMOUS))["text"])
    assert nobody["account"] == "not signed in" and nobody["help"][0].startswith("treg login")
    assert nobody["about"].endswith("its tools")

    rejected = decode((await _view(clients, "/home", status=401, headers={"X-Treg-Token": "not-a-key"}))["text"])
    assert rejected["account"] == "token rejected: invalid token"


async def test_balance_names_the_team_it_is_for(clients: AsyncClient):
    org = await _org_id(clients)
    env = await _view(clients, f"/orgs/{org}/balance?limit=1")
    slug = (await clients.get("/orgs")).json()[0]["slug"]
    assert env["text"].startswith(f"\n  Balance  {account_views.usd(env['data']['balance_micro'])}   ")
    assert f"team {slug}\n" in env["text"] and "recent ledger" in env["text"]


async def test_catalog_get_hides_the_providers_a_team_pin_refuses(clients: AsyncClient):
    org = await _org_id(clients)
    r = await clients.post(f"/orgs/{org}/pins", json={"capability": "tiktok.user.profile", "provider": "tikhub"})
    assert r.status_code == 200, r.text
    pinned = decode((await _view(clients, "/catalog/endpoints/tikhub.tiktok.user.profile"))["text"])
    assert {row["id"].split(".")[0] for row in pinned["compare"]} == {"tikhub"}
    assert pinned["pinned"].startswith("your team pins this job to tikhub")

    anyone = decode((await _view(clients, "/catalog/endpoints/tikhub.tiktok.user.profile", headers=ANONYMOUS))["text"])
    assert "pinned" not in anyone and len({row["id"].split(".")[0] for row in anyone["compare"]}) > 1


async def test_catalog_get_says_which_credential_serves_the_caller(clients: AsyncClient):
    signed_in = decode((await _view(clients, "/catalog/endpoints/tikhub.tiktok.video.comments"))["text"])
    assert "tikhub" in signed_in["access"]
    anyone = decode((await _view(clients, "/catalog/endpoints/tikhub.tiktok.video.comments", headers=ANONYMOUS))["text"])
    assert "access" not in anyone


async def test_own_key_marks_the_providers_the_team_holds_a_credential_for(clients: AsyncClient):
    org = await _org_id(clients)
    async with session_maker() as db:
        db.add(Secret(org_id=org, name="tikhub-key", kind="env", value=crypto.encrypt("k"), provider="tikhub"))
        await db.commit()
    shelf = decode((await _view(clients, "/catalog/platforms/tiktok"))["text"])
    assert any(row["own_key"] for row in shelf["endpoints"] if row["id"].startswith("tikhub."))
    found = decode((await _view(clients, "/catalog/search?q=tiktok+comments&limit=25"))["text"])
    assert any(row.get("own_key") for row in found["results"])
    anyone = decode((await _view(clients, "/catalog/platforms/tiktok", headers=ANONYMOUS))["text"])
    assert "own_key" not in anyone["endpoints"][0]


async def test_a_job_with_a_routed_id_answers_as_that_endpoint(clients: AsyncClient):
    """`catalog get <job>` with a routed id answers as that endpoint's card.

    The view embeds the routed card under `data`. Comparing that card byte-for-byte to a second
    GET of the routed id flakes when live observations flip between the two requests
    (`observed_pending` / fold), so this asserts the durable identity and the envelope shape.
    """
    r = await clients.get("/catalog/endpoints/tiktok.user.profile", headers=VIEW)
    assert r.status_code == 200 and r.headers["X-Treg-View"] == "1"
    assert r.content.startswith(b'{"data":')
    text_at = r.content.find(b',"text":')
    assert text_at > 0
    data = json.loads(r.content[len(b'{"data":'):text_at])
    assert data == r.json()["data"]
    assert data["endpoint"]["id"] == "treg.tiktok.user.profile"
    assert data["endpoint"]["kind"] == "routed"
    assert decode(r.json()["text"])["id"] == "treg.tiktok.user.profile"
    routed = await clients.get("/catalog/endpoints/treg.tiktok.user.profile")
    assert routed.status_code == 200
    assert routed.json()["endpoint"]["id"] == data["endpoint"]["id"]
    plain = await clients.get("/catalog/endpoints/tiktok.user.profile")
    assert plain.status_code == 404, "without the header the route answers as it always did"


def _unrouted_job() -> str:
    cat = catalog_store.load()
    for cap in sorted(cat.capabilities):
        eps = cat.for_capability(cap)
        if (len({e["provider"] for e in eps}) > 1 and not any(e.get("kind") == "routed" for e in eps)
                and cap not in cat.by_id and cat.for_platform(cap.split(".", 1)[0])):
            return cap
    raise AssertionError("the catalog has no job without a routed id")


async def test_a_job_without_a_routed_id_lists_every_provider_of_it(clients: AsyncClient):
    job = _unrouted_job()
    env = await _view(clients, f"/catalog/endpoints/{job}")
    page = decode(env["text"])
    assert page["job"] == job and page["providers"] > 1 and page["compare"]
    assert env["data"] == {**{k: v for k, v in page.items() if k != "help"}, "pinned": None, "help": page["help"]}


async def test_an_unknown_id_says_what_to_try(clients: AsyncClient):
    env = await _view(clients, "/catalog/endpoints/tikhub.tiktok.video.coments", status=404)
    page = decode(env["text"])
    assert page["error"] == "unknown_endpoint" and page["help"] == [f"treg catalog get {page['did_you_mean'][0]}"]
    assert env["data"]["detail"]["did_you_mean"] == page["did_you_mean"]


async def test_search_offers_the_same_search_with_more_rows(clients: AsyncClient):
    env = await _view(clients, "/catalog/search?q=&limit=2&added_within_days=400")
    page = decode(env["text"])
    assert page["added_within_days"] == 365 and page["capped_at_days"] == 365
    assert page["help"][-1] == f"treg catalog search --new 400 --limit {min(env['data']['total'], 100)}"


async def test_a_platform_the_catalog_lacks_is_a_rendered_refusal(clients: AsyncClient):
    page = decode((await _view(clients, "/catalog/platforms/nope", status=404))["text"])
    assert page == {"error": "http_404", "message": "unknown platform 'nope'", "status": 404}


async def test_a_tool_request_is_logged_or_refused_as_json(clients: AsyncClient):
    env = await _view(clients, "/tool-requests", method="POST", json={"capability": "  Ahrefs backlinks ", "source": "cli"})
    assert env["text"] == ('logged: "Ahrefs backlinks"\n'
                           "requests steer which provider gets added next - the most-asked-for tools land first\n")
    refused = await _view(clients, "/tool-requests", status=422, method="POST", json={"capability": " "})
    assert refused["text"] == json.dumps(refused["data"], indent=2) + "\n"


BALANCE = {
    "balance_micro": 987_400,
    "blocks": [{"kind": "promotional", "amount_micro": 1_000_000, "remaining_micro": 999_400,
                "created_at": "2026-07-30T10:00:00"},
               {"kind": "earned", "amount_micro": 3_000, "remaining_micro": 2_000, "created_at": "2026-07-31T10:00:00"},
               {"kind": "earned", "amount_micro": 5_000, "remaining_micro": 5_000, "created_at": "2026-08-01T10:00:00"}],
    "holds": [{"endpoint_id": "tikhub.x.y", "amount_micro": 12_000}, {"endpoint_id": None, "amount_micro": 1}],
    "entries": {"items": [{"kind": "settle", "amount_micro": -600, "endpoint_id": "tikhub.x.y", "meta": {},
                           "created_at": "2026-07-30T10:01:00"},
                          {"kind": "grant", "amount_micro": 1_000_000, "endpoint_id": None,
                           "meta": {"reason": "welcome"}, "created_at": "2026-07-30T10:00:00"}]},
}
AUTOTOPUP_ON = {"enabled": True, "amount_micro": 20_000_000, "threshold_micro": 5_000_000,
                "monthly_cap_micro": 2_000_000_000, "month_spend_micro": 0}


@pytest.mark.parametrize("body,auto", [
    (BALANCE, AUTOTOPUP_ON),
    (BALANCE, {"enabled": False, "disabled_reason": "the card was declined"}),
    ({**BALANCE, "blocks": [], "holds": [], "entries": {"items": []}}, None),
    ({**BALANCE, "blocks": [], "holds": [], "entries": {"items": [], "withheld": True}}, None),
])
def test_balance_text_is_what_the_python_cli_prints(monkeypatch, capsys, body, auto):
    """The Python CLI prints `treg balance` itself; the registry's text must not drift from it."""
    from treg import cli

    class _Resp:
        def __init__(self, data):
            self.status_code, self.text, self._data = 200, "", data

        def json(self):
            return self._data

    class _C:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, path, **kw):
            return _Resp({"configured": True, "autotopup": auto} if path == "/billing" else body)

    monkeypatch.setattr(cli, "_client", lambda cfg, **kw: _C())
    monkeypatch.setattr(cli, "_active_org", lambda cfg, c: {"org_id": 1, "slug": "acme"})
    cli.cmd_balance(cli.build_parser().parse_args(["balance"]), {})
    printed = capsys.readouterr().out.replace(" — ", " - ")
    assert printed == account_views.balance(body, team="acme", autotopup=auto)
