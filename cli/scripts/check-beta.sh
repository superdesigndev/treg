#!/bin/sh
# The binary CLI's whole local check: Go vet and unit tests, two releases signed with a throwaway
# minisign key, and the parity and beta-channel suites against a local registry.
#
#   cli/scripts/check-beta.sh          (needs go, minisign, uv; run from anywhere in the repo)
set -eu

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cd "$ROOT/cli"
test -z "$(gofmt -l .)" || { gofmt -l .; echo "gofmt: the files above are not formatted" >&2; exit 1; }
go vet ./...
go test ./...
go build -o "$WORK/treg-go" ./cmd/treg

minisign -G -W -p "$WORK/key.pub" -s "$WORK/key.sec" -f </dev/null >/dev/null
KEY="$(sed -n 2p "$WORK/key.pub")"
for v in 1.0.0 1.0.1; do
  TREG_UPDATE_KEY="$KEY" MINISIGN_KEY_FILE="$WORK/key.sec" "$ROOT/cli/scripts/release.sh" "$v" "$WORK/releases/cli-v$v" >/dev/null
done

cd "$ROOT"
TREG_GO_BIN="$WORK/treg-go" TREG_GO_RELEASES="$WORK/releases" \
  uv run --frozen --with pytest-xdist python -m pytest tests/cli_go -n auto -q
