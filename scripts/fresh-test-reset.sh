#!/usr/bin/env bash
# fresh-test-reset.sh — wipe every trace of treg from this machine so the next
# onboarding run is a true first-time experience. No backups: this is a dev tool
# for testing signup/install flows, and everything it removes is re-creatable
# (`treg login`, `treg mcp install`, install.sh).
#
# Removes, when present:
#   ~/.treg/                      CLI login token, active org, local-proxy CA state
#   ~/.claude/skills/treg/        the agent skill install.sh drops into Claude Code
#   Claude Code MCP registration  `claude mcp remove treg` (user scope)
#   Codex MCP registration        `codex mcp remove treg`, if a codex CLI exists
#   the treg binary itself        uv tool / pipx / brew / pip, whichever answers
#
# Deliberately NOT touched: a repo checkout's .env (server-side platform keys,
# not user state) and ~/.local/bin on PATH generally.

set -uo pipefail

say()  { printf '  %-34s %s\n' "$1" "$2"; }

echo "treg fresh-test reset"

if [ -d "$HOME/.treg" ]; then
  rm -rf "$HOME/.treg" && say "~/.treg" "removed (login + proxy state)"
else
  say "~/.treg" "not present"
fi

if [ -d "$HOME/.claude/skills/treg" ]; then
  rm -rf "$HOME/.claude/skills/treg" && say "~/.claude/skills/treg" "removed"
else
  say "~/.claude/skills/treg" "not present"
fi

if command -v claude >/dev/null 2>&1; then
  if claude mcp get treg >/dev/null 2>&1; then
    claude mcp remove treg >/dev/null 2>&1 && say "claude mcp (treg)" "removed"
  else
    say "claude mcp (treg)" "not registered"
  fi
else
  say "claude mcp (treg)" "claude CLI not found — skipped"
fi

if command -v codex >/dev/null 2>&1; then
  if codex mcp get treg >/dev/null 2>&1; then
    codex mcp remove treg >/dev/null 2>&1 && say "codex mcp (treg)" "removed"
  else
    say "codex mcp (treg)" "not registered"
  fi
fi

# Try each installer that could own it; the package is named tools-registry on
# PyPI but treg in Homebrew. Loop because more than one can be present at once.
removed_binary=""
if command -v uv >/dev/null 2>&1 && uv tool list 2>/dev/null | grep -q '^tools-registry '; then
  uv tool uninstall tools-registry >/dev/null 2>&1 && removed_binary="$removed_binary uv-tool"
fi
if command -v pipx >/dev/null 2>&1 && pipx list 2>/dev/null | grep -q 'tools-registry'; then
  pipx uninstall tools-registry >/dev/null 2>&1 && removed_binary="$removed_binary pipx"
fi
if command -v brew >/dev/null 2>&1 && brew list treg >/dev/null 2>&1; then
  brew uninstall treg >/dev/null 2>&1 && removed_binary="$removed_binary brew"
fi
if command -v treg >/dev/null 2>&1; then
  # Still resolvable → a plain `pip install` (or stray copy). Ask pip, then fall
  # back to deleting the executable pointed at, which is at worst a dangling shim.
  pip uninstall -y tools-registry >/dev/null 2>&1 || pip3 uninstall -y tools-registry >/dev/null 2>&1
  command -v treg >/dev/null 2>&1 && rm -f "$(command -v treg)"
  command -v treg >/dev/null 2>&1 || removed_binary="$removed_binary pip/stray"
fi
if [ -n "$removed_binary" ]; then
  say "treg binary" "removed (${removed_binary# })"
elif command -v treg >/dev/null 2>&1; then
  say "treg binary" "STILL PRESENT at $(command -v treg) — remove by hand"
else
  say "treg binary" "not installed"
fi

echo
leftover=0
[ -d "$HOME/.treg" ] && leftover=1
[ -d "$HOME/.claude/skills/treg" ] && leftover=1
command -v treg >/dev/null 2>&1 && leftover=1
if [ "$leftover" -eq 0 ]; then
  echo "clean — this machine now looks like a first-time user's."
else
  echo "⚠ some state survived (see above)."
  exit 1
fi
