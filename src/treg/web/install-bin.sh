#!/bin/sh
# treg installer for the binary CLI: curl -fsSL {BASE}/install.sh?channel=beta | sh
#
# Downloads one file for this machine from the release the registry names, checks it against the
# release's SHA256SUMS, puts it in ~/.local/bin (TREG_INSTALL_DIR to change) and points it at this
# registry. Every older treg (any Python version) runs `sh -c "<this script>"` with no arguments on
# `treg update`, so it must work that way too.
#
#   --token <t>    sign in non-interactively (also TREG_TOKEN) and register the MCP server
#   --skills <s>   passed to `treg skill bootstrap`
#   --channel <c>  stable or beta (default {CHANNEL})
set -eu

BASE="{BASE}"
AD_LINK="{AD_LINK}"
CHANNEL="${TREG_CHANNEL:-{CHANNEL}}"
TOKEN="${TREG_TOKEN:-}"
SKILLS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --token) TOKEN="${2:-}"; shift 2 ;;
    --token=*) TOKEN="${1#*=}"; shift ;;
    --skills) SKILLS="${2:-}"; shift 2 ;;
    --skills=*) SKILLS="${1#*=}"; shift ;;
    --channel) CHANNEL="${2:-}"; shift 2 ;;
    --channel=*) CHANNEL="${1#*=}"; shift ;;
    *) printf 'treg install: unknown option %s\n' "$1" >&2; exit 2 ;;
  esac
done
case "$CHANNEL" in stable|beta) ;; *) printf 'treg install: --channel is stable or beta\n' >&2; exit 2 ;; esac

say() { printf '%s\n' "$*"; }
fail() { printf 'treg install: %s\n' "$*" >&2; report failed "$REASON"; exit 1; }

# Anonymous install telemetry under the CLI's rules (docs/context/interface/cli.md), sent to this
# registry, which forwards it without the address: TREG_TELEMETRY=0|false|off or DO_NOT_TRACK=1
# turn it off, and a failed send never changes the install.
TELEMETRY=1
case "$(printf '%s' "${TREG_TELEMETRY:-}" | tr '[:upper:]' '[:lower:]')" in 0|false|off) TELEMETRY="" ;; esac
[ "${DO_NOT_TRACK:-}" = "1" ] && TELEMETRY=""
clean() { printf '%s' "$1" | tr -cd 'A-Za-z0-9._+-' | cut -c1-40; }
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
config_dir() {
  # shellcheck disable=SC2088 # a literal ~/ in TREG_CONFIG, which the CLI expands itself
  case "${TREG_CONFIG:-}" in
    "") printf '%s' "$HOME/.treg" ;;
    "~/"*) dirname "$HOME/${TREG_CONFIG#"~/"}" ;;
    *) dirname "$TREG_CONFIG" ;;
  esac
}
install_id() {
  dir=$(config_dir)
  file="$dir/analytics-id"
  if [ ! -s "$file" ]; then
    mkdir -p "$dir" 2>/dev/null || return 0
    (umask 077; set -C; new_uuid > "$file") 2>/dev/null || true
  fi
  id=$(tr -d ' \n' < "$file" 2>/dev/null) || return 0
  printf '%s' "$id" | grep -qiE '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$' && printf '%s' "$id"
}
report() {  # report <step> [reason]
  [ -n "$TELEMETRY" ] && [ -n "$INSTALL_ID" ] && command -v curl >/dev/null 2>&1 || return 0
  props="\"install_step\":\"$1\",\"reason\":\"${2:-}\",\"method\":\"binary\",\"prior_version\":\"$PRIOR_VERSION\""
  props="$props,\"version\":\"$VERSION\",\"on_path\":$ON_PATH,\"os\":\"$UNAME_S\",\"arch\":\"$UNAME_M\""
  props="$props,\"ad_link\":$AD,\"with_token\":$TOK,\"channel\":\"$CHANNEL\""
  curl -fsS --connect-timeout 2 --max-time 3 -H 'Content-Type: application/json' \
    -d "{\"events\":[{\"event\":\"cli_install_step\",\"distinct_id\":\"$INSTALL_ID\",\"uuid\":\"$(new_uuid)\",\"properties\":{$props}}]}" \
    "$BASE/cli/events" >/dev/null 2>&1 || true
}

REASON=""
VERSION=""
ON_PATH=false
UNAME_S=$(clean "$(uname -s 2>/dev/null)")
UNAME_M=$(clean "$(uname -m 2>/dev/null)")
PRIOR_VERSION=$(clean "$(treg --version 2>/dev/null | awk '{print $2}')" || true)
AD=false; [ -n "$AD_LINK" ] && AD=true
TOK=false; [ -n "$TOKEN" ] && TOK=true
INSTALL_ID=""
[ -n "$TELEMETRY" ] && INSTALL_ID=$(install_id || true)
report started

case "$UNAME_S" in
  Darwin) OS=darwin ;;
  Linux) OS=linux ;;
  MINGW*|MSYS*|CYGWIN*) OS=windows ;;
  *) REASON=unsupported_os; fail "this installer supports macOS, Linux and Windows (Git Bash); on Windows PowerShell run: irm $BASE/install.ps1 | iex" ;;
esac
case "$UNAME_M" in
  x86_64|amd64) ARCH=amd64 ;;
  arm64|aarch64) ARCH=arm64 ;;
  *) REASON=unsupported_arch; fail "no treg build for $UNAME_M" ;;
esac
command -v curl >/dev/null 2>&1 || { REASON=no_curl; fail "curl is required"; }

EXE=treg
[ "$OS" = windows ] && EXE=treg.exe
DIR="${TREG_INSTALL_DIR:-$HOME/.local/bin}"
BIN="$DIR/$EXE"

