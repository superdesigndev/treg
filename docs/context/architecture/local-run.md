---
title: Local CLI runs - run a vendor CLI as a dedicated user with a server-held credential (`treg run`)
status: shipped
sources:
  - src/treg/localrun.py
  - src/treg/egress.py
  - src/treg/fsjail.py
related:
  - interface/api.md
  - interface/cli.md
  - interface/env-import.md
  - interface/shell.md
  - architecture/auth-secrets.md
  - architecture/data-model.md
---

# Local CLI runs (`treg run --local`)

A member runs a vendor CLI (`stripe`, `gh`, `gcloud`, ...) on their own machine while the credential
stays in treg custody. The goal: **no other program of the same user may read the credential** while
the CLI runs. Server-side CLI execution is disabled for every caller (`POST /run` is denied
unconditionally, see AGENTS.md); `server_runnable` in the tool view is always false.

## Why a dedicated user

Only a **genuinely different uid** hides the CLI's environment and memory from the member. Two cheaper
ideas do not work and must not be reintroduced:

- **`PR_SET_DUMPABLE=0` before exec**: `execve` resets the flag, so the exec'd CLI is readable again.
- **Rootless bubblewrap**: you own the user namespace you create, so you can still read into it.

A member cannot read another uid's `/proc/<pid>/environ` on Linux, nor get its task port on macOS
(`task_for_pid` is denied across uids). So the CLI runs as the **`treg-run`** system user, created by
`treg setup-local-run` (`useradd` on Linux, a hidden `dscl` service account on macOS), with a sudoers
rule that lets the member run only the runner as `treg-run`, never a shell. treg must be installed at a
system-accessible path: `treg-run` cannot traverse a member's `0700` home to exec it.

Without that setup, `treg run` runs as the member, best-effort, with a warning. Windows is not covered.
The boundary is real in both directions: the CLI cannot read the member's owner-only files or
`localhost`-bound state.

## The grant

The member's `treg run` hands off via `sudo -u treg-run <runner>`, passing the member's own treg token
(`TREG_RUN_TOKEN/BASE/ORG`, plus `TREG_RUN_FSJAIL`, kept by the sudoers `env_keep`). The runner calls
`POST /tools/{name}/grant`, receives the credential and runs the CLI with it. The vendor credential only
ever exists under `treg-run`.

`localrun.py` must stay importable by the light CLI install: server-only imports are lazy inside
`render_grant` (the "Lightweight CLI modules" import-linter contract).

Rules a change must keep:

- **OAuth secrets release only the leaf.** The inject entry's `secret_field` must be in
  `_OAUTH_RELEASABLE_FIELDS`; a grant never hands out `refresh_token` or `client_secret`.
- **A grant releases only a secret that belongs to the tool**: one of its bindings' `secret_id`, a
  secret in the tool's bundle, or one owned by the tool's owner, always same-org. Otherwise a member
  could point an inject entry at another user's secret id and read it.
- **Shared keys need the runner proof.** A non-admin running a tool whose key they do not own must
  present `X-Treg-Run-Proof` (the value in `/etc/treg-run/proof`, readable only by `treg-run`). A
  direct member `/grant` call thus cannot extract a teammate's key. The same decision sets
  `redact_output`, and the client scrubs the injected value from stdout/stderr, holding
  `len(secret)-1` bytes between reads so a value split across chunks is still caught.
- **The catalog never enables a run**; only the owner's `tool.cli.enabled` does. `effective_profile`
  merges `tool.cli` over the catalog profile (deny lists unioned unless `deny_defaults: false`).
- **Deny is checked server-side** (`check_deny`, at grant, where the secret lives) against subcommands
  that would print the key or run member code as `treg-run`.
- **An explicit `inject: []` means inject nothing.** A self-authenticating CLI (credential in its own
  config) stores an empty list so the catalog's inject is not merged back in; `render_grant` returns
  empty items for it.
- An ambiguous multi-credential tool with no explicit mapping fails the grant rather than inject the
  wrong key.
- Grants are member+ (a viewer may call through the proxy but not extract a value), subject to the
  member's `tool_access` and `local_run_enabled` gates (owner exempt, see
  [multi-tenancy](multi-tenancy.md)), and audited with key-shaped argv tokens redacted.

## Egress allow-list

A CLI feature that runs member code inherits the key and could send it over the network. Setup
restricts the `treg-run` uid's outbound traffic to the registry plus every catalog `base_url` host
(`egress.py`): macOS `pf` per-user rules, reloaded at boot by a LaunchDaemon; Linux nftables owner
match. The list holds IPs resolved at install time, so vendor IP rotation drops legitimate calls until
`sudo treg setup-local-run --refresh-egress` re-resolves them. `--no-egress` opts out.

## Filesystem jail (`treg run --fs-jail`, opt-in, macOS only)

Confines the run's writes to a private per-run scratch (pointed at as `HOME`/`TMPDIR`, removed after)
via a `sandbox-exec` profile (`fsjail.py`), so member code cannot write the key where the member reads
it. Opt-in because it also blocks legitimate output files. There is no Linux implementation.
