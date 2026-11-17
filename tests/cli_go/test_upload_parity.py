"""`treg scan`, `treg upload` and the hidden `treg import` from the Go CLI beside the Python CLI:
the same output, exit status, files written and what each registers, against a real registry."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from .agent_sandbox import Machine, both, pair, run_in, same_files, same_run
from .cli_go_harness import ROOT, Canned, normalize, write_config

BUNDLED_CATALOG = ROOT / "cli" / "internal" / "providers" / "catalog.json"
REGENERATE = ("uv run python -c \"import json; from treg import providers as p; from tests.cli_go.cli_go_harness "
              "import normalize; open('cli/internal/providers/catalog.json', 'w').write(normalize(json.dumps("
              "{'version': p.CATALOG_VERSION, 'providers': p.CATALOG}, indent=1, ensure_ascii=False)) + '\\n')\"")


def test_bundled_provider_catalog_matches_python():
    """The Go CLI's offline floor is the Python CLI's providers.CATALOG, version included."""
    from treg import providers
    want = json.loads(normalize(json.dumps({"version": providers.CATALOG_VERSION, "providers": providers.CATALOG},
                                           ensure_ascii=False)))
    assert json.loads(BUNDLED_CATALOG.read_text(encoding="utf-8")) == want, f"regenerate it: {REGENERATE}"


ENV = """# a project .env
export OPENAI_API_KEY="sk-test-1"
TREG_RESEND_API_KEY='re_2'
HF_TOKEN=hf
GITHUB_TOKEN=gh1
GH_TOKEN=gh2
GITHUB_CLIENT_ID=cid
GITHUB_CLIENT_SECRET=csec
TWILIO_ACCOUNT_SID=AC1
TWILIO_AUTH_TOKEN=tw
CRUSTDATA_API_KEY=cd
TELEGRAM_BOT_TOKEN=tg
STRIPE_WEBHOOK_SECRET=whsec
POSTHOG_HOST=https://x
SECRET_KEY=dj
MYSTERY_API_KEY=m
DEBUG=1
"""


def _project(m: Machine, echo_url: str, *, env: str = ENV) -> Path:
    root = m.home / "project"
    m.write("project/.env", env)
    m.write("project/skills/notes/SKILL.md", "---\nname: notes\n---\nHow we write notes.\n")
    m.write("project/skills/notes/guide/style.md", "be brief\r\nand kind\n")
    m.write("project/skills/notes/node_modules/x.js", "junk")
    m.write("project/skills/contracted/SKILL.md", "---\nname: contracted\n---\nCall it.\n")
    m.write("project/skills/contracted/.secret/token", "tok-from-file\n")
    m.write("project/skills/contracted/treg.json", json.dumps({
        "name": "contracted", "base_url": echo_url,
        "secrets": [{"file": ".secrets/token", "name": "contracted", "kind": "env"}],
        "bindings": [{"secret": "contracted", "injector": "env", "location": "header",
                      "name": "Authorization", "format": "Bearer {secret}"}]}))
    m.write("project/skills/echo-env/SKILL.md", "---\nname: echo-env\n---\nRun run.py.\n")
    m.write("project/skills/echo-env/run.py", f'API = "{echo_url}"\nimport os\nos.environ.get("ECHO_TOKEN")\n')
    m.write("project/skills/stripe-cli/SKILL.md", "---\nname: stripe-cli\n---\nUse stripe.\n")
    m.write("project/skills/gapped/SKILL.md", "---\nname: gapped\n---\nSee docs.example.com\n")
    m.write("project/skills/gapped/tool.sh", 'curl -H "x: $GAPPED_API_KEY" "$URL"\n')
    m.write("project/skills/multi/SKILL.md", "---\nname: multi\n---\nhttps://api.multi.example/v1 is the API\n")
    m.write("project/skills/multi/.secrets/developer_token", "dev\n")
    m.write("project/skills/multi/.secrets/token.json", '{"token": "t", "refresh_token": "r"}')
    m.write("project/skills/multi/.secrets/client_secret.json", '{"installed": {}}')
    m.write("project/skills/.hidden/SKILL.md", "---\nname: hidden\n---\n")
    m.write("project/skills/broken/SKILL.md", "---\nname: broken\n---\n")
    m.write("project/skills/broken/treg.json", "{not json")
    return root


def _clis(m: Machine) -> None:
    for name in ("gh", "stripe", "pscale", "openai", "az"):
        m.fake_bin(name)
    m.write(".config/stripe/config.toml", "[default]\n")


@pytest.fixture(scope="session")
def upload_teams(live):
    return live.signup("upload-py@example.com", "Upload Py"), live.signup("upload-go@example.com", "Upload Go")


def _wipe(live, team) -> None:
    h = {"X-Treg-Token": team.token}
    for kind in ("bundles", "tools", "secrets"):
        for row in live.http.get(f"/{kind}", headers=h).json():
            live.http.delete(f"/{kind}/{row['id']}", headers=h)


@pytest.fixture
def teams(live, upload_teams):
    for t in upload_teams:
        _wipe(live, t)
    return upload_teams


def _machines(tmp_path, live, teams):
    py_t, go_t = teams
    py_m = Machine(tmp_path / "py" / "home", tmp_path / "py" / "bin")
    go_m = Machine(tmp_path / "go" / "home", tmp_path / "go" / "bin")

    for m, t in ((py_m, py_t), (go_m, go_t)):
        m.home.mkdir(parents=True)
        m.bin.mkdir(parents=True)
        write_config(m.home, base_url=live.url, token=t.token, active_org=t.slug, identity=True)
    return py_m, go_m


def state(live, team) -> dict:
    """What a team has registered, with ids, owners and team names left out."""
    h = {"X-Treg-Token": team.token}
    secrets = {s["id"]: s for s in live.http.get("/secrets", headers=h).json()}
    bundles = {b["id"]: live.http.get(f"/bundles/{b['id']}", headers=h).json()
               for b in live.http.get("/bundles", headers=h).json()}

    def secret_name(sid):
        return secrets[sid]["name"] if sid in secrets else sid

    def unref(entries):
        return [{**e, **({"secret_id": secret_name(e["secret_id"])} if "secret_id" in e else {})}
                for e in entries or []]

    tools = []
    for t in sorted(live.http.get("/tools", headers=h).json(), key=lambda t: t["name"]):
        cli = t.get("cli")
        if cli and "inject" in cli:
            cli = {**cli, "inject": unref(cli["inject"])}
        tools.append({"name": t["name"], "base_url": t["base_url"], "bindings": unref(t["bindings"]),
                      "health_check": t["health_check"], "examples": t["examples"], "cli": cli,
                      "bundle": bundles.get(t["bundle_id"], {}).get("name")})
    return {
        "tools": tools,
        "secrets": sorted((s["name"], s["kind"], bundles.get(s["bundle_id"], {}).get("name")) for s in secrets.values()),
        "bundles": sorted(({"name": b["name"], "recipe": b["recipe"], "files": b["files"],
                            "tools": sorted(t["name"] for t in b["tools"]),
                            "secrets": sorted(s["name"] for s in b["secrets"])} for b in bundles.values()),
                          key=lambda b: b["name"]),
    }


def _project_cwd(m):
    return m.home / "project"


@pytest.mark.parametrize("argv", [
    ["scan"],
    ["scan", "env"],
    ["scan", "env", "--select", "OpenAI, github"],
    ["scan", "env", "--select", "nothing-like-it"],
    ["scan", "clis"],
    ["scan", "clis", "--status"],
    ["scan", "skills", "--skills-dir", "skills"],
    ["scan", "skills", "--skills-dir", "skills", "--select", "NOTES,multi"],
    ["scan", "--dir", "skills/notes"],
    ["scan", "skills", "--skills-dir", "skills/echo-env"],
    ["upload", "--dry-run", "--llm", "--skills-dir", "skills"],
    ["import", "env", "--dry-run"],
])
def test_scan(tmp_path, live, echo, go_bin, argv):
    py_m, go_m = pair(tmp_path, registry=live.url, token="unused")
    for m in (py_m, go_m):
        _project(m, echo.url)
        _clis(m)
    py, go = both(py_m, go_m, go_bin, argv, cwd=_project_cwd, env={"GH_TOKEN": "from-env"})
    same_run(py, go, py_m, go_m)
    same_files(py_m, go_m)


@pytest.mark.parametrize("argv", [
    ["scan", "env"],
    ["scan", "skills"],
    ["scan", "skills", "--skills-dir", "missing"],
    ["scan"],
    ["upload", "env", "--env-file", "nope.env"],
])
def test_scan_nothing_there(tmp_path, live, go_bin, argv):
    py_m, go_m = pair(tmp_path, registry=live.url)
    for m in (py_m, go_m):
        m.mkdirs("empty", ".claude/skills/one", ".agents/skills")
        m.write(".claude/skills/one/SKILL.md", "---\nname: one\n---\n")
    py, go = both(py_m, go_m, go_bin, argv, cwd=lambda m: m.home / "empty")
    same_run(py, go, py_m, go_m)


def test_upload_refuses_without_intent(tmp_path, live, teams, echo, go_bin):
    """Never import silently: no terminal and no --all, --select or --dry-run is a refusal."""
    py_m, go_m = _machines(tmp_path, live, teams)
    for m in (py_m, go_m):
        _project(m, echo.url)
    for argv in (["upload", "env"], ["upload", "skills", "--skills-dir", "skills"]):
        py, go = both(py_m, go_m, go_bin, argv, cwd=_project_cwd)
        assert py.code == go.code == 1
        same_run(py, go, py_m, go_m)
    assert state(live, teams[0]) == state(live, teams[1])


def test_upload_env(tmp_path, live, teams, echo, go_bin):
    py_m, go_m = _machines(tmp_path, live, teams)
    for m in (py_m, go_m):
        _project(m, echo.url)
    for argv in (["upload", "env", "--all"], ["upload", "env", "--all"], ["upload", "env", "--select", "openai,twilio", "--replace"],
                 ["upload", "env", "--all", "--llm"]):
        py, go = both(py_m, go_m, go_bin, argv, cwd=_project_cwd)
        same_run(py, go, py_m, go_m)
        same_files(py_m, go_m)
        assert state(live, teams[0]) == state(live, teams[1])
    tools = {t["name"]: t for t in state(live, teams[1])["tools"]}
    assert sorted(tools) == ["crustdata", "github", "github-2", "huggingface", "openai", "resend", "twilio"]
    assert tools["crustdata"]["bindings"][1] == {"secret_id": "CRUSTDATA_API_KEY", "injector": "env", "location": "header",
                                                 "name": "x-api-version", "format": "2025-11-01"}


def test_upload_env_llm(tmp_path, live, teams, fake, go_bin):
    """The model's suggestions are confirmed one by one; without a terminal each is declined."""
    fake.route("/v1/chat/completions", Canned(200, {}, json.dumps({"choices": [{"message": {"content": (
        '<think>{"resolved": []}</think>```json\n{"resolved": ['
        '{"var": "MYSTERY_API_KEY", "provider": "Mystery Co", "base_url": "https://api.mystery.example/v2",'
        ' "auth": {"shape": "api_key_header", "header": "X-Mystery"}},'
        '{"var": "OTHER_TOKEN", "provider": "Other", "base_url": "http://insecure.example", "auth": {"shape": "bearer"}}]}\n```')}}]}).encode()))
    py_m, go_m = _machines(tmp_path, live, teams)
    for m in (py_m, go_m):
        m.write("project/.env", "MYSTERY_API_KEY=m\nOTHER_TOKEN=o\n")
    for argv in (["upload", "env", "--all", "--llm", "--llm-token", "k", "--llm-base-url", fake.url + "/v1"],
                 ["upload", "env", "--all", "--llm", "--llm-base-url", fake.url + "/v1"],
                 ["upload", "env", "--all", "--llm", "--llm-token", "k", "--llm-base-url", fake.url + "/nope"]):
        py, go = both(py_m, go_m, go_bin, argv, cwd=_project_cwd)
        assert py.code == go.code == 0
        if "/nope" in argv[-1]:
            assert "✗ LLM call failed: " in py.text and "✗ LLM call failed: " in go.text
            continue
        same_run(py, go, py_m, go_m)
    sent = [json.loads(normalize(s.body.decode())) for s in fake.seen if s.path == "/v1/chat/completions"]
    assert len(sent) == 2 and sent[0] == sent[1]
    assert [s.headers["authorization"] for s in fake.seen if s.path == "/v1/chat/completions"] == ["Bearer k"] * 2