# A Homebrew install on PATH is upgraded by Homebrew: installing a second copy would leave PATH
# order to decide which one runs.
current=$(command -v treg 2>/dev/null || true)
case "$current" in
  */Cellar/*|/opt/homebrew/*|/home/linuxbrew/*|*/.linuxbrew/*)
    if [ "$CHANNEL" = stable ]; then
      say "treg is installed with Homebrew ($current). Upgrade it with: brew upgrade treg"
      report installed
      exit 0
    fi ;;
esac

manifest=$(curl -fsSL "$BASE/cli/latest?channel=$CHANNEL&os=$OS&arch=$ARCH") || {
  REASON=no_release; fail "could not ask $BASE which treg release to install"; }
field() { printf '%s' "$manifest" | tr -d '\n' | sed -n "s/.*\"$1\": *\"\\([^\"]*\\)\".*/\\1/p"; }
VERSION=$(clean "$(field version)")
URL=$(field url)
SUMS_URL=$(field checksums_url)
ARCHIVE=$(field archive)
[ -n "$VERSION" ] && [ -n "$URL" ] && [ -n "$SUMS_URL" ] && [ -n "$ARCHIVE" ] || {
  REASON=bad_manifest; fail "the registry's release answer is incomplete"; }

TMP=$(mktemp -d 2>/dev/null || mktemp -d -t treg)
trap 'rm -rf "$TMP"' EXIT INT TERM
say "Downloading treg $VERSION ($OS/$ARCH)..."
curl -fsSL -o "$TMP/$ARCHIVE" "$URL" || { REASON=download; fail "could not download $URL"; }
curl -fsSL -o "$TMP/SHA256SUMS" "$SUMS_URL" || { REASON=download; fail "could not download $SUMS_URL"; }
want=$(awk -v f="$ARCHIVE" '$2 == f || $2 == "*"f {print $1}' "$TMP/SHA256SUMS")
if command -v sha256sum >/dev/null 2>&1; then
  got=$(sha256sum "$TMP/$ARCHIVE" | awk '{print $1}')
elif command -v shasum >/dev/null 2>&1; then
  got=$(shasum -a 256 "$TMP/$ARCHIVE" | awk '{print $1}')
else
  REASON=no_sha256; fail "sha256sum or shasum is required to verify the download"
fi
[ -n "$want" ] && [ "$want" = "$got" ] || { REASON=checksum; fail "the download does not match the release's SHA256SUMS"; }

mkdir -p "$TMP/x"
case "$ARCHIVE" in
  *.zip) (cd "$TMP/x" && { unzip -q "../$ARCHIVE" 2>/dev/null || tar -xf "../$ARCHIVE"; }) ;;
  *) tar -xzf "$TMP/$ARCHIVE" -C "$TMP/x" ;;
esac
[ -f "$TMP/x/$EXE" ] || { REASON=archive; fail "the archive holds no $EXE"; }
mkdir -p "$DIR" || { REASON=install_dir; fail "cannot create $DIR"; }
# Same-directory copy, then rename: an interrupted install never leaves a half-written treg, and
# a running treg keeps its old file.
cp "$TMP/x/$EXE" "$DIR/.$EXE.new" && chmod 755 "$DIR/.$EXE.new" && mv -f "$DIR/.$EXE.new" "$BIN" || {
  REASON=install_command; fail "cannot write $BIN"; }

if [ "$(command -v treg 2>/dev/null || true)" = "$BIN" ]; then
  ON_PATH=true
fi
"$BIN" config --base-url "$BASE" --channel "$CHANNEL" >/dev/null
if [ -n "$AD_LINK" ]; then
  dir=$(config_dir)
  mkdir -p "$dir" && printf '%s' "$AD_LINK" > "$dir/ad-link" || true
fi
if [ -n "$SKILLS" ]; then
  "$BIN" skill bootstrap --skills "$SKILLS" || say "(skill bootstrap failed; run: treg skill bootstrap)"
else
  "$BIN" skill bootstrap || say "(skill bootstrap failed; run: treg skill bootstrap)"
fi
if [ -n "$TOKEN" ]; then
  TREG_TOKEN="" "$BIN" login --token "$TOKEN" && "$BIN" mcp install || true
fi
report installed

# How a path reads to a person: ~/... under the home directory.
shown() { case "$1" in "$HOME"/*) printf '~/%s' "${1#"$HOME"/}" ;; *) printf '%s' "$1" ;; esac; }
say ""
say "Installed treg $VERSION to $(shown "$BIN")"
if [ "$ON_PATH" = true ]; then
  [ -n "$TOKEN" ] || say "Next: treg login"
else
  case "${SHELL:-}" in
    */zsh) rc="$HOME/.zshrc" ;;
    */bash) if [ "$OS" = darwin ]; then rc="$HOME/.bash_profile"; else rc="$HOME/.bashrc"; fi ;;
    */fish) rc="" ;;
    *) rc="$HOME/.profile" ;;
  esac
  case "$DIR" in
    "$HOME"/*) entry="\$HOME/${DIR#"$HOME"/}" ;;
    *) entry="$DIR" ;;
  esac
  found=$(command -v treg 2>/dev/null || true)
  if [ -n "$found" ]; then
    say "Another treg comes first on your PATH: $(shown "$found")"
    say "Remove it, or put $(shown "$DIR") first on PATH:"
  else
    say "$(shown "$DIR") is not on your PATH. Add it:"
  fi
  if [ -z "$rc" ]; then
    say "  fish_add_path $(shown "$DIR")"
  else
    say "  echo 'export PATH=\"$entry:\$PATH\"' >> $(shown "$rc") && . $(shown "$rc")"
  fi
  say "Then: treg login"
fi
