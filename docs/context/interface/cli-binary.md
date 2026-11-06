---
title: The binary CLI (cli/, Go) - compatibility, releases, self-update and usage events
status: building
sources:
  - cli/cmd/treg/main.go
  - cli/internal/buildinfo/buildinfo.go
  - cli/internal/term/term.go
  - cli/internal/commands/root.go
  - cli/internal/commands/env.go
  - cli/internal/commands/helpers.go
  - cli/internal/commands/account.go
  - cli/internal/session/proc_darwin.go
  - cli/internal/session/proc_linux.go
  - cli/internal/session/proc_other.go
  - cli/internal/update/detach_unix.go
  - cli/internal/update/detach_windows.go
  - cli/internal/api/client.go
  - cli/internal/api/url.go
  - cli/internal/app/app.go
  - cli/internal/config/config.go
  - cli/internal/session/session.go
  - cli/internal/output/output.go
  - cli/internal/output/call.go
  - cli/internal/commands/call.go
  - cli/internal/commands/update.go
  - cli/internal/telemetry/telemetry.go
  - cli/internal/update/update.go
  - cli/internal/update/method.go
  - cli/internal/update/legacy.go
  - cli/internal/minisign/minisign.go
  - cli/scripts/release.sh
  - src/treg/routers/cli_client.py
  - src/treg/web/install-bin.sh
  - src/treg/web/install.ps1
  - tests/cli_go/cli_go_harness.py
  - tests/cli_go/cli_go_live.py
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
  request, the login, MCP and telemetry ones included.
- **Output.** stdout carries the answer and treg's lines go to stderr, as in [cli](cli.md). A JSON
  body is re-indented by two spaces like the Python CLI's, but strings and numbers keep the
  upstream's spelling (the Python CLI escapes non-ASCII as `\uXXXX` and normalizes numbers): the
  parsed values are equal, the bytes are not. treg's own lines use a plain dash where the Python
  CLI wrote an em dash.
- **Parity suite.** `tests/cli_go` runs both CLIs against a fake registry (canned status, headers
  and body) and a live one (`python -m treg` on a fresh sqlite database, a signed-up team, an echo
  upstream registered as its own tool) and compares exit status, stdout (JSON parsed), stderr,
  config writes and the request each sent. It runs with `TREG_GO_BIN` (or `TREG_GO_BUILD=1`); the
  `cli-go` CI job runs it, and Go's vet and unit tests on Linux, macOS and Windows.

## Releases and channels

- `cli/scripts/release.sh <version>` builds six targets into `treg_<version>_<os>_<arch>.tar.gz`
  (`.zip` on Windows), writes `SHA256SUMS` and signs it with minisign. It refuses to build without
  `TREG_UPDATE_KEY`, the public key compiled into each binary: a build without it could never
  verify, and so never apply, an update. The manual `CLI release` workflow publishes the result as a
  `cli-v<version>` GitHub release, a pre-release on the beta channel.
- `GET /cli/latest?channel=&os=&arch=` (`routers/cli_client.py`) names the release a CLI on that
  channel and platform runs: `TREG_CLI_LATEST_VERSION` for stable, `TREG_CLI_BETA_VERSION` for beta
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
`/cli/events` on a goroutine while it works, so no command waits on analytics and none is lost to a
process that exits first. A sender claims the queue by renaming it, requeues what it could not
send, picks up a claim a killed process left after a minute, and drops events for a registry
without the relay. Each event carries a uuid, so PostHog drops a resend.

`POST /cli/events` takes no token: each event's properties are checked against a fixed vocabulary
and anything else is dropped, the caller's address is never forwarded (`$geoip_disable`), each
address has an in-memory window, and a well-formed batch is always a 204. A registry without a
PostHog key drops them, so a self-hosted CLI reports nowhere else. `install-bin.sh` sends
`cli_install_step` the same way.
