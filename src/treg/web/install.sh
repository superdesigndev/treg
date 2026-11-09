#!/bin/sh
# treg CLI installer  -  curl -fsSL {BASE}/install.sh | sh
# Installs the `treg` command and points it at this server ({BASE}).
set -e

BASE="{BASE}"

# Optional token for a one-shot, fully-authed setup:
#   curl -fsSL {BASE}/install.sh | sh -s -- --token <key>      (or: TREG_TOKEN=<key> … | sh)
# With it, this installs the CLI + skill AND signs in + registers the MCP server into every supported
# agent, header-authed — no browser. The key bakes in the team, so nothing else is needed. Without a
# token, it installs the CLI + skill and prints the sign-in step (the original behavior, unchanged).
#
# Only the treg skill installs by default. The registry's other skills are opt-in, named with
#   curl -fsSL {BASE}/install.sh | sh -s -- --skills lead-signals,recruit      (or: --skills all)
TOKEN="${TREG_TOKEN:-}"
SKILLS=""
# The code when fetched as {BASE}/i/<code>; empty on the plain URL.
AD_LINK="{AD_LINK}"
while [ $# -gt 0 ]; do
  case "$1" in
    --token) TOKEN="${2:-}"; [ $# -gt 1 ] && shift; shift ;;
    --token=*) TOKEN="${1#--token=}"; shift ;;
    --skills) SKILLS="${2:-}"; [ $# -gt 1 ] && shift; shift ;;
    --skills=*) SKILLS="${1#--skills=}"; shift ;;
    *) shift ;;
  esac
done
# Install from PyPI (fast, public, no git clone). The base package is the light CLI; the FastAPI/DB
# server stack is the `tools-registry[server]` extra, which people who self-host install separately.
#
# `[proxy]` is included here on purpose. It adds only `cryptography`, which is what `treg <command>`
# needs to generate this machine's certificate authority — and it ships as a prebuilt wheel, so there
# is no compiler and no meaningful wait. Without it the headline feature (`treg claude`) stops on its
# first run asking to be installed, which is a bad first minute. `pip install tools-registry` stays
# light for anyone who wants only the plain CLI.
SRC="tools-registry[proxy]"

# The supported interpreter range — keep in sync with `requires-python` in pyproject.toml.
# Passed explicitly because uv otherwise resolves against its DEFAULT interpreter (its own managed
# Python first), and when that is outside the range — a machine whose uv-managed default is 3.11,
# or 3.14+ once that ships as the OS default — the install dies on resolution instead of picking
# (or downloading) a compatible Python. With the constraint, uv fetches one if none is installed.
PYREQ=">=3.12,<3.14"

