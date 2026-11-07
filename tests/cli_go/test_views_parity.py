"""The views the registry renders (`X-Treg-View: 1`), printed by the Go CLI, against the Python CLI
that builds them itself: bare `treg`, `catalog`, `catalog search|get|request`, `balance` and
`feedback get`, in text and `--json`."""
from __future__ import annotations

import json
import time

import pytest

from .cli_go_harness import PYTHON_CLI, Canned, FakeRegistry, normalize, run, write_config

NO_PROXY = {"NO_PROXY": "*", "no_proxy": "*"}


def _pair(tmp_path, base_url, go_bin, argv, *, config=None, env=None):
    out = []
    for label, cli in (("py", PYTHON_CLI), ("go", [str(go_bin)])):
        home = tmp_path / label
        write_config(home, base_url=base_url, **(config or {}))
        out.append(run(cli + argv, home=home, env=NO_PROXY | (env or {})))
    return out


def _signed_in(team) -> dict:
    return {"token": team.token, "active_org": team.slug, "identity": True}


def _same(py, go):
    assert py.code == go.code, (py, go)
    assert normalize(py.text) == normalize(go.text), (py.text, go.text)
    assert normalize(py.err) == normalize(go.err), (py.err, go.err)


def _identity(live, email: str) -> str:
    started = live.http.post("/auth/email/start", json={"email": email})
    proof = live.http.post("/auth/email/verify", json={"email": email, "code": started.json()["dev_code"]})
    live.http.cookies.clear()
    return proof.json()["token"]


# ---- bare `treg` -------------------------------------------------------------------------------

def _home(text: str) -> tuple[str | None, list[str]]:
    """The registry line, and every other line but the CLI's own `treg:` line. The Go CLI prints the
    registry line second, before the registry's text; the Python CLI printed it after `about:`."""
    lines = normalize(text).splitlines()
    assert lines[0].startswith("treg: ")
    registry = next((line for line in lines if line.startswith("registry: ")), None)
    return registry, [line for line in lines[1:] if line != registry]


def _same_home(py, go):
    assert py.code == go.code == 0, (py, go)
    assert _home(py.text) == _home(go.text), (py.text, go.text)
    assert go.text.splitlines()[1].startswith("registry: ") or _home(go.text)[0] is None
    assert normalize(py.err) == normalize(go.err)


@pytest.mark.parametrize("who", ["team", "nobody", "rejected", "no_team"])
def test_home(tmp_path, live, team, go_bin, who):
    config = {"team": _signed_in(team), "nobody": {}, "rejected": {"token": "not-a-key"},
              "no_team": {"token": _identity(live, "no-team@example.com"), "identity": True}}[who]
    py, go = _pair(tmp_path, live.url, go_bin, [], config=config)
    _same_home(py, go)
    py, go = _pair(tmp_path, live.url, go_bin, ["--json"], config=config)
    data = json.loads(go.out)
    if who in ("team", "no_team"):
        mine = live.http.get("/home", headers={"X-Treg-Token": config["token"]}).json()
        assert data == mine
        assert {k: v for k, v in json.loads(py.out).items() if k in data} == data
    else:
        assert "detail" in data and go.code == 0


@pytest.mark.parametrize("canned", [Canned(500, {}, b"oops"), Canned(418, {}, b'{"x":1}'), Canned(200, {}, b'{"x":1}')])
def test_home_when_the_registry_cannot_render_it(tmp_path, go_bin, fake, canned):
    fake.route("/home", canned)
    py, go = _pair(tmp_path, fake.url, go_bin, [], config={"token": "tok"})
    _same_home(py, go)
    py, go = _pair(tmp_path, fake.url, go_bin, ["--json"], config={"token": "tok"})
    assert {k: v for k, v in json.loads(py.out).items() if k not in ("bin", "version")} == \
           {k: v for k, v in json.loads(go.out).items() if k not in ("bin", "version")}


def test_home_when_nothing_answers(tmp_path, go_bin):
    """The Python CLI named the failure by its httpx exception; the Go CLI gives the system's reason."""
    py, go = _pair(tmp_path, "http://127.0.0.1:9", go_bin, [], config={"token": "tok"})
    assert py.code == go.code == 0, (py, go)
    (py_registry, py_rest), (go_registry, go_rest) = _home(py.text), _home(go.text)
    assert py_registry == 'registry: "http://127.0.0.1:9 (ConnectError reaching the registry)"'
    assert go_registry == 'registry: "http://127.0.0.1:9 (not reachable: connection refused)"'
    assert py_rest == go_rest and go.err == ""
    go = run([str(go_bin), "--json"], home=tmp_path / "go", env=NO_PROXY)
    assert json.loads(go.out)["unavailable"] == "not reachable: connection refused"


