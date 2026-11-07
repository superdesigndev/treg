---
title: The binary CLI (cli/, Go) - compatibility, releases, self-update and usage events
status: building
sources:
  - cli/cmd/treg/main.go
  - cli/internal/agents/agents.go
  - cli/internal/agents/mcp.go
  - cli/internal/api/client.go
  - cli/internal/api/proxy.go
  - cli/internal/api/proxy_darwin.go
  - cli/internal/api/proxy_other.go
  - cli/internal/api/proxy_parse.go
  - cli/internal/api/proxy_windows.go
  - cli/internal/api/url.go
  - cli/internal/api/view.go
  - cli/internal/app/app.go
  - cli/internal/buildinfo/buildinfo.go
  - cli/internal/commands/account.go
  - cli/internal/commands/args.go
  - cli/internal/commands/audit.go
  - cli/internal/commands/await.go
  - cli/internal/commands/balance.go
  - cli/internal/commands/call.go
  - cli/internal/commands/catalog.go
  - cli/internal/commands/connections.go
  - cli/internal/commands/env.go
  - cli/internal/commands/feedback.go
  - cli/internal/commands/feedback_get.go
  - cli/internal/commands/health.go
  - cli/internal/commands/help.go
  - cli/internal/commands/helpers.go
  - cli/internal/commands/home.go
  - cli/internal/commands/host.go
  - cli/internal/commands/mcp.go
  - cli/internal/commands/mediatype.go
  - cli/internal/commands/org.go
  - cli/internal/commands/resources.go
  - cli/internal/commands/review.go
  - cli/internal/commands/root.go
  - cli/internal/commands/secret.go
  - cli/internal/commands/skills.go
  - cli/internal/commands/tool.go
  - cli/internal/commands/topup.go
  - cli/internal/commands/update.go
  - cli/internal/commands/upload.go
  - cli/internal/commands/upload_clis.go
  - cli/internal/commands/upload_env.go
  - cli/internal/commands/upload_llm.go
  - cli/internal/commands/upload_skills.go
  - cli/internal/commands/usage.go
  - cli/internal/config/config.go
  - cli/internal/minisign/minisign.go
  - cli/internal/output/call.go
  - cli/internal/output/output.go
  - cli/internal/output/view.go
  - cli/internal/prompt/hidden_other.go
  - cli/internal/prompt/hidden_unix.go
  - cli/internal/prompt/prompt.go
  - cli/internal/prompt/termios_darwin.go
  - cli/internal/prompt/termios_linux.go
  - cli/internal/providers/providers.go
  - cli/internal/providers/catalog.json
  - cli/internal/pyjson/pyjson.go
  - cli/internal/session/proc_darwin.go
  - cli/internal/session/proc_linux.go
  - cli/internal/session/proc_other.go
  - cli/internal/session/session.go
  - cli/internal/skillscan/cliprofile.go
  - cli/internal/skillscan/skillscan.go
  - cli/internal/telemetry/telemetry.go
  - cli/internal/term/term.go
  - cli/internal/update/detach_unix.go
  - cli/internal/update/detach_windows.go
  - cli/internal/update/legacy.go
  - cli/internal/update/method.go
  - cli/internal/update/update.go
  - cli/scripts/check-beta.sh
  - cli/scripts/release.sh
  - src/treg/application/cli_releases.py
  - src/treg/infra/github_releases.py
  - src/treg/routers/cli_client.py
  - src/treg/web/install-bin.sh
  - src/treg/web/install.ps1
  - tests/cli_go/cli_go_harness.py
  - tests/cli_go/cli_go_live.py
  - tests/cli_go/agent_sandbox.py
related:
  - interface/cli.md
  - interface/api.md
  - ops/deploy.md
---

# The binary CLI

`cli/` is a Go rewrite of `treg`, its own module, built into one static file per platform. The
Python CLI (`src/treg/cli.py`, [cli](cli.md)) stays the reference and is frozen: the binary prints
what it prints for the same registry answers, exits with the same statuses and reads and writes the
same files. It is not the default install yet: `/install.sh` installs the Python CLI and
`/install.sh?channel=beta` installs the binary.

## Compatibility with the Python CLI

- **Config.** `config.Load` / `Save` read and write `config.json` in the Python layout (atomic
  write, `0600`, empty as `null`, a corrupt file reads as fresh, the legacy multi-org format
  migrates). Fields this version does not know are kept in their order, so a newer writer's
  additions survive; `channel` and `auto_update` are the binary's own.