# Anonymous install telemetry, the CLI's own rules (docs/context/interface/cli.md): only a registry
# that sets a PostHog key sends, TREG_TELEMETRY=0|false|off or DO_NOT_TRACK=1 turn it off, and a
# failed send never changes the install. The id is the CLI's `analytics-id`, written here when it is
# missing, so a later command joins the install that produced it.
PH_KEY="{POSTHOG_KEY}"
PH_HOST="{POSTHOG_HOST}"
case "$(printf '%s' "${TREG_TELEMETRY:-}" | tr '[:upper:]' '[:lower:]')" in 0|false|off) PH_KEY="" ;; esac
[ "${DO_NOT_TRACK:-}" = "1" ] && PH_KEY=""
METHOD=""
VERSION=""
ON_PATH=false
clean() { printf '%s' "$1" | tr -cd 'A-Za-z0-9._+-' | cut -c1-40; }
treg_version() { clean "$(treg --version 2>/dev/null | awk '{print $2}')"; }
new_uuid() {
  if [ -r /proc/sys/kernel/random/uuid ]; then
    cat /proc/sys/kernel/random/uuid
  elif command -v uuidgen >/dev/null 2>&1; then
    uuidgen | tr 'A-F' 'a-f'
  else
    od -An -N16 -tx1 /dev/urandom | tr -d ' \n' |
      sed -E 's/^(.{8})(.{4})(.{4})(.{4})(.{12})$/\1-\2-\3-\4-\5/'
  fi
}
install_id() {
  # shellcheck disable=SC2088 # a literal ~/ in TREG_CONFIG, which the CLI expands itself
  case "${TREG_CONFIG:-}" in
    "") dir="$HOME/.treg" ;;
    "~/"*) dir=$(dirname "$HOME/${TREG_CONFIG#"~/"}") ;;
    *) dir=$(dirname "$TREG_CONFIG") ;;
  esac
  file="$dir/analytics-id"
  if [ ! -s "$file" ]; then
    mkdir -p "$dir" 2>/dev/null || return 0
    (umask 077; set -C; new_uuid > "$file") 2>/dev/null || true
  fi
  id=$(tr -d ' \n' < "$file" 2>/dev/null) || return 0
  printf '%s' "$id" | grep -qiE '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$' && printf '%s' "$id"
}
report() {  # report <step> [reason]
  [ -n "$PH_KEY" ] && [ -n "$INSTALL_ID" ] && command -v curl >/dev/null 2>&1 || return 0
  props="\"install_step\":\"$1\",\"reason\":\"${2:-}\",\"method\":\"$METHOD\",\"prior_version\":\"$PRIOR_VERSION\""
  props="$props,\"version\":\"$VERSION\",\"on_path\":$ON_PATH,\"os\":\"$OS\",\"arch\":\"$ARCH\""
  props="$props,\"ad_link\":$AD,\"with_token\":$TOK,\"\$process_person_profile\":false,\"\$geoip_disable\":true"
  curl -fsS --connect-timeout 2 --max-time 3 -H 'Content-Type: application/json' \
    -d "{\"api_key\":\"$PH_KEY\",\"event\":\"cli_install_step\",\"distinct_id\":\"$INSTALL_ID\",\"properties\":{$props}}" \
    "$PH_HOST/i/v0/e/" >/dev/null 2>&1 || true
}
leave_ad_link() {
  if [ -n "$AD_LINK" ]; then
    mkdir -p "$HOME/.treg" 2>/dev/null && printf '%s\n' "$AD_LINK" > "$HOME/.treg/ad-link" 2>/dev/null || true
  fi
}
fail() {  # fail <reason>
  report failed "$1"
  [ -z "$PRIOR_VERSION" ] || exit 1
  script=$(curl -fsSL "$BASE/install.sh?channel=beta" 2>/dev/null) || exit 1
  case "$script" in "#!/bin/sh"*) ;; *) exit 1 ;; esac
  printf '\nInstalling the treg binary instead, which needs no Python…\n\n'
  leave_ad_link
  set -- --channel stable
  if [ -n "$TOKEN" ]; then set -- "$@" --token "$TOKEN"; fi
  if [ -n "$SKILLS" ]; then set -- "$@" --skills "$SKILLS"; fi
  exec sh -c "$script" install-bin "$@"
}
INSTALL_ID=""
[ -n "$PH_KEY" ] && INSTALL_ID=$(install_id || true)
OS=$(clean "$(uname -s 2>/dev/null)")
ARCH=$(clean "$(uname -m 2>/dev/null)")
PRIOR_VERSION=$(treg_version || true)
AD=false; [ -n "$AD_LINK" ] && AD=true
TOK=false; [ -n "$TOKEN" ] && TOK=true
report started

printf '\n\033[38;5;173m▚ tools-registry\033[0m - installing the treg CLI…\n\n'

if command -v uv >/dev/null 2>&1; then
  METHOD=uv
  uv tool install --force --python "$PYREQ" "$SRC" || fail install_command