# ---- catalog -----------------------------------------------------------------------------------

CATALOG = [
    ["catalog"],
    ["catalog", "tiktok"],
    ["catalog", "tiktok", "--all"],
    ["catalog", "nope"],
    ["catalog", "search", "tiktok", "comments"],
    ["catalog", "search", "tiktok", "comments", "--limit", "3"],
    ["catalog", "search", "zzqx", "vvqwk"],
    ["catalog", "search", "--new"],
    ["catalog", "search", "phone", "--new", "60"],
    ["catalog", "search", "--new", "400", "--limit", "2"],
    ["catalog", "search", "linkedin", "--sort", "newest"],
    ["catalog", "get", "tikhub.tiktok.video.comments"],
    ["catalog", "get", "tikhub.tiktok.user.profile", "--limit", "2"],
    ["catalog", "get", "treg.people.email.find"],
    ["catalog", "get", "people.email.find"],
    ["catalog", "get", "tikhub.tiktok.video.coments"],
    ["catalog", "get", "nope.nope"],
]


@pytest.mark.parametrize("signed_in", [True, False], ids=["signed-in", "anonymous"])
@pytest.mark.parametrize("json_flag", [False, True], ids=["text", "json"])
@pytest.mark.parametrize("argv", CATALOG, ids=" ".join)
def test_catalog(tmp_path, live, team, go_bin, argv, json_flag, signed_in):
    if argv[1:2] == ["get"] and len(argv) > 2:
        _warm_observations(live, argv[2])
    py, go = _pair(tmp_path, live.url, go_bin, (["--json"] if json_flag else []) + argv,
                   config=_signed_in(team) if signed_in else {})
    _same(py, go) if not json_flag else _same_json(py, go)


def _warm_observations(live, endpoint_id: str) -> None:
    """`observed_pending` says the observation cache had not read an endpoint yet and fills in on
    its own; read it until it has, so both CLIs see the same answer."""
    for _ in range(50):
        r = live.http.get(f"/catalog/endpoints/{endpoint_id}")
        if r.status_code != 200 or not r.json().get("observed_pending"):
            return
        time.sleep(0.1)


def _same_json(py, go):
    assert py.code == go.code, (py, go)
    assert json.loads(py.out) == json.loads(go.out), (py.out[:2000], go.out[:2000])
    assert py.err == go.err == ""


def _unrouted_job() -> str:
    from treg.domain.catalog import store as catalog_store
    cat = catalog_store.load()
    for cap in sorted(cat.capabilities):
        eps = cat.for_capability(cap)
        if (len({e["provider"] for e in eps}) > 1 and not any(e.get("kind") == "routed" for e in eps)
                and cap not in cat.by_id and cat.for_platform(cap.split(".", 1)[0])):
            return cap
    raise AssertionError("the catalog has no job without a routed id")


@pytest.mark.parametrize("json_flag", [False, True], ids=["text", "json"])
def test_catalog_get_a_job_without_a_routed_id(tmp_path, live, team, go_bin, json_flag):
    py, go = _pair(tmp_path, live.url, go_bin, (["--json"] if json_flag else []) + ["catalog", "get", _unrouted_job()],
                   config=_signed_in(team))
    _same(py, go) if not json_flag else _same_json(py, go)


def test_catalog_get_hides_the_providers_a_pin_refuses(tmp_path, live, go_bin):
    pinned = live.signup("pinned@example.com", "Pinned Team")
    r = live.http.post(f"/orgs/{pinned.org_id}/pins", headers={"X-Treg-Token": pinned.token},
                       json={"capability": "tiktok.user.profile", "provider": "tikhub"})
    r.raise_for_status()
    for argv in (["catalog", "get", "tikhub.tiktok.user.profile"], ["catalog", "get", "tikhub.tiktok.user.profile", "--limit", "1"]):
        py, go = _pair(tmp_path, live.url, go_bin, argv, config=_signed_in(pinned))
        _same(py, go)
        assert "your team pins this job to tikhub" in go.text


@pytest.mark.parametrize("argv", [["catalog", "search"], ["catalog", "get"], ["catalog", "request", " "],
                                  ["catalog", "tiktok", "comments"]], ids=" ".join)
def test_catalog_refuses_before_asking(tmp_path, live, go_bin, argv):
    py, go = _pair(tmp_path, live.url, go_bin, argv)
    _same(py, go)
    assert go.code == 1


