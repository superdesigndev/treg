#!/bin/sh
# Build every platform's treg archive for one version, with SHA256SUMS and, given a minisign key,
# its signature:
#
#   TREG_UPDATE_KEY=<base64 public key> [MINISIGN_KEY_FILE=<secret key>] cli/scripts/release.sh 1.2.3 [outdir]
#
# TREG_UPDATE_KEY is built into each binary; it is the key `treg update` verifies SHA256SUMS
# against, so a release built without it could never update itself and is refused here.
set -eu

VERSION="${1:?usage: release.sh <version> [outdir]}"
case "$VERSION" in [0-9]*.[0-9]*.[0-9]*) ;; *) echo "release.sh: the version is MAJOR.MINOR.PATCH" >&2; exit 2 ;; esac
: "${TREG_UPDATE_KEY:?set TREG_UPDATE_KEY to the minisign public key (its base64 line)}"
CLI="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${2:-$CLI/dist/$VERSION}"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PKG=github.com/superdesigndev/treg/cli/internal/buildinfo
LDFLAGS="-s -w -X $PKG.Version=$VERSION -X $PKG.UpdateKey=$TREG_UPDATE_KEY"
for target in darwin/amd64 darwin/arm64 linux/amd64 linux/arm64 windows/amd64 windows/arm64; do
  os="${target%/*}"; arch="${target#*/}"
  exe=treg; [ "$os" = windows ] && exe=treg.exe
  dir="$WORK/$os-$arch"
  mkdir -p "$dir"
  (cd "$CLI" && CGO_ENABLED=0 GOOS="$os" GOARCH="$arch" go build -trimpath -ldflags "$LDFLAGS" -o "$dir/$exe" ./cmd/treg)
  name="treg_${VERSION}_${os}_${arch}"
  if [ "$os" = windows ]; then
    (cd "$dir" && rm -f "$OUT/$name.zip" && zip -q -X "$OUT/$name.zip" "$exe")
  else
    tar -C "$dir" -czf "$OUT/$name.tar.gz" "$exe"
  fi
done

cd "$OUT"
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum treg_"$VERSION"_* > SHA256SUMS
else
  shasum -a 256 treg_"$VERSION"_* > SHA256SUMS
fi
if [ -n "${MINISIGN_KEY_FILE:-}" ]; then
  rm -f SHA256SUMS.minisig
  minisign -S -s "$MINISIGN_KEY_FILE" -m SHA256SUMS -t "treg $VERSION" </dev/null
fi
ls -1 "$OUT"