elif command -v pipx >/dev/null 2>&1; then
  METHOD=pipx
  # pipx needs a concrete interpreter, not a range — probe for one inside PYREQ before falling
  # back to pipx's default (which is pipx's own Python and may be outside the range).
  PIPX_PY=""
  for py in python3.13 python3.12; do
    if command -v "$py" >/dev/null 2>&1; then PIPX_PY="$py"; break; fi
  done
  if [ -n "$PIPX_PY" ]; then
    pipx install --force --python "$PIPX_PY" "$SRC" || fail install_command
  else
    pipx install --force "$SRC" || fail install_command
  fi
elif command -v pip3 >/dev/null 2>&1; then
  METHOD=pip3
  # pip can only install into the interpreter it belongs to. Outside PYREQ it prints a wall of
  # "Ignored the following versions" and dies — say what is wrong and how to fix it instead.
  if ! pip3 --version 2>/dev/null | grep -qE '\(python 3\.1[23]\)'; then
    echo "pip3 belongs to $(pip3 --version 2>/dev/null | sed -n 's/.*(python \([0-9.]*\)).*/Python \1/p'), but treg needs Python 3.12 or 3.13." >&2
    echo "Easiest fix — install uv (it fetches a matching Python by itself), then rerun this command:" >&2
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    fail python_version
  fi
  pip3 install --user --upgrade "$SRC" || fail install_command
else
  echo "Need Python 3.12 or 3.13 and one of: uv (recommended), pipx, or pip3." >&2
  echo "Install uv:  https://docs.astral.sh/uv/getting-started/installation/" >&2
  fail no_installer
fi

if command -v treg >/dev/null 2>&1; then
  ON_PATH=true
  VERSION=$(treg_version || true)
fi
report installed

# point the CLI at this server (falls back silently on older CLIs)
treg config --base-url "$BASE" >/dev/null 2>&1 || true

# Fetched as {BASE}/i/<code>: leave the code for the next `treg login`.
leave_ad_link

# install the official treg skill into every detected agent so it knows how to use treg.
# `treg skill bootstrap` fans out across all supported agents (Claude Code, Cursor, Codex, Gemini,
# Copilot, OpenCode, Windsurf, …). A CLI too old for `--skills` still installs the treg skill
# without it; one too old for bootstrap gets the Claude-only drop.
if [ -n "$SKILLS" ] && treg skill bootstrap --skills "$SKILLS" 2>/dev/null; then
  :
elif treg skill bootstrap 2>/dev/null; then
  :
else
  SKILL_DIR="$HOME/.claude/skills/treg"
  if mkdir -p "$SKILL_DIR" 2>/dev/null && curl -fsSL "$BASE/skill.md" -o "$SKILL_DIR/SKILL.md" 2>/dev/null; then
    printf '\033[32m✓\033[0m Installed the \033[1mtreg\033[0m skill for Claude Code (%s)\n' "$SKILL_DIR"
  fi
fi

# One-shot authed setup when a token was passed: sign in (the key bakes in the team) and register the
# MCP server into every supported agent, header-authed. Both are best-effort — a failure here never
# fails the install, since the CLI + skill are already in place.
if [ -n "$TOKEN" ]; then
  # Show login's own error: it can fail after the token was accepted.
  if LOGIN_ERR=$(treg login --token "$TOKEN" 2>&1 >/dev/null); then
    printf '\033[32m✓\033[0m Signed in.\n'
    treg mcp install 2>/dev/null || true
  else
    printf '\033[33m!\033[0m Sign-in with that token failed: %s\n' "$(printf '%s\n' "$LOGIN_ERR" | tail -n 1)"
    printf '  Run \033[1mtreg login\033[0m to sign in.\n'
  fi
  printf '\n\033[32m✓\033[0m treg is set up. Docs & tutorial:  %s/tutorial\n\n' "$BASE"
else
  printf '\n\033[32m✓\033[0m Installed \033[1mtreg\033[0m. Next:\n'
  printf '    \033[38;5;173mtreg login\033[0m      # sign in (GitHub or email) - first login registers you\n'
  printf '\nDocs & interactive tutorial:  %s/tutorial\n\n' "$BASE"
fi
