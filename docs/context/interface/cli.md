---
title: The CLI (treg) + skill scaffolding
status: shipped
sources:
  - src/treg/cli.py
  - src/treg/cli_view.py
  - src/treg/application/home.py
  - tests/test_released_cli_compat.py
  - tests/test_cli_key_compatibility.py
  - src/treg/routers/auth_helpers.py
  - src/treg/cli_analytics.py
  - src/treg/cli_session.py
  - src/treg/convert.py
  - src/treg/agents.py
  - src/treg/routers/api_keys.py
  - tests/test_api_keys.py
related:
  - interface/api.md
  - interface/skill.md
---

# The `treg` CLI

A thin client over the API in `src/treg/cli.py` (stdlib `argparse` plus `httpx`): every command is
HTTP calls with no business logic of its own. `USAGE.md` and `treg <cmd> -h` are the command
reference; every command carries a description, per-argument help and an Examples epilog, so help
is self-teaching. This fragment holds only what the code and help cannot say.

## Front page and routing

- **Bare `treg` is the home view.** Nearly every caller is an agent, so `treg` with no arguments
  answers with live state (`GET /home`, `application/home.py`) instead of a usage error that costs a
  turn on `--help`. Each read stands alone: one that fails leaves out its own line and the command
  always exits 0. A blocker (no registry at that URL, a 5xx, not signed in, a stale active team, no
  team, an empty balance) leads the `help` list. A registry older than `/home` (404) gets separate
  reads (`_home_state_by_parts`).
- **`treg --help`** renders the curated `HELP_GROUPS` table, not argparse's alphabetical list. A
  command absent from every group still parses; that is how back-compat aliases (`treg add`,
  `oauth connect`, `run`, `runs`, `calls`, `import`, `setup-local-run`, ...) survive hidden. Never
  remove an alias: old scripts and cached agent instructions use them. The top-level
  `add_subparsers` pins `parser_class=argparse.ArgumentParser`; otherwise argparse clones the grouped
  parser into every subparser and `treg call -h` prints the front page.
- **`treg <program>`** (`cmd_with`) runs one command with the team's credentials. `main()` routes a
  bare word there only when it is not a treg subcommand and exists on `PATH`
  (`_looks_like_a_program`), so a typo stays an argparse error and a stray `call` binary cannot
  shadow `treg call`.
- **`catalog search|get|request`** are positional verbs inside `cmd_catalog`, not argparse
  subcommands, so `treg catalog <platform>` keeps working and a multi-word query needs no quoting.
  The catalog commands are unauthenticated: the catalog is public and worth reading before signing
  up. The CLI never parses catalog YAML (pyyaml is a server extra).
- **`serve start`** detaches a child via `sys.executable`, never the `treg` on `PATH`, which may be an
  older build.

## Output for agents

- **TOON views.** The home view and `catalog` print through `cli_view.render` (the reference
  `toon-format` encoder, pinned to one release because `cli_view` overrides its private table
  decisions). The whole output decodes as TOON, which is how tests read it. Commands and request
  values print unquoted so they copy without unescaping. Nothing is clipped except a list row's
  `name`; ids, prices and notes print whole. A refusal is data too: `_fail` prints `error`, `message`
  and `help` on stdout and exits 1.
- **`--json`** prints the server's JSON as received. On `call` (not `--await`) it prints one envelope,
  `{"result": <body>, "_treg": {...}}` (`_call_envelope`), and suppresses the charge, hint and
  diagnostic stderr lines: a script that merged the streams once discarded every result it had paid
  for. Exit status is unchanged (1 on HTTP >= 400). `--json` and `--org` are stripped in `main` and
  last for one `main()` call.
- **stdout is the exact body.** Everything treg adds goes to stderr: on HTTP >= 400 one diagnostic
  line (`_show_failure_diagnostics`) with the status, whose answer it is (`X-Treg-Error: 1` = treg
  refused, absent = relayed provider answer), the call id and any charge. treg's saturation 503
  carries no `X-Treg-Error` (`api.md`), so the CLI reads its typed `treg_saturated` body; a client
  that checks only the header blames the provider. `X-Treg-Capacity: exhausted` swaps in the shared
  notice from `capacity_notice.py`. With `TREG_TOKEN` set, treg's own 401 adds that the variable
  overrides `treg login`. A metered 2xx gets a charge line (an async submission says "reserved up to",
  since its header is the hold ceiling). A large 2xx sent to a terminal or pipe gets a size line
  before the body, so an agent's output cut keeps it; the line names no JSON path, because a guessed
  one can be wrong.
- **Hints.** `X-Treg-Hint: review|feedback` (older `X-Treg-Review: requested`) prints one stderr line
  per kind. `X-Treg-Routed-Tool` on a provider endpoint's success prints a pointer to the routed
  `treg.<capability>` tool (`_treg.routed_tool` under `--json`).