def test_upload_clis(tmp_path, live, teams, go_bin):
    py_m, go_m = _machines(tmp_path, live, teams)
    for m in (py_m, go_m):
        _clis(m)
        m.write("project/.env", "PSCALE_X=1\n")
    for argv in (["upload", "clis"], ["upload", "clis"], ["upload", "clis", "--replace"], ["scan", "clis", "--status"]):
        py, go = both(py_m, go_m, go_bin, argv, cwd=_project_cwd, env={"GH_TOKEN": "gh-from-env"})
        same_run(py, go, py_m, go_m)
        same_files(py_m, go_m)
        assert state(live, teams[0]) == state(live, teams[1])
    tools = {t["name"]: t for t in state(live, teams[1])["tools"]}
    assert sorted(tools) == ["gh", "stripe"]
    assert tools["gh"]["bindings"][0]["secret_id"] == "gh-key"
    assert tools["stripe"]["cli"]["inject"] == []


def test_upload_clis_add(tmp_path, live, teams, go_bin):
    py_m, go_m = _machines(tmp_path, live, teams)
    for m in (py_m, go_m):
        m.fake_bin("my_tool")
        m.mkdirs("project")
    for argv, env in ((["upload", "clis", "--add", "my_tool", "--env", "MY_TOOL_TOKEN", "--base-url", "https://api.my.example"],
                       {"MY_TOOL_TOKEN": "secret-1"}),
                      (["upload", "clis", "--add", "my_tool", "--env", "", "--base-url", "https://api.my.example"], {}),
                      (["upload", "clis", "--add", "my_tool", "--env", "UNSET_TOKEN", "--base-url", "https://api.my.example",
                        "--replace"], {}),
                      (["upload", "clis", "--add", "not-installed", "--env", "", "--base-url", "https://x.example"], {}),
                      (["upload", "clis", "--add", "gh", "--env", "", "--base-url", "https://x.example"], {}),
                      (["scan", "clis", "--add", "my_tool", "--env", "", "--base-url", "https://api.my.example"], {})):
        if argv[3] == "gh":
            for m in (py_m, go_m):
                m.fake_bin("gh")
        py, go = both(py_m, go_m, go_bin, argv, cwd=_project_cwd, env=env)
        same_run(py, go, py_m, go_m)
        assert state(live, teams[0]) == state(live, teams[1])
    tools = {t["name"]: t for t in state(live, teams[1])["tools"]}
    assert tools["my-tool"]["cli"] == {"bin": "my_tool", "enabled": True, "auth_mechanism": "env",
                                       "inject": [{"via": "env", "name": "UNSET_TOKEN"}]}


