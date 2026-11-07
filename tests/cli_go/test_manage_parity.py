"""The team, tool, secret, connection, audit, feedback, hosting and billing commands print the same
output, exit with the same status, write the same config and send the registry the same requests
from the Go CLI as from the Python CLI. A fake registry plays each answer, so refusals and odd
bodies are covered too."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field

import pytest

from .cli_go_harness import PYTHON_CLI, Canned, normalize, run, same_stdout, write_config

ORGS = json.dumps([
    {"org_id": 7, "slug": "acme", "name": "Acme", "role": "owner", "active": True},
    {"org_id": 9, "slug": "beta-team", "name": "Beta Team ü", "role": "member", "active": False},
]).encode()
OK = Canned(200, {}, b'{"ok": true}')
DEFAULT_KEY = Canned(200, {}, json.dumps({"org": "beta-team", "token": "beta-default-key",
                                          "default_key_state": "active"}).encode())
TOOLS = json.dumps([{"id": 2, "name": "stripe", "cli": {"inject": ["STRIPE_KEY"], "enabled": False, "deny": []}},
                    {"id": 3, "name": "gh"}]).encode()
CALLS = json.dumps([
    {"id": 11, "kind": "proxy", "user_email": "a@b.c", "tool_name": "echo", "method": "GET", "path": "/v1",
     "status_code": 200, "created_at": "2026-11-01T10:00:00"},
    {"id": 12, "kind": "local_run", "tool_name": "gh", "created_at": "2026-11-01T11:00:00"},
    {"id": 13, "user_email": "é@b.c", "tool_name": "gen", "method": None, "path": "/x", "status_code": 201,
     "created_at": "2026-11-01T09:00:00", "cost": 1.5e-7,
     "async_task": {"status": "done", "settled_micro": 1200, "result_url": "https://r", "extra": 1}},
    {"id": 14, "tool_name": "same", "created_at": "2026-11-01T10:00:00"},
]).encode()
RUNS = json.dumps([
    {"id": "r1", "user_email": "a@b.c", "tool": "gh", "argv": ["pr", "list"], "exit_code": None,
     "where": "local", "created_at": "2026-11-01T12:00:00"},
    {"id": "r2", "tool": "db", "argv": None, "exit_code": 3, "where": "server", "created_at": None},
]).encode()
PROVIDERS = json.dumps([
    {"service": "google-search-console", "auth_kind": "oauth", "display_name": "Search Console"},
    {"service": "hunter", "auth_kind": "key", "display_name": "Hunter", "token_label": "API key",
     "setup_url": "https://hunter.io/api-keys"},
    {"service": "tomba", "auth_kind": "key", "display_name": "Tomba", "needs_extra_credential": True,
     "extra_credential_label": "secret"},
]).encode()


@dataclass
class Case:
    argv: list[str]
    routes: dict[str, list[Canned]] = field(default_factory=dict)
    config: dict = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    stdin: bytes | None = b""
    files: dict[str, bytes] = field(default_factory=dict)
    # Files the command may write beside the config, compared byte for byte afterwards.
    written: tuple[str, ...] = ()
    # Help text is each parser's own layout; it must still carry this text.
    help: str = ""


BASE_CONFIG = {"token": "tok-1", "active_org": "acme", "email": "a@b.c", "identity": False}

CASES: dict[str, Case] = {
    # health
    "health": Case(["health"], {"/health": [Canned(200, {}, b'[{"secret_id": 1, "status": "ok"}]')]}),
    "health_run": Case(["health", "--run"], {"/health/run": [Canned(200, {}, b'{"checked": 2, "failed": 0}')]}),
    "health_refused": Case(["health"], {"/health": [Canned(401, {}, b'{"detail": "invalid token"}')]}),
    # org
    "org_ls": Case(["org", "ls"], {"/orgs": [Canned(200, {}, ORGS)]}),
    "org_ls_override": Case(["--org", "beta-team", "org", "ls"], {"/orgs": [Canned(200, {}, ORGS)]}),
    "org_ls_json": Case(["--json", "org", "ls"], {"/orgs": [Canned(200, {}, ORGS)]}),
    "org_ls_refused": Case(["org", "ls"], {"/orgs": [Canned(403, {}, b'{"detail": "agent keys cannot list teams"}')]}),
    "org_use_identity": Case(["org", "use", "beta-team"], {"/orgs": [Canned(200, {}, ORGS)], "/auth/cli-token": [DEFAULT_KEY]},
                             config={"identity": True}),
    "org_use_not_member": Case(["org", "use", "nope"], {"/orgs": [Canned(200, {}, ORGS)]}, config={"identity": True}),
    "org_use_no_teams": Case(["org", "use", "nope"], {"/orgs": [Canned(200, {}, b"[]")]}),
    "org_use_key_switch": Case(["org", "use", "beta-team"], {"/orgs": [Canned(200, {}, ORGS)]}),
    "org_use_key_same": Case(["org", "use", "acme"], {"/orgs": [Canned(200, {}, ORGS)]}),
    "org_use_unverified": Case(["org", "use", "acme"], {"/orgs": [Canned(500, {}, b"down")]}),
    "org_use_exchange_refused": Case(["org", "use", "beta-team"], {
        "/orgs": [Canned(200, {}, ORGS)],
        "/auth/cli-token": [Canned(403, {}, b'{"detail": "this team requires SSO"}')]}, config={"identity": True}),
    "org_use_disabled_key": Case(["org", "use", "beta-team"], {
        "/orgs": [Canned(200, {}, ORGS)],
        "/auth/cli-token": [Canned(200, {}, b'{"org": "beta-team", "token": "k", "default_key_state": "disabled"}')]},
        config={"identity": True}),
    "org_create": Case(["org", "create", "New Team"], {"/orgs": [Canned(200, {}, json.dumps(
        {"org": "new-team", "org_id": 12, "name": "New Team", "role": "owner", "token": "new-key"}).encode())]}),
    "org_create_json": Case(["--json", "org", "create", "New <Team>"], {"/orgs": [Canned(200, {}, json.dumps(
        {"org": "new-team", "org_id": 12, "name": "New <Team>", "role": "owner", "token": "new-key"}).encode())]}),
    "org_create_refused": Case(["org", "create", "admin"], {"/orgs": [Canned(400, {"X-Treg-Error": "1"},
                                                                             b'{"detail": "that name is reserved"}')]}),
    "org_members": Case(["org", "members"], {"/orgs": [Canned(200, {}, ORGS)],
                                             "/orgs/7/members": [Canned(200, {}, b'[{"user_id": 1, "role": "owner"}]')]}),
    "org_members_override": Case(["--org", "beta-team", "org", "members"], {
        "/orgs": [Canned(200, {}, ORGS)], "/orgs/9/members": [Canned(200, {}, b"[]")]}),
    "org_members_agent_key": Case(["org", "members"], {
        "/orgs": [Canned(403, {}, b'{"detail": "no"}')],
        "/auth/me": [Canned(200, {}, b'{"email": "bot@x", "org_id": 7, "org": "acme", "role": "member"}')],
        "/orgs/7/members": [Canned(403, {"X-Treg-Error": "1"}, b'{"detail": "admins only"}')]}),
    "org_members_signed_out": Case(["org", "members"], {
        "/orgs": [Canned(401, {}, b'{"detail": "invalid token"}')], "/auth/me": [Canned(401, {}, b'{"detail": "no session"}')]}),
    "org_members_no_team": Case(["org", "members"], {"/orgs": [Canned(200, {}, b"[]")]}),
    "org_agents": Case(["org", "agents"], {"/orgs": [Canned(200, {}, ORGS)], "/orgs/7/agents": [Canned(200, {}, b'[{"user_id": 5}]')]}),
    "org_agent_rm": Case(["org", "agent-rm", "007"], {"/orgs": [Canned(200, {}, ORGS)], "/orgs/7/agents/7": [OK]}),
    "org_agent_rm_bad_id": Case(["org", "agent-rm", "seven"]),
    "org_agent_new": Case(["org", "agent-new", "ci-bot", "--tools", "stripe, gh,,", "--cap", "500", "--role", "viewer",
                           "--local-run", "off", "--projects", "apollo,2", "--pin", "Customer = cust_A", "--pin", "ws=9"],
                          {"/orgs": [Canned(200, {}, ORGS)], "/orgs/7/agents": [Canned(200, {}, b'{"token": "agent-tok"}')]}),
    "org_agent_new_defaults": Case(["org", "agent-new", "ci-bot", "--all-tools", "--all-projects"],
                                   {"/orgs": [Canned(200, {}, ORGS)], "/orgs/7/agents": [Canned(200, {}, b'{"token": "t"}')]}),
    "org_agent_new_no_tty": Case(["org", "agent-new", "ci-bot"],
                                 {"/orgs": [Canned(200, {}, ORGS)], "/orgs/7/agents": [Canned(200, {}, b'{"token": "t"}')]}),
    "org_agent_new_bad_pin": Case(["org", "agent-new", "ci-bot", "--all-tools", "--pin", "customer"],
                                  {"/orgs": [Canned(200, {}, ORGS)]}),
    "org_agent_new_bad_role": Case(["org", "agent-new", "ci-bot", "--role", "owner"]),
    "org_agent_new_bad_cap": Case(["org", "agent-new", "ci-bot", "--cap", "lots"]),
    "org_bare": Case(["org"]),
    "org_unknown": Case(["org", "frobnicate"]),
    # tool
    "tool_ls": Case(["tool", "ls"], {"/tools": [Canned(200, {}, TOOLS)]}),
    "tool_add_secret": Case(["tool", "add", "stripe", "--base-url", "https://api.stripe.com", "--secret", "4",
                             "--auth-format", "Basic {secret}", "--health", '{"path": "v1/balance", "n": 1.50}'],
                            {"/tools": [Canned(200, {}, b'{"id": 5, "name": "stripe"}')]}),
    "tool_add_plain": Case(["tool", "add", "open", "--base-url", "https://api.example.com"],
                           {"/tools": [Canned(200, {}, b'{"id": 6}')]}),
    "tool_add_binds": Case(["tool", "add", "ads", "--base-url", "https://api.x.com", "--secret", "9", "--bind", "secret=1",
                            "--bind", "secret_id= 2 ,name=developer-token, format={secret},location=query"],
                           {"/tools": [Canned(200, {}, b'{"id": 7}')]}),
    "tool_add_raw_binding": Case(["tool", "add", "ads", "--base-url", "https://api.x.com",
                                  "--binding", '{"secret_id": 1, "name": "X-Key", "format": "{secret}", "note": "ü<&>"}'],
                                 {"/tools": [Canned(200, {}, b'{"id": 8}')]}),
    "tool_add_bad_binding": Case(["tool", "add", "ads", "--base-url", "https://x", "--binding", '{"secret_id": 1,}']),
    "tool_add_bad_health": Case(["tool", "add", "ads", "--base-url", "https://x", "--health", "{path: me}"]),
    "tool_add_bind_no_secret": Case(["tool", "add", "ads", "--base-url", "https://x", "--bind", "name=X"]),
    "tool_add_bind_bad_id": Case(["tool", "add", "ads", "--base-url", "https://x", "--bind", "secret=abc"]),
    "tool_add_bind_unknown": Case(["tool", "add", "ads", "--base-url", "https://x", "--bind", "secret=1,header=X"]),
    "tool_add_no_base": Case(["tool", "add", "ads"]),
    "tool_add_refused": Case(["tool", "add", "x", "--base-url", "http://localhost"],
                             {"/tools": [Canned(422, {"X-Treg-Error": "1"}, b'{"detail": "base_url must be public"}')]}),
    "tool_rm": Case(["tool", "rm", "2"], {"/tools/2": [Canned(200, {}, b'{"deleted": 2}')]}),
    "tool_rm_missing": Case(["tool", "rm", "99"], {"/tools/99": [Canned(404, {"X-Treg-Error": "1"}, b'{"detail": "no such tool"}')]}),
    "tool_update": Case(["tool", "update", "2", "--base-url", "https://api.stripe.com/v2", "--health", "null",
                         "--bind", "secret=3"], {"/tools/2": [OK]}),
    "tool_update_local_run": Case(["tool", "update", "2", "--local-run", "on"], {"/tools": [Canned(200, {}, TOOLS)], "/tools/2": [OK]}),
    "tool_update_local_run_new": Case(["tool", "update", "3", "--local-run", "off"], {"/tools": [Canned(200, {}, TOOLS)], "/tools/3": [OK]}),
    "tool_update_local_run_missing": Case(["tool", "update", "4", "--local-run", "on"], {"/tools": [Canned(200, {}, TOOLS)]}),
    "tool_update_local_run_refused": Case(["tool", "update", "4", "--local-run", "on"],
                                          {"/tools": [Canned(403, {"X-Treg-Error": "1"}, b'{"detail": "viewer"}')]}),
    "tool_update_nothing": Case(["tool", "update", "2"]),
    "tool_update_bad_local_run": Case(["tool", "update", "2", "--local-run", "maybe"]),
    # secret
    "secret_ls": Case(["secret", "ls"], {"/secrets": [Canned(200, {}, b'[{"id": 1, "name": "K", "kind": "env"}]')]}),
    "secret_rm": Case(["secret", "rm", "+4"], {"/secrets/4": [Canned(200, {}, b'{"deleted": 4}')]}),
    "secret_update": Case(["secret", "update", "4", "--value", "new & <v>", "--kind", "oauth", "--name", ""],
                          {"/secrets/4": [OK]}),
    "secret_update_nothing": Case(["secret", "update", "4"]),
    "secret_update_bad_kind": Case(["secret", "update", "4", "--kind", "pem"]),
    "secret_add_value": Case(["secret", "add", "STRIPE_KEY", "--value", "sk_live_ü&<x> "],
                             {"/secrets": [Canned(200, {}, b'{"id": 4, "name": "STRIPE_KEY"}')]}),
    "secret_add_empty_value": Case(["secret", "add", "K", "--value", ""], {"/secrets": [Canned(200, {}, b'{"id": 5}')]}),
    "secret_add_env_var": Case(["secret", "add", "AHREFS", "--env-var", "AHREFS_API_KEY"],
                               {"/secrets": [Canned(200, {}, b'{"id": 6}')]},
                               files={".env": b"# keys\r\nexport AHREFS_API_KEY = \"quoted value\" \r\nAHREFS_API_KEY_2=x\n"
                                              b"  EXPORT  OTHER='y'\nAHREFS_API_KEY='last ''wins'\n"}),
    "secret_add_env_file": Case(["secret", "add", "K", "--env-var", "OTHER", "--env-file", "conf/prod.env", "--kind", "cli_auth"],
                                {"/secrets": [Canned(200, {}, b'{"id": 7}')]},
                                files={"conf/prod.env": b"OTHER = 'single'\nEMPTY=\n"}),
    "secret_add_env_var_empty": Case(["secret", "add", "K", "--env-var", "EMPTY", "--env-file", "conf/prod.env"],
                                     files={"conf/prod.env": b"EMPTY=\"\"\n"}),
    "secret_add_env_var_missing_file": Case(["secret", "add", "K", "--env-var", "X", "--env-file", "nope.env"]),
    "secret_add_file": Case(["secret", "add", "gcp", "--file", "creds.json", "--kind", "secret_file"],
                            {"/secrets": [Canned(200, {}, b'{"id": 8}')]},
                            files={"creds.json": b'\n  {"token": "t",\r\n "x": 1}\n\n'}),
    "secret_add_file_missing": Case(["secret", "add", "gcp", "--file", "nope.json"]),
    "secret_add_nothing": Case(["secret", "add", "K"]),
    "secret_add_bad_kind": Case(["secret", "add", "K", "--value", "v", "--kind", "pem"]),
    "secret_add_dir_env": Case(["secret", "add", "TOKEN", "--dir", "./skill/"], {"/secrets": [Canned(200, {}, b'{"id": 9}')]},
                               files={"skill/.secret/token": b"tok-value\n", "skill/.secret/client.json": b'{"client_id": "c"}'}),
    "secret_add_dir_sync_new": Case(["secret", "add", "GADS", "--dir", "skill", "--kind", "oauth"],
                                    {"/secrets": [Canned(200, {}, b'{"id": 10}')]},
                                    files={"skill/.secrets/token.json": b'{"refresh_token": "r", "access_token": "a"}',
                                           "skill/treg.json": json.dumps({"name": "ads", "base_url": "https://x", "ü": "é",
                                                                          "n": 1.0, "big": 10**20, "secrets": []},
                                                                         ensure_ascii=False).encode()},
                                    written=("skill/treg.json",)),
    "secret_add_dir_sync_existing": Case(["secret", "add", "GADS2", "--dir", "skill", "--kind", "secret_file"],
                                         {"/secrets": [Canned(200, {}, b'{"id": 11}')]},
                                         files={"skill/.secret/token.json": b'{"token": "t"}',
                                                "skill/treg.json": b'{"secrets": [{"name": "old", "file": ".secret/token.json", '
                                                                   b'"kind": "env", "keep": [1, 2]}], "name": "s"}'},
                                         written=("skill/treg.json",)),
    "secret_add_dir_sync_no_secrets": Case(["secret", "add", "GADS3", "--dir", "skill", "--kind", "oauth"],
                                           {"/secrets": [Canned(200, {}, b'{"id": 12}')]},
                                           files={"skill/.secret/t.json": b'{"refresh_token": "r"}',
                                                  "skill/treg.json": b'{"name": "s"}'},
                                           written=("skill/treg.json",)),
    "secret_add_dir_sync_malformed": Case(["secret", "add", "G", "--dir", "skill", "--kind", "oauth"],
                                          {"/secrets": [Canned(200, {}, b'{"id": 13}')]},
                                          files={"skill/.secret/t.json": b'{"refresh_token": "r"}',
                                                 "skill/treg.json": b'{"name": "s",\n "secrets": [}'},
                                          written=("skill/treg.json",)),
    "secret_add_dir_refused": Case(["secret", "add", "G", "--dir", "skill", "--kind", "oauth"],
                                   {"/secrets": [Canned(422, {"X-Treg-Error": "1"}, b'{"detail": "sandbox limit"}')]},
                                   files={"skill/.secret/t.json": b'{"refresh_token": "r"}', "skill/treg.json": b"{}"},
                                   written=("skill/treg.json",)),
    "secret_add_dir_none": Case(["secret", "add", "G", "--dir", "skill"], files={"skill/SKILL.md": b"# s"}),
    "secret_add_dir_no_kind": Case(["secret", "add", "G", "--dir", "skill", "--kind", "oauth"],
                                   files={"skill/.secret/b": b"x", "skill/.secret/a.json": b'{"client_secret": "s"}'}),
    "secret_add_dir_ambiguous": Case(["secret", "add", "G", "--dir", "skill"],
                                     files={"skill/.secret/b": b"x", "skill/.secrets/a": b"y", "skill/.secret/c.json": b"{}"}),
    # connections
    "connections_bare": Case(["connections"], {"/connections": [Canned(200, {}, b'[{"id": 12, "provider": "gsc"}]')]}),
    "connections_ls": Case(["connections", "ls"], {"/connections": [Canned(200, {}, b"[]")]}),
    "connections_unknown": Case(["connections", "frob"]),
    "connections_providers": Case(["connections", "providers"], {"/oauth/providers": [Canned(200, {}, PROVIDERS)]}),
    "oauth_providers": Case(["oauth", "providers"], {"/oauth/providers": [Canned(200, {}, PROVIDERS)]}),
    "oauth_bare": Case(["oauth"]),
    "connections_resources": Case(["connections", "resources", "12"],
                                  {"/connections/12/resources": [Canned(200, {}, b'[{"ref": "sc-domain:x.com"}]')]}),
    "connections_use": Case(["connections", "use", "12", "sc-domain:example.com"], {"/connections/12/resource": [OK]}),
    "connections_rm": Case(["connections", "rm", "12"], {"/connections/12": [Canned(404, {"X-Treg-Error": "1"}, b'{"detail": "gone"}')]}),
    "connect_oauth": Case(["connections", "connect", "gsc-main", "--provider", "google-search-console", "--capability", "write"], {
        "/oauth/providers": [Canned(200, {}, PROVIDERS)],
        "/oauth/start": [Canned(200, {}, json.dumps({"state": "st1", "redirect_uri": "https://treg.to/oauth/callback",
                                                     "consent_url": "https://accounts.google.com/o?x=1",
                                                     "connect_guidance": "Pick the property."}).encode())],
        "/oauth/status/st1": [Canned(200, {}, b'{"status": "done", "secret_id": 31, "name": "gsc-main"}')]}),
    "connect_oauth_failed": Case(["oauth", "connect", "--provider", "google-search-console"], {
        "/oauth/providers": [Canned(500, {}, b"down")],
        "/oauth/start": [Canned(200, {}, b'{"state": "st2", "redirect_uri": "r", "consent_url": "c"}')],
        "/oauth/status/st2": [Canned(200, {}, b'{"status": "error", "detail": "access_denied"}')]}),
    "connect_oauth_refused": Case(["connections", "connect", "--provider", "nope"], {
        "/oauth/providers": [Canned(200, {}, PROVIDERS)],
        "/oauth/start": [Canned(404, {"X-Treg-Error": "1"}, b'{"detail": "unknown provider nope"}')]}),
    "connect_nothing": Case(["connections", "connect"]),
    "connect_key_stdin_not_pasted": Case(["connections", "connect", "--provider", "google-search-console", "--key-stdin"],
                                         {"/oauth/providers": [Canned(200, {}, PROVIDERS)]}),
    "connect_pasted_stdin": Case(["connections", "connect", "--provider", "hunter", "--key-stdin"], {
        "/oauth/providers": [Canned(200, {}, PROVIDERS)],
        "/connections/token": [Canned(200, {}, b'{"id": 40, "name": "hunter"}')]}, stdin=b"  hk_123 \r\n"),
    "connect_pasted_no_tty": Case(["connections", "connect", "--provider", "hunter"],
                                  {"/oauth/providers": [Canned(200, {}, PROVIDERS)]}, stdin=b"idle"),
    "connect_pasted_empty": Case(["connections", "connect", "--provider", "hunter", "--key-stdin"],
                                 {"/oauth/providers": [Canned(200, {}, PROVIDERS)]}, stdin=b" \n"),
    "connect_pasted_extra_args": Case(["connections", "connect", "my-name", "--provider", "hunter", "--key-stdin"],
                                      {"/oauth/providers": [Canned(200, {}, PROVIDERS)]}, stdin=b"k"),
    "connect_pasted_refused": Case(["connections", "connect", "--provider", "hunter", "--key-stdin"], {
        "/oauth/providers": [Canned(200, {}, PROVIDERS)],
        "/connections/token": [Canned(400, {"X-Treg-Error": "1"}, b'{"detail": "the key did not verify"}')]}, stdin=b"bad"),
    "connect_pasted_accepted_not_ok": Case(["connections", "connect", "--provider", "hunter", "--key-stdin"], {
        "/oauth/providers": [Canned(200, {}, PROVIDERS)],
        "/connections/token": [Canned(202, {}, b'{"pending": true}')]}, stdin=b"k"),
    "connect_pair": Case(["connections", "connect", "--provider", "tomba", "--key-stdin"], {
        "/oauth/providers": [Canned(200, {}, PROVIDERS)],
        "/connections/token": [Canned(200, {}, b'{"id": 41, "name": "tomba"}')],
        "/connections/41/extra-credential": [Canned(200, {}, b'{"id": 41, "name": "tomba"}')]},
        stdin=b"\n ta_key \n\n ts_secret \nignored\n"),
    "connect_pair_missing_second": Case(["oauth", "connect", "--provider", "tomba", "--key-stdin"],
                                        {"/oauth/providers": [Canned(200, {}, PROVIDERS)]}, stdin=b"only-key\n"),
    "connect_byo": Case(["connections", "connect", "gsc", "--client-secret", "client.json", "--scopes", "s1", "s2",
                         "--capability", "read"], {
        "/oauth/start": [Canned(200, {}, b'{"state": "st3", "redirect_uri": "r", "consent_url": "c"}')],
        "/oauth/status/st3": [Canned(200, {}, b'{"status": "done", "secret_id": 50, "name": "gsc"}')]},
        files={"client.json": json.dumps({"installed": {"client_id": "cid", "client_secret": "cs",
                                                        "token_uri": None}}).encode()}),
    "connect_byo_scopes_first": Case(["connections", "connect", "--client-secret", "client.json", "--scopes", "s1", "gsc"],
                                     files={"client.json": b'{"web": {"client_id": "cid", "client_secret": "cs"}}'}),
    "connect_byo_bad_json": Case(["connections", "connect", "gsc", "--client-secret", "client.json"],
                                 files={"client.json": b'{"web": '}),
    "connect_byo_missing_file": Case(["connections", "connect", "gsc", "--client-secret", "nope.json"]),
    "connect_byo_not_google": Case(["connections", "connect", "gsc", "--client-secret", "client.json"],
                                   files={"client.json": b'{"other": {"client_id": "x"}}'}),
    "connect_scopes_empty": Case(["connections", "connect", "gsc", "--client-secret", "c.json", "--scopes"]),
    # resources
    "resources_list": Case(["resources", "list", "--provider", "fishaudio", "--kind", "voice", "--include-deleted"],
                           {"/orgs": [Canned(200, {}, ORGS)], "/orgs/7/provider-resources": [Canned(200, {}, b'{"items": []}')]}),
    "resources_list_plain": Case(["resources", "list"],
                                 {"/orgs": [Canned(200, {}, ORGS)], "/orgs/7/provider-resources": [Canned(200, {}, b"[]")]}),
    "resources_list_no_team": Case(["resources", "list"], {"/orgs": [Canned(200, {}, b"[]")]}),
    # calls and audit
    "calls": Case(["calls", "--limit", "5"], {"/calls": [Canned(200, {}, CALLS)]}),
    "audit": Case(["audit"], {"/calls": [Canned(200, {}, CALLS)], "/runs": [Canned(200, {}, RUNS)]}),
    "audit_limit": Case(["audit", "--limit", "2"], {"/calls": [Canned(200, {}, CALLS)], "/runs": [Canned(200, {}, RUNS)]}),
    "audit_negative_limit": Case(["audit", "--limit", "-2"], {"/calls": [Canned(200, {}, CALLS)], "/runs": [Canned(200, {}, RUNS)]}),
    "audit_calls": Case(["audit", "--calls"], {"/calls": [Canned(200, {}, CALLS)]}),
    "audit_runs": Case(["audit", "--runs", "--limit", "3"], {"/runs": [Canned(200, {}, RUNS)]}),
    "audit_both": Case(["audit", "--runs", "--calls"]),
    "audit_refused": Case(["audit"], {"/calls": [Canned(200, {}, CALLS)],
                                      "/runs": [Canned(403, {"X-Treg-Error": "1"}, b'{"detail": "admins only"}')]}),
    # review
    "review": Case(["review", " call_1 ", "useful", "--reason", "  Helped.  "],
                   {"/reviews": [Canned(201, {}, json.dumps({"review_id": 3, "status": "received", "note": "ü"}).encode())]}),
    "review_no_reason": Case(["review", "c1", "not_sure"], {"/reviews": [Canned(201, {}, b'{"review_id": 4}')]}),
    "review_bad_id": Case(["review", "bad id!", "useful"]),
    "review_bad_reason": Case(["review", "c1", "useful", "--reason", "   "]),
    "review_bad_usefulness": Case(["review", "c1", "great"]),
    "review_own_tool": Case(["review", "c1", "partly"], {"/reviews": [Canned(400, {}, b'{"detail": "echo <input>"}')]}),
    "review_not_found": Case(["review", "c1", "partly"], {"/reviews": [Canned(404, {}, b"{}")]}),
    "review_server_error": Case(["review", "c1", "partly"], {"/reviews": [Canned(502, {}, b"bad gateway")]}),
    "review_not_json": Case(["review", "c1", "partly"], {"/reviews": [Canned(201, {}, b"<html>")]}),
    # feedback
    "feedback_submit": Case(["feedback", "submit", "friction", "The pagination example is unclear. ü",
                             "--call-id", "c1", "--call-id", "c2", "--endpoint-id", "x.y"],
                            {"/feedback": [Canned(201, {}, b'{"feedback_id": 9, "status": "received"}')]}),
    "feedback_shorthand": Case(["feedback", "quality", "outdated", "--call-id", "c3"],
                               {"/feedback": [Canned(201, {}, b'{"feedback_id": 10, "status": "received"}')]}),
    "feedback_stdin": Case(["feedback", "submit", "other", "-"],
                           {"/feedback": [Canned(201, {}, b'{"feedback_id": 11}')]}, stdin=b"line one\r\nline two\n"),
    "feedback_too_long": Case(["feedback", "submit", "other", "x" * 2001]),
    "feedback_blank": Case(["feedback", "other", "   "]),
    "feedback_rate_limited": Case(["feedback", "submit", "other", "hi"], {"/feedback": [Canned(429, {}, b'{"detail": "slow"}')]}),
    "feedback_invalid": Case(["feedback", "submit", "other", "hi"], {"/feedback": [Canned(422, {}, b'{"detail": "echo"}')]}),
    "feedback_server_error": Case(["feedback", "submit", "other", "hi"], {"/feedback": [Canned(500, {}, b"")]}),
    "feedback_bad_category": Case(["feedback", "submit", "praise", "hi"]),
    "feedback_unknown": Case(["feedback", "praise", "hi"]),
    "feedback_bare": Case(["feedback"], help="Proactively share problems and suggestions about treg."),
    # host
    "host": Case(["host", "face.JPG"], {"/media": [Canned(201, {}, json.dumps({
        "url": "https://treg.to/m/abc", "token": "abc", "content_type": "image/jpeg", "size": 9,
        "expires_at": "2026-11-14T00:00:00Z"}).encode())]}, files={"face.JPG": b"\xff\xd8jpegdata"}),
    "host_json": Case(["--json", "host", "voice.mp3"], {"/media": [Canned(201, {}, b'{"url": "u", "size": 3}')]},
                      files={"voice.mp3": b"ID3"}),
    "host_override": Case(["host", "clip.bin", "--content-type", "video/mp4"],
                          {"/media": [Canned(201, {}, b'{"url": "u", "content_type": "video/mp4", "size": 2, "expires_at": "e"}')]},
                          files={"clip.bin": b"\x00\x01"}),
    "host_unknown_type": Case(["host", "notes.qqq"], files={"notes.qqq": b"x"}),
    "host_missing": Case(["host", "./dir//missing.png"]),
    "host_refused": Case(["host", "big.png"], {"/media": [Canned(413, {"X-Treg-Error": "1"}, b'{"detail": "too big"}')]},
                         files={"big.png": b"png"}),
    # usage
    "usage": Case(["usage", "--by", "workspace", "--days", "7"], {"/orgs": [Canned(200, {}, ORGS)], "/orgs/7/usage/by-tag": [
        Canned(200, {}, json.dumps({"key": "workspace", "days": 7, "rows": [
            {"value": "ws_1", "charged_micro": 1234567890, "calls": 1},
            {"value": "ws_ü", "charged_micro": 600, "calls": 12}],
            "unattributed_micro": 42, "total_micro": 1234568532}).encode())]}),
    "usage_empty": Case(["usage"], {"/orgs": [Canned(200, {}, ORGS)], "/orgs/7/usage/by-tag": [
        Canned(200, {}, b'{"key": "customer", "days": 30, "rows": [], "unattributed_micro": 0, "total_micro": 0}')]}),
    "usage_json": Case(["--json", "usage"], {"/orgs": [Canned(200, {}, ORGS)], "/orgs/7/usage/by-tag": [Canned(200, {}, b'{"key": "c"}')]}),
    "usage_no_team": Case(["usage"], {"/orgs": [Canned(200, {}, b"[]")]}),
    # topup
    "topup": Case(["topup", "100"], {
        "/billing/topup": [Canned(200, {}, b'{"amount_micro": 100000000, "url": "https://checkout.stripe.com/x"}')],
        "/billing": [Canned(200, {}, b'{"topup": {"bonus_tiers": {"50": 5, "100": 10, "250": 15}}}')]}),
    "topup_default": Case(["topup"], {
        "/billing/topup": [Canned(200, {}, b'{"amount_micro": 20000000, "url": "https://checkout.stripe.com/y"}')],
        "/billing": [Canned(200, {}, b'{"topup": {"bonus_tiers": {"50": 5}}}')]}),
    "topup_fraction": Case(["topup", "1e1"], {
        "/billing/topup": [Canned(200, {}, b'{"amount_micro": 10500000, "url": "u"}')],
        "/billing": [Canned(503, {}, b'{"detail": "billing is not configured"}')]}),
    "topup_json": Case(["--json", "topup", "25.5"], {"/billing/topup": [Canned(200, {}, b'{"amount_micro": 25500000, "url": "u"}')]}),
    "topup_refused": Case(["topup", "0.5"], {"/billing/topup": [Canned(400, {"X-Treg-Error": "1"}, b'{"detail": "minimum is $5"}')]}),
    "topup_bad_amount": Case(["topup", "lots"]),
    "topup_auto_off": Case(["topup", "--auto", "off"], {"/billing/autotopup": [Canned(200, {}, b'{"autotopup": {"enabled": false}}')]}),
    "topup_auto_off_json": Case(["--json", "topup", "--auto", "off", "--cap", "0"],
                                {"/billing/autotopup": [Canned(200, {}, b'{"autotopup": {"enabled": false}}')]}),
    "topup_auto_bad": Case(["topup", "--auto", "maybe"]),
    # global flags, environment and registry-wide answers
    "org_override_exchange": Case(["--org", "beta-team", "org", "members"], {
        "/auth/cli-token": [DEFAULT_KEY], "/orgs": [Canned(200, {}, ORGS)],
        "/orgs/9/members": [Canned(200, {}, b'[{"user_id": 2}]')]}, config={"identity": True}),
    "org_override_unknown": Case(["--org", "nope", "secret", "ls"], {
        "/auth/cli-token": [Canned(403, {}, b'{"detail": "you are not a member of nope"}')]}, config={"identity": True}),
    "treg_url": Case(["secret", "ls"], {"/secrets": [Canned(200, {}, b"[]")]}, env={"TREG_URL": "{fake}"},
                     config={"base_url": "http://127.0.0.1:9"}),
    "treg_url_org_create": Case(["org", "create", "X"], {"/orgs": [Canned(200, {}, b'{"org": "x", "token": "kx"}')]},
                                env={"TREG_URL": "{fake}"}, config={"base_url": "http://127.0.0.1:9"}),
    "treg_token": Case(["org", "create", "X"], {"/orgs": [Canned(200, {}, b'{"org": "x", "token": "kx"}')]},
                       env={"TREG_TOKEN": "env-token", "TREG_ORG": "env-org"}),
    "treg_token_rejected": Case(["tool", "ls"], {"/tools": [Canned(401, {"X-Treg-Error": "1"}, b'{"detail": "invalid token"}')]},
                                env={"TREG_TOKEN": "env-token"}),
    "saturated": Case(["tool", "ls"], {"/tools": [Canned(503, {"Retry-After": "3"}, b'{"treg_saturated": true}')]}),
    "html_error": Case(["org", "ls"], {"/orgs": [Canned(502, {"Content-Type": "text/html"}, b"<html>bad gateway</html>")]}),
    "client_notice": Case(["secret", "ls"], {"/secrets": [Canned(200, {"X-Treg-Client-Notice": "treg 9.9.9 is available."}, b"[]")]}),
    "client_notice_json": Case(["--json", "secret", "ls"],
                               {"/secrets": [Canned(200, {"X-Treg-Client-Notice": "treg 9.9.9 is available."}, b"[]")]}),
    "waf_retry": Case(["secret", "add", "Q", "--value", "' OR 1=1; DROP TABLE x; --"], {"/secrets": [
        Canned(403, {"Content-Type": "text/html"}, b"<html>blocked</html>"), Canned(200, {}, b'{"id": 1}')]}),
    "balance_refusal": Case(["topup", "5"], {"/billing/topup": [Canned(402, {"X-Treg-Error": "1"},
                                                                         b'{"detail": {"error": "insufficient_balance"}}')]}),
    "choose_org": Case(["secret", "ls"], {"/secrets": [Canned(400, {"X-Treg-Error": "1"}, b'{"detail": "choose an org (send X-Treg-Org)"}')]}),
    # more edges
    "org_agent_new_empty_tools": Case(["org", "agent-new", "bot", "--tools", ","],
                                      {"/orgs": [Canned(200, {}, ORGS)], "/orgs/7/agents": [Canned(200, {}, b'{"token": "t"}')]}),
    "connect_oauth_flaky_poll": Case(["connections", "connect", "--provider", "google-search-console"], {
        "/oauth/providers": [Canned(200, {}, PROVIDERS)],
        "/oauth/start": [Canned(200, {}, b'{"state": "s4", "redirect_uri": "r", "consent_url": "c"}')],
        "/oauth/status/s4": [Canned(502, {}, b"<html>"), Canned(200, {}, b'{"status": "pending"}'),
                             Canned(200, {}, b'{"status": "done", "secret_id": 1, "name": null}')]}),
    "review_long_reason": Case(["review", "c1", "useful", "--reason", "é" * 201]),
    "review_max_reason": Case(["review", "c1", "useful", "--reason", "é" * 200], {"/reviews": [Canned(201, {}, b"{}")]}),
    "feedback_max_unicode": Case(["feedback", "submit", "other", "é" * 2000], {"/feedback": [Canned(201, {}, b"{}")]}),
    "usage_bad_days": Case(["usage", "--days", "week"]),
    "audit_bad_limit": Case(["audit", "--limit", "1.5"]),
    "secret_rm_bad_id": Case(["secret", "rm", "4x"]),
    "tool_update_bad_health": Case(["tool", "update", "2", "--health", "{\"path\": \"x\"\n"]),
    "topup_inf": Case(["topup", " 1_000.5 "], {"/billing/topup": [Canned(200, {}, b'{"amount_micro": 1000500000, "url": "u"}')],
                                               "/billing": [Canned(200, {}, b'{"topup": {"bonus_tiers": {"x": 1}}}')]}),
}


def _home(tmp_path, name: str, url: str, case: Case):
    home = tmp_path / name
    write_config(home, **({"base_url": url} | BASE_CONFIG | case.config))
    for rel, data in case.files.items():
        path = home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return home


def _run(fake, tmp_path, label: str, cli: list[str], case: Case):
    case = copy.copy(case)
    case.env = {k: v.replace("{fake}", fake.url) for k, v in case.env.items()}
    fake.routes = copy.deepcopy(case.routes)
    fake.route("*", Canned(599, {}, b'{"detail": "unexpected request"}'))
    start = len(fake.seen)
    home = _home(tmp_path, label, fake.url, case)
    result = run(cli + case.argv, home=home, env=case.env, stdin=case.stdin)
    files = {rel: (home / rel).read_bytes() for rel in case.written}
    config = json.loads((home / ".treg" / "config.json").read_text())
    return result, fake.seen[start:], config, files


IGNORED = {"user-agent", "accept-encoding", "connection", "host", "x-treg-client-version", "x-treg-session", "x-treg-agent"}


def _requests(seen):
    return [(s.method, s.path, {k: v for k, v in s.headers.items() if k not in IGNORED}, s.body) for s in seen]


def _err(text: str) -> str:
    """A usage error is the parser's own wording in each CLI; the status (2) says which kind it is."""
    return normalize(text)