- **Headers sent.** `_client` sends `X-Treg-Client-Version` (recorded as `client_version` on
  `tool_called`, so output changes compare across releases) and, with telemetry on, `X-Treg-Session`
  and `X-Treg-Agent`. Like every `x-treg-*` header, none reaches the upstream.
- **Edge WAF.** `_RegistryClient` re-sends a request once, base64-encoded with
  `X-Treg-Body-Encoding: base64`, when an edge 403s it with an HTML block page (never treg's own JSON
  403). That is what lets SQL- or HTML-bearing bodies through a CDN.
- **`mcp grants`** prints the grant id whole while clipping every other column: it is the argument
  `mcp use-team` takes. See [mcp-oauth](../architecture/mcp-oauth.md#but-the-choice-must-stay-visible-and-reversible-afterwards).

## Identity and config

- `~/.treg/config.json` (or `TREG_CONFIG`) holds one bearer token and an active org slug.
  `_save_config` writes atomically with mode `0600`; a corrupt file reads as empty so it cannot brick
  every command. `login` persists the token before the best-effort active-org lookup, so a transient
  `/orgs` failure cannot discard a fresh token.
- **`TREG_TOKEN`** (+ optional `TREG_ORG`) beats the config file and never writes it, so each coding
  agent on one machine can run as its own scoped agent while the file stays the human's.
- `_active_org` resolves `/orgs`, falling back to `/auth/me` when `/orgs` refuses an agent key.
  `strict=False` is for callers that only enrich output: `sys.exit` raises `SystemExit`, which
  `except Exception` does not catch, so a call-site try/except would not save the page.
- **Browser login.** `POST /auth/cli/start` returns a `login_id` and a short pairing code; the
  `/login?cli=<id>#code=<code>` page shows the code from the URL fragment (never sent to the server,
  so never in request logs) so the user confirms it matches the terminal. The server still validates
  the code at approve time: a login you did not start cannot be approved into a token.
- **Team Default key, not bootstrap.** An org-less login yields a signed `scp=bootstrap` credential
  that identifies the account for onboarding, expires after seven days and cannot call team
  resources. Selecting, creating, joining or accepting a team replaces the stored token with that
  membership's Default key (the same deterministic key the dashboard shows, so disable and rotation
  apply to CLI use). The email-code login uses the response's HttpOnly browser session for that
  exchange, never the bootstrap credential. `org use` writes the new team and token only after the
  exchange succeeds; an Additional or Agent key cannot switch memberships. The token is what people
  copy out of the CLI (curl, an MCP client, an agent env), so it must carry its team: unpinned, it
  fails there with `choose an org`. A typed key's signed team is authoritative; a conflicting
  `X-Treg-Org` does not override it. The MCP installer refuses a bootstrap credential before writing
  any client configuration. `login --token` verifies through `/auth/me` before saving.
- **Released-CLI compatibility.** Old PyPI CLIs would save a bootstrap token from email login and
  keep the previous token after team create or invite. `routers.auth_helpers.require_managed_cli`
  stops those known requests with HTTP 426 (`X-Treg-Error: 1`, "run `treg update`") before issuing
  email credentials, creating a team or consuming an invite. Current CLIs send
  `X-Treg-Key-Protocol: 1`; the legacy hint is the released CLI's `python-httpx/` User-Agent plus
  `ngrok-skip-browser-warning: 1` without that marker. It is a compatibility check, not an
  authorization boundary: omitting or forging it never relaxes token restrictions.
  `test_released_cli_compat` checks that refusal preserves config bytes and the prior team.

## Calls

- **`call`** has three shapes (`call <tool> <path>`, a full URL, a catalog endpoint id), all to
  `/call/`. The endpoint-id shape walks the credential ladder server-side
  (`_resolve_marketplace_call` in `application/call/resolve.py`; see
  [proxy-model](../architecture/proxy-model.md)). `--header` adds a header the binding cannot know;
  an injected credential always wins. `--query` is relayed as a list of pairs (duplicates survive)
  and fills `{placeholder}` path params. `treg call <catalog-id> <path>` answers 400 naming the
  endpoint's parameter slots.
- **`--await [--timeout]`** reads `X-Treg-Async` (a no-op without it), using descriptor semantics from
  `treg.domain.asynctasks`, never a CLI-side copy. It prints the task id and a resumable command to
  stderr, polls every descriptor `interval` and never past `max_age`, skips polling on
  `terminal_on_submission`, retries network/5xx failures with backoff, and keeps waiting through
  unknown statuses after one warning. Stdout holds only the terminal response bytes; a fetch-mode
  result prints a retrieval command instead of downloading, and running it writes non-text bytes
  untouched (the upstream `Content-Type` stays authoritative). **Exit codes are a contract:** 0
  success, 2 provider terminal failure, 3 timeout, interruption or recoverable polling failure, 1
  malformed usage or metadata.