- **Requests.** `api/url.go` reproduces httpx's encoding, the only one the registry and every
  upstream behind `/call/` have seen: dot segments resolved after the path joins the base URL,
  existing `%XX` kept, everything outside httpx's `PATH_SAFE` percent-encoded; the query is
  urlencode over ordered pairs. Headers are the Python CLI's; `User-Agent` is
  `treg/<version> (<os>/<arch>)` (no server code reads the CLI's UA). `TREG_URL` applies to every
  request, the login, MCP and telemetry ones included. The proxy is httpx's: the `*_proxy`
  environment variables when any is set, else the macOS or Windows system proxy with its
  exceptions (`api/proxy.go`); a loopback host is never proxied.
- **Output.** stdout carries the answer and treg's lines go to stderr, as in [cli](cli.md). A JSON
  body is re-indented by two spaces like the Python CLI's, but strings and numbers keep the
  upstream's spelling (the Python CLI escapes non-ASCII as `\uXXXX` and normalizes numbers): the
  parsed values are equal, the bytes are not. treg's own lines use a plain dash where the Python
  CLI wrote an em dash, quote a value as Go does (`"x"`, not Python's `'x'`), name a file it cannot
  read, or a request that got no answer, with the system's reason (`connection refused`, `timed
  out`) rather than Python's errno text or httpx's exception name, write dollars
  with cents, and print nothing (or `-`) where the Python CLI printed an absent value as `None`.
- **Views.** Bare `treg`, `catalog`, `balance` and `feedback get` send `X-Treg-View: 1` and print
  the registry's `text`; `--json` prints its `data` ([api](api.md), "Views"). A registry without
  views gets its JSON printed and one stderr line saying so. The client prints only what the
  registry cannot know: the `treg:` line (version and path) and a `registry:` line off treg.to.
- **`call --await`** follows the registry's task (`/tasks/{call_id}`, [tasks](../architecture/tasks.md))
  and never reads `X-Treg-Async` itself; a registry without the task endpoint gets exit 1 naming it.
- **Bodies and files.** `internal/pyjson` decodes JSON with Go's decoder, keeping key order and
  number spelling, and encodes it as Python does: a request body as httpx's `json=` (compact, UTF-8
  kept), the `treg.json` that `secret add --dir` rewrites as `json.dumps(indent=2)`. A JSON
  argument that does not parse is refused in the decoder's words. A number argument is what Go's
  `strconv` reads: no surrounding spaces, no underscores in an integer, no negative count, where the
  Python CLI took whatever `int()` and `float()` take. A path is named as it was given, not
  normalized.
  `treg host` takes the media type from the extension: its own table for the image, audio and video
  formats vendors fetch, else the system's (`mime.TypeByExtension`).
- **Prompts.** A command asks only where the Python CLI asks, and only a person at a terminal
  (`internal/prompt`): the tool-access checklist of `org agent-new`, a pasted key read without echo
  from the controlling terminal, the auto top-up consent. Without a terminal the access is every
  tool, a pasted-key connect needs `--key-stdin`, and the consent is no, even for a `y` piped in;
  no command reads an agent's idle pipe.
- **Crashes.** Where the Python CLI dies with a traceback (a `--file` that does not exist), the
  binary prints the exception's last line and exits 1.
- **Parity suite.** `tests/cli_go` runs both CLIs against a fake registry (canned status, headers
  and body) and a live one (`python -m treg` on a fresh sqlite database, a signed-up team, an echo
  upstream registered as its own tool) and compares exit status, stdout (JSON parsed), stderr,
  config writes and the request each sent; prompts run on a pseudo-terminal that is each CLI's
  controlling terminal, answered key by key. It runs with `TREG_GO_BIN` (or `TREG_GO_BUILD=1`); the
  `cli-go` CI job runs it, and Go's vet and unit tests on Linux, macOS and Windows.

## Commands that write outside treg

`skill bootstrap|ls|install`, `mcp install|grants|use-team`, `scan`, `upload` and the hidden
`import` write into coding agents' directories and configs, so their parity covers files too:
`tests/cli_go/agent_sandbox.py` runs both CLIs in a fresh HOME with fake agent installs and a PATH
whose `claude` records its arguments, and compares every file left in HOME, modes included.

- **Same bytes.** `internal/pyjson` reads JSON keeping member order (a repeated key keeps its first
  place and last value), so a file the CLI edits keeps its owner's order, and writes it as Python's
  `json.dumps` does (`ensure_ascii`, float `repr`), and files are read with Python's universal
  newlines, so an agent's MCP config, `skills.json` or a skill's `treg.json` is the same whichever
  CLI wrote it. The Python-exact writing goes when the Python CLI does; the order stays.
- **Where agents live.** `internal/agents` holds both tables: each agent's skill directories
  (honouring `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `XDG_CONFIG_HOME`) and the MCP writers. Every path
  is under the home directory, which is `%USERPROFILE%` on Windows.
- **MCP.** The server written is `<registry>/mcp/`, the registry this CLI talks to (`TREG_URL`
  included, where the Python CLI wrote the configured base URL), never the binary's path. Files
  holding a token are replaced atomically and are `0600` from the first byte. Codex's table is cut
  and appended as text, as the Python CLI does, and must parse before it lands.
- **`skill install <name>`** also installs one of the registry's public skills (its well-known
  index, what `skill bootstrap --skills` installs) when the team has no skill of that name; the
  Python CLI refused it as "no skill named ... in this org", which was most `skill install`
  failures, agents asking for `lead-signals` and the like.