def test_upload_clis_add_needs_its_answers_go(tmp_path, live, teams, go_bin):
    """Intentional difference: without a terminal the Go CLI refuses to prompt for --env and
    --base-url instead of reading an idle pipe; the Python CLI read stdin regardless."""
    _, go_m = _machines(tmp_path, live, teams)
    go_m.fake_bin("my_tool")
    go = run_in(go_m, [str(go_bin)], ["upload", "clis", "--add", "my_tool"], stdin=None)
    assert go.code == 1
    assert go.err.startswith("non-interactive: pass --env VAR")
    go = run_in(go_m, [str(go_bin)], ["upload", "clis", "--add", "my_tool", "--env", "X"])
    assert go.code == 1 and go.err.startswith("non-interactive: pass --base-url URL")
    assert state(live, teams[1])["tools"] == []


def test_upload_skills(tmp_path, live, teams, echo, go_bin):
    py_m, go_m = _machines(tmp_path, live, teams)
    for m in (py_m, go_m):
        _project(m, echo.url, env="ECHO_TOKEN=tok-from-env\n")
    for argv in (["upload", "skills", "--skills-dir", "skills/", "--all"],
                 ["upload", "skills", "--skills-dir", "skills", "--all"],
                 ["upload", "skills", "--skills-dir", "skills", "--select", "notes", "--replace"],
                 ["upload", "--all", "--dir", "skills"]):
        py, go = both(py_m, go_m, go_bin, argv, cwd=_project_cwd, env={"STRIPE_API_KEY": "sk_machine"})
        same_run(py, go, py_m, go_m)
        same_files(py_m, go_m)
        assert state(live, teams[0]) == state(live, teams[1])
    names = [b["name"] for b in state(live, teams[1])["bundles"]]
    assert names == ["contracted", "echo-env", "multi", "notes", "stripe-cli"]
    contract = json.loads((go_m.home / "project/skills/echo-env/treg.json").read_text())
    assert contract["secrets"] == [{"env": "ECHO_TOKEN", "name": "echo-env", "kind": "env"}]
    for tool, token in (("contracted", "tok-from-file"), ("echo-env", "tok-from-env")):
        call = run_in(go_m, [str(go_bin)], ["call", tool, "v1/x"])
        assert call.code == 0, call
        assert json.loads(call.out)["authorization"] == f"Bearer {token}"


def test_upload_everything_in_one_dir(tmp_path, live, teams, echo, go_bin):
    py_m, go_m = _machines(tmp_path, live, teams)
    for m in (py_m, go_m):
        _project(m, echo.url, env="ECHO_TOKEN=t\nRESEND_API_KEY=re\n")
        _clis(m)
    py, go = both(py_m, go_m, go_bin, ["upload", "--all", "--skills-dir", "skills"], cwd=_project_cwd)
    same_run(py, go, py_m, go_m)
    same_files(py_m, go_m)
    assert state(live, teams[0]) == state(live, teams[1])


def test_import_alias_says_its_new_name(tmp_path, live, go_bin):
    py_m, go_m = pair(tmp_path, registry=live.url)
    for m in (py_m, go_m):
        m.write("project/.env", "OPENAI_API_KEY=x\n")
    py, go = both(py_m, go_m, go_bin, ["import", "env", "--all"], cwd=_project_cwd)
    same_run(py, go, py_m, go_m)
    assert go.err.startswith("note: `treg import` is now `treg upload`")
