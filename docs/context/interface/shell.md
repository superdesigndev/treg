---
title: Shell mode (treg shell) - transparent CLI interception
status: shipped
sources:
  - src/treg/shell.py
  - src/treg/cli.py
related:
  - architecture/local-run.md
  - architecture/local-proxy.md
  - interface/cli.md
---

# Shell mode (`treg shell`)

`treg shell start` opens a subshell in which the team's registered CLIs (`stripe`, `gh`, ...) run
with the team credential, no key typed; `exit` reverts everything. For one command without a
subshell, `treg <command>` ([local-proxy](../architecture/local-proxy.md)) is the usual door.

## Shims on `PATH`, not a shell hook

`start_session` writes one shim per CLI into a private `0700` session dir and puts it first on
`PATH`; the shell's own name resolution decides what is routed, with no `preexec`/`DEBUG` traps. Only
tools whose `cli` is `enabled` are shimmed (otherwise `treg run` would 403), and a `bin` that is not a
plain filename is skipped, since a shim is a file we write.

Each shim is `exec env PATH="$TREG_SHELL_REALPATH" treg run <tool> -- "$@"`:

- The original `PATH` makes `treg run` find the real binary, so the shim cannot recurse into itself.
- The literal `--` fences treg's parsing from the user's arguments.
- Cobra `__complete*` calls exec the real binary directly: completion needs no credential, and routing
  it through treg would flood the audit and burn the daily cap one keystroke at a time.

Every shim runs locally through `treg run`, so grant, deny, runner proof, OAuth leaf and audit all
come from [local-run](../architecture/local-run.md). Server execution is disabled, so `--server-for`
only warns and falls back to local. Do not add a long-lived in-process credential agent: per-command
dedicated-user isolation leaves nothing in the member's process, which is the stronger posture.

## Session lifecycle

`TREG_SHELL` marks the session and blocks nesting. `_run_subshell` ignores `SIGINT`/`SIGQUIT` (they
belong to the interactive child) and stops the child on `SIGTERM`/`SIGHUP`, so `treg shell stop` and a
closed terminal both reach teardown without orphans. `--ttl` closes the session after N minutes.

## `--proxy`

Also captures HTTPS calls the agent makes on its own, through the
[local proxy](../architecture/local-proxy.md). Opt-in, because interception can break a
certificate-pinned client. The allow-list is seeded from the tool listing already fetched for the
shims (every registered host, CLI or not). The proxy's environment is applied after the session's own
variables and may not set `PATH` or `TREG_SHELL*`, so it cannot break name resolution or fake a nested
session. The banner lists the captured hosts; a member must never discover interception by accident.
When `cryptography` (the `[proxy]` extra) is missing, treg offers the install command matching how this
copy was installed (pipx, pip or uv).