- **Provider catalog.** `internal/providers/catalog.json` is the Python CLI's `providers.CATALOG`
  at its `CATALOG_VERSION`, the floor when the registry's `/providers.json` is older or
  unreachable; a test keeps the two equal and prints the regeneration command. The cache is keyed
  by the registry actually used.
- **Prompts.** The checkbox and the credential prompts run only on a terminal, as in the Python
  CLI. Where the Python CLI read stdin regardless (`upload clis --add` without `--env` or
  `--base-url`, the `--llm` confirmations), the binary does not read an idle pipe: `--add` refuses
  and names the flags, and each LLM suggestion is declined as on end of input.

## Releases and channels

- `cli/scripts/release.sh <version>` builds six targets into `treg_<version>_<os>_<arch>.tar.gz`
  (`.zip` on Windows), writes `SHA256SUMS` and signs it with minisign. It refuses to build without
  `TREG_UPDATE_KEY`, the public key compiled into each binary: a build without it could never
  verify, and so never apply, an update. The manual `CLI release` workflow publishes the result as a
  `cli-v<version>` GitHub release, a pre-release on the beta channel.
- `GET /cli/latest?channel=&os=&arch=` (`routers/cli_client.py`, deciding in
  `application/cli_releases.py`, the GitHub lookup in `infra/github_releases.py`) names the release
  a CLI on that channel and platform runs: `TREG_CLI_LATEST_VERSION` for stable, `TREG_CLI_BETA_VERSION` for beta
  (beta falls back to stable), else the newest matching GitHub release of `cli_release_repo`. A 0.x
  value names a Python CLI release and never an archive. Download URLs come from
  `TREG_CLI_DOWNLOAD_BASE_URL`. Publishing a release moves no one; the operator's variable does,
  so a bad release is held back without a new one.
- `install-bin.sh` runs as `sh -c` with no arguments, because every older `treg update` runs the
  served installer that way. It reads `/cli/latest`, checks the archive against `SHA256SUMS` (a
  shell has no signature tool; the binary verifies signatures on update), installs into
  `~/.local/bin` by same-directory copy and rename, installs the Windows binary from Git Bash, tells
  a stable-channel Homebrew user to `brew upgrade treg` instead of installing a second copy, prints
  the exact PATH line when the directory is not on PATH, records the channel, bootstraps the skill
  and signs in with `TREG_TOKEN`. `install.ps1` does the same in PowerShell and adds the directory
  to the user PATH.

## Self-update

`update.Install` fetches `SHA256SUMS` and its signature, verifies the signature with the key built
into the binary (`internal/minisign`), checks the archive's sum, extracts the binary and renames it
over the running one (Windows renames the running file to `.old`; the next run removes it). `treg
update [--channel stable|beta]` does it in the foreground; a Homebrew or npm install is told its
package manager's command.

A release build the installer placed also updates itself: at most once a day a command starts a
detached `treg __update` and never waits for it. That process removes the Python CLI's uv or pipx
package once (their uninstallers delete the `treg` entry point, now this binary's path, so a copy
is put back the moment it goes), installs a newer release, re-runs `skill bootstrap` as re-running
the installer did, and leaves its outcome in `update-state.json`. `TREG_NO_AUTO_UPDATE` or
`"auto_update": false` turn the background update off.

## Usage events

The binary records the Python CLI's `cli_command_completed` (same vocabulary and opt-outs, [cli](cli.md)
"Anonymous CLI analytics"; the command name is the Python handler's, so one command keeps one name)
plus `arch` and `channel`. At the end of a command the event is appended to
`telemetry-queue.jsonl` beside the config; a later command sends the queue to its registry's
`/cli/events` on a goroutine while it works, through the command's own connection pool (one per
process, `api.Transport`), so no command waits on analytics and none is lost to a process that
exits first. A sender claims the queue by renaming it, requeues what it could not
send, picks up a claim a killed process left after a minute, and drops events for a registry
without the relay. Each event carries a uuid, so PostHog drops a resend.

`POST /cli/events` takes no token: each event's properties are checked against a fixed vocabulary
and anything else is dropped, the caller's address is never forwarded (`$geoip_disable`), each
address has an in-memory window, and a well-formed batch is always a 204. A registry without a
PostHog key drops them, so a self-hosted CLI reports nowhere else. `install-bin.sh` sends
`cli_install_step` the same way.