@pytest.mark.parametrize("argv", [["catalog", "search", "--new", "phone"], ["catalog", "search", "x", "--sort", "old"],
                                  ["catalog", "search", "x", "--limit", "many"]], ids=" ".join)
def test_catalog_usage_errors(tmp_path, live, go_bin, argv):
    py, go = _pair(tmp_path, live.url, go_bin, argv)
    assert py.code == go.code == 2 and py.out == go.out == b""


@pytest.mark.parametrize("json_flag", [False, True], ids=["text", "json"])
def test_catalog_request(tmp_path, live, team, go_bin, json_flag):
    py, go = _pair(tmp_path, live.url, go_bin, (["--json"] if json_flag else []) + ["catalog", "request", "Ahrefs", "backlinks"],
                   config=_signed_in(team))
    if not json_flag:
        _same(py, go)
        return
    assert py.code == go.code == 0 and py.err == go.err == ""
    filed = [json.loads(r.out) for r in (py, go)]
    assert filed[0].pop("id") != filed[1].pop("id"), "each run files its own request"
    assert filed[0] == filed[1]


def test_catalog_request_refused(tmp_path, go_bin):
    """A refusal prints as JSON with the CLI's diagnostics line, from the registry's text."""
    from treg.routers.views import account as account_views
    body = {"detail": "too many tool requests from here — try again later"}
    plain, viewed = FakeRegistry(), FakeRegistry()
    try:
        plain.route("/tool-requests", Canned(429, {}, json.dumps(body).encode()))
        envelope = {"data": body, "text": account_views.as_json(body)}
        viewed.route("/tool-requests", Canned(429, {"X-Treg-View": "1"}, json.dumps(envelope).encode()))
        py = run(PYTHON_CLI + ["catalog", "request", "x"], home=_home_dir(tmp_path, "py", plain.url))
        go = run([str(go_bin), "catalog", "request", "x"], home=_home_dir(tmp_path, "go", viewed.url))
        _same(py, go)
        assert go.code == 1 and "HTTP 429" in go.err
    finally:
        plain.close()
        viewed.close()


def _home_dir(tmp_path, label, url):
    home = tmp_path / label
    write_config(home, base_url=url)
    return home


def test_a_registry_without_views_gets_its_json_printed(tmp_path, go_bin, fake):
    fake.route("/catalog/platforms", Canned(200, {}, b'{"platforms":[]}'))
    go = run([str(go_bin), "catalog"], home=_home_dir(tmp_path, "go", fake.url))
    assert go.code == 0 and json.loads(go.out) == {"platforms": []}
    assert "without the text this CLI prints" in go.err
    assert fake.seen[-1].headers["x-treg-view"] == "1"


# ---- balance and feedback ----------------------------------------------------------------------

@pytest.mark.parametrize("argv", [["balance"], ["balance", "--limit", "1"], ["--json", "balance"]], ids=" ".join)
def test_balance(tmp_path, live, team, go_bin, argv):
    py, go = _pair(tmp_path, live.url, go_bin, argv, config=_signed_in(team))
    _same(py, go) if "--json" not in argv else _same_json(py, go)


@pytest.mark.parametrize("config", ["rejected", "no_team"])
def test_balance_without_a_team(tmp_path, live, go_bin, config):
    config = {"rejected": {"token": "not-a-key"},
              "no_team": {"token": _identity(live, "no-team-balance@example.com"), "identity": True}}[config]
    py, go = _pair(tmp_path, live.url, go_bin, ["balance"], config=config)
    _same(py, go)
    assert go.code == 1


def test_feedback_get(tmp_path, live, team, go_bin):
    filed = live.http.post("/feedback", headers={"X-Treg-Token": team.token},
                           json={"category": "other", "message": "café ✓ is slow", "call_ids": ["c1"]})
    filed.raise_for_status()
    fid = str(filed.json()["feedback_id"])
    for argv in (["feedback", "get", fid], ["--json", "feedback", "get", fid], ["feedback", "get", "999999"],
                 ["--json", "feedback", "get", "999999"], ["feedback", "get", "0"]):
        py, go = _pair(tmp_path, live.url, go_bin, argv, config=_signed_in(team))
        assert py.out == go.out and py.code == go.code, (argv, py, go)
    py, go = _pair(tmp_path, live.url, go_bin, ["feedback", "get", fid], config={"token": "not-a-key"})
    assert py.out == go.out and py.code == go.code == 1
    py, go = _pair(tmp_path, live.url, go_bin, ["feedback", "get", "abc"], config=_signed_in(team))
    assert py.code == go.code == 2
