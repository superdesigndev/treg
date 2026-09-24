---
name: dev-local
description: One-command local dev stack for tools-registry. Use when asked to "start the dev server", "run treg locally", "test login locally", "bring the stack up", or before any manual/browser test against localhost.
---

# dev-local — the local treg stack in one command

`scripts/dev-local.sh` runs the FastAPI server in a tmux session with dev-safe
settings, and gives you a sandboxed CLI that never touches `~/.treg/config.json`.

| Service | Command (managed by the script) | Port |
|---|---|---|
| treg server | `uv run python -m treg --reload` + `TREG_EMAIL_DEV_MODE=true`, own sqlite `treg-dev.db` | 18790 |
| Dashboard Vite | `npm run dev` in `frontend/`, hot updates loaded by the Python entry | 5173 |

No infra deps (sqlite). Prerequisites: Node 22.12+, npm, `tmux`, `uv` (the script runs `uv sync` if `.venv` is missing).

## Subcommands

```
scripts/dev-local.sh up          # start (idempotent) — prints URLs when healthy
scripts/dev-local.sh down        # stop the session
scripts/dev-local.sh status      # windows + port check
scripts/dev-local.sh logs        # last 200 lines of the server window
scripts/dev-local.sh restart     # respawn the server window
scripts/dev-local.sh attach      # attach to tmux
scripts/dev-local.sh cli <args>  # working-tree treg CLI, sandboxed HOME, pre-pointed at localhost
scripts/dev-local.sh reset       # down + wipe treg-dev.db and the CLI sandbox
```

`cli` is the key trick for login testing: `scripts/dev-local.sh cli login` runs the
repo's CLI against localhost with `HOME=scripts/.dev-home`, so the real
`~/.treg/config.json` (usually pointing at production) is never overwritten.
Email OTP dev mode is on — codes appear in the login page / API response, no mail sender needed.

`TREG_DEV_DB=/absolute/path/to/dev.db` selects a separate database without resetting the default.

For a LAN preview, build with `bash scripts/build-dashboard.sh`, then use
`TREG_FRONTEND_DEV=false scripts/dev-local.sh restart` (or `up` if stopped). This serves
compiled assets on port 18790; the default hot-update mode requires a browser on the same machine.

## Troubleshooting

- **Port 18790 in use, no session** → something else owns it: `lsof -i :18790`.
- **Server window died** → `scripts/dev-local.sh logs` for the traceback, then `restart`.
- **Stale dev state / want a fresh DB** → `scripts/dev-local.sh reset` then `up`.