@pytest.mark.parametrize("name", sorted(CASES))
def test_command(fake, tmp_path, go_bin, name):
    case = CASES[name]
    py, py_seen, py_cfg, py_files = _run(fake, tmp_path, "py", PYTHON_CLI, case)
    go, go_seen, go_cfg, go_files = _run(fake, tmp_path, "go", [str(go_bin)], case)
    assert py.code == go.code, (py, go)
    if case.help:
        assert case.help in py.text and case.help in go.text, (py.out, go.out)
    else:
        assert same_stdout(py, go), (py.out, go.out)
    if py.code == 2:
        assert go.err.startswith("treg: error: "), go.err
    elif "Traceback (most recent call last):" in py.err:
        # The Python CLI crashed with this exception; the Go CLI names it in one line.
        assert normalize(go.err) == normalize("treg: " + py.err.strip().splitlines()[-1] + "\n"), (py.err, go.err)
    else:
        assert _err(py.err) == _err(go.err), (py.err, go.err)
    assert _requests(py_seen) == _requests(go_seen)
    assert py_cfg == go_cfg
    assert py_files == go_files


@pytest.mark.parametrize("argv", [["secret", "ls"], ["org", "members"], ["audit"], ["feedback", "other", "hi"],
                                  ["review", "c1", "useful"]])
def test_unreachable_registry(tmp_path, go_bin, argv):
    """The Python CLI crashes on a refused connection (feedback and review print their own JSON);
    the Go CLI names the registry it could not reach, with the same status."""
    case = Case(argv, config={"base_url": "http://127.0.0.1:9"}, env={"NO_PROXY": "*"})
    py = run(PYTHON_CLI + argv, home=_home(tmp_path, "py", "", case), env=case.env, stdin=b"")
    go = run([str(go_bin)] + argv, home=_home(tmp_path, "go", "", case), env=case.env, stdin=b"")
    assert py.code == go.code == 1, (py, go)
    if "Traceback" in py.err:
        assert "ConnectError" in py.err and go.text == ""
        assert go.err.startswith("treg: could not reach http://127.0.0.1:9: "), go.err
    else:
        assert (py.out, py.err) == (go.out, go.err)
