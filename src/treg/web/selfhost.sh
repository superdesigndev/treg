#!/bin/sh
# treg — run your own registry locally, with no account.
#   curl -fsSL {BASE}/selfhost.sh | sh
#
# Brings up a server you are ALREADY SIGNED INTO: no signup, no email, no password. Everything
# stays on this machine (sqlite under ~/.treg), bound to loopback only. When you want the team
# version later, `treg cloud join` moves it to a hosted registry.
set -e

PORT="${TREG_PORT:-18790}"
HOME_DIR="${TREG_HOME:-$HOME/.treg}"
URL="http://localhost:$PORT"

say() { printf '  %s\n' "$1"; }
printf '\n\033[38;5;173m▚ treg\033[0m - starting your local registry…\n\n'

# ---- 1. python (the server needs it; the CLI alone would not) -------------------------------
PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "  python3 is required (brew install python / apt install python3)"; exit 1; }

# ---- 2. install the server + CLI into an isolated venv --------------------------------------
mkdir -p "$HOME_DIR"
VENV="$HOME_DIR/venv"
if [ ! -x "$VENV/bin/treg" ]; then
  say "installing treg (server + CLI) into $VENV"
  "$PY" -m venv "$VENV" >/dev/null
  "$VENV/bin/pip" install --quiet --upgrade pip >/dev/null
  "$VENV/bin/pip" install --quiet "tools-registry[server]" >/dev/null
else
  say "treg already installed — reusing $VENV"
fi

# ---- 3. a key so secrets survive a restart --------------------------------------------------
ENV_FILE="$HOME_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
  KEY="$("$VENV/bin/python" -m treg keygen)"
  cat > "$ENV_FILE" <<EOF
# treg local mode — this machine only. Never point TREG_PUBLIC_URL at a public domain while
# TREG_SINGLE_USER is true: the no-login dashboard refuses to run anywhere but loopback + sqlite.
TREG_SINGLE_USER=true
TREG_DATABASE_URL=sqlite+aiosqlite:///$HOME_DIR/treg.db
TREG_PUBLIC_URL=$URL
TREG_SECRET_KEY=$KEY
EOF
  chmod 600 "$ENV_FILE"
  say "created $ENV_FILE (your encryption key lives here — keep it)"
fi

# ---- 4. run it ------------------------------------------------------------------------------
if curl -fsS "$URL/meta" >/dev/null 2>&1; then
  say "a treg server is already running on $PORT"
else
  say "starting the server on $PORT"
  ( cd "$HOME_DIR" && PORT="$PORT" nohup "$VENV/bin/python" -m treg > "$HOME_DIR/server.log" 2>&1 & )
  i=0
  while [ $i -lt 40 ]; do
    curl -fsS "$URL/meta" >/dev/null 2>&1 && break
    i=$((i+1)); sleep 0.5
  done
  curl -fsS "$URL/meta" >/dev/null 2>&1 || { echo "  server did not start — see $HOME_DIR/server.log"; exit 1; }
fi

# ---- 5. point the CLI at it, with the token the server just minted --------------------------
TOKEN_FILE="$HOME_DIR/local-token"
if [ -f "$TOKEN_FILE" ]; then
  "$VENV/bin/treg" login --url "$URL" --token "$(cat "$TOKEN_FILE")" >/dev/null 2>&1 || true
fi

# ---- 6. put `treg` on the PATH --------------------------------------------------------------
BIN="$HOME/.local/bin"
mkdir -p "$BIN"
ln -sf "$VENV/bin/treg" "$BIN/treg"
case ":$PATH:" in *":$BIN:"*) ;; *) say "add to your shell: export PATH=\"\$HOME/.local/bin:\$PATH\"" ;; esac

printf '\n\033[32m  ✓ your registry is running\033[0m\n\n'
say "Dashboard   $URL/app   (already signed in — no account needed)"
say "CLI         treg tool ls"
say "Next        treg connect stripe    # paste a key once, then call it with no key"
printf '\n'