- **Pasted-key connect.** `connections connect --provider` reads `GET /oauth/providers`; a `key` or
  `token` provider reads the key with `getpass` or, with `--key-stdin`, all of stdin, keeping it off
  the command line. Without a terminal and without `--key-stdin` it exits with that instruction
  rather than reading stdin, because an agent's idle pipe would hang the read. A pair-signed
  provider (`needs_extra_credential`) reads both halves before posting either, so a half-built
  connection is never the outcome. Instagram grant selection is in
  [instagram-oauth](../architecture/instagram-oauth.md).
- **`audit`** merges `/calls` and `/runs` and drops `kind == "local_run"` call records, because
  `/runs` already lists those grants as `where: "local"`.

## Local runs

- **`cli run` guard.** `args` is an `argparse.REMAINDER`, which silently swallows a treg flag typed
  after the tool name. `cmd_run` reads the real `sys.argv` and refuses a tier flag (`--server`,
  `--local`, `--timeout`, `--fs-jail`) after the tool but before `--`, while a flag after `--` still
  reaches the vendor CLI (`treg run db -- --timeout 30`).
- **Runner proof gate.** `cli setup --run-proof` installs `/etc/treg-run/proof` (root-owned, 0400,
  readable only by `treg-run`); the runner exports it as `TREG_RUN_PROOF` and `_run_helper` sends it
  as `X-Treg-Run-Proof` on the grant call. That is how the server releases a shared (non-owned) key
  to the isolated runner but refuses a direct member call; without it only owned-key tools run
  locally. On a shared-key run (`redact_output`), `_StreamRedactor` scrubs the injected value from
  the CLI's output. See [local-run](../architecture/local-run.md).
- **`--server`** sends `POST /run`, which is disabled for every caller and answers 503; `cli shell
  --server-for` therefore never routes a tool to the server.

## Skill scaffolding and the `treg.json` contract (`convert.py`)

`treg.json` lets a skill self-describe its registration; `skill init --dir` writes it and `skill add
--dir` registers it, loading the named secret files at push time so no secret value lives in the
file. `resolve_secret_path` swaps `.secret` and `.secrets` when the exact spelling is absent, so a
shared contract survives per-machine spelling drift. `secret add --dir` syncs back into an existing
`treg.json` so CLI changes stay authoritative. `auto_bindings` (shared with the dashboard) gives each
additional credential its own header so bindings never collide. `skill install` keeps a folder that
already exists unless `--force` and ends with a summary of kept skills; the Access agent instruction
defers to that output. Bundle file paths are re-checked to stay inside the skill dir, and secrets are
never shipped.

## Caller tags and budgets

`org agent-new --pin key=value` pins a tag the holder cannot override, and the pin survives a rotate:
a rotate must never silently unpin a scoped token. Re-running `org agent-new` with the same name
rotates and kills the previous token; scope flags are sent only when given, so a rotate never widens
scope. `org budget-set` writes only the limits named, so `--block` never wipes a cap. Caps are
advisory (concurrent calls can overshoot); the prepaid balance is the hard limit
([money](../architecture/money.md)).

## Anonymous CLI analytics

`main` calls `cli_analytics.track_command` after dispatch (failures and interrupts included), sending
`cli_command_completed` with the command name (`catalog` records its form), exit code, success,
duration, package version, OS, `session`, `agent` and fixed-vocabulary `_note` fields (`error`,
`status_class`, `tool` only when it has an endpoint id's shape). A usage error sends the unrecognized
option names without values. **No other argument, query, body, path, token, email or team identifier
is collected**; keep it that way. An installation is a random UUID in `analytics-id` beside
`TREG_CONFIG`, independent of login.

`cli_session.current` resolves the session once per invocation: `TREG_SESSION_ID`, else the nearest
agent on the process tree (agent variables are inherited by nested agents, so the tree decides which
agent is nearest before its own variable is read: `CLAUDE_CODE_SESSION_ID`, `CODEX_THREAD_ID`, else
the agent's pid and start time), else a reused `session-id` file with an idle timeout; a terminal or
tmux ancestor would join unrelated commands. Machine-derived ids are sent hashed, never raw.

The hosted registry's public ingestion token is the default only for that registry; self-hosted URLs
send nothing unless `TREG_CLI_POSTHOG_KEY` is set. `TREG_TELEMETRY=0|false|off` or `DO_NOT_TRACK=1`
disables analytics, id creation, the process walk and both session headers. Capture runs in a daemon
thread with a short timeout and wait budget; telemetry failures are silent and never change output or
exit status.
