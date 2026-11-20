#!/bin/sh
# The binary CLI's whole local check: Go vet and unit tests, two releases built by GoReleaser and
# signed with a throwaway minisign key, and the parity and beta-channel suites against a local
# registry.
#
#   cli/scripts/check-beta.sh          (needs go, minisign, uv, and goreleaser or network for
#                                      `go run`; run from anywhere in the repo)
set -eu

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cd "$ROOT/cli"
test -z "$(gofmt -l .)" || { gofmt -l .; echo "gofmt: the files above are not formatted" >&2; exit 1; }
go vet ./...
go run golang.org/x/tools/go/analysis/passes/modernize/cmd/modernize@v0.52.0 -test ./...
go test ./...
go build -o "$WORK/treg-go" ./cmd/treg

# Two releases as CI builds them (.goreleaser.yaml), signed with a throwaway key.
if command -v goreleaser >/dev/null 2>&1; then
  GORELEASER=goreleaser
else
  GORELEASER="go run github.com/goreleaser/goreleaser/v2@v2.20.0"
fi
minisign -G -W -p "$WORK/key.pub" -s "$WORK/key.sec" -f </dev/null >/dev/null
KEY="$(sed -n 2p "$WORK/key.pub")"
for v in 1.0.0 1.0.1; do
  TREG_UPDATE_KEY="$KEY" MINISIGN_KEY_FILE="$WORK/key.sec" TREG_RELEASE_VERSION="$v" \
    $GORELEASER release --snapshot --clean >/dev/null
  mkdir -p "$WORK/releases/v$v"
  cp dist/treg_"$v"_* dist/SHA256SUMS dist/SHA256SUMS.minisig "$WORK/releases/v$v/"
done
rm -rf dist

cd "$ROOT"
TREG_GO_BIN="$WORK/treg-go" TREG_GO_RELEASES="$WORK/releases" \
  uv run --frozen --with pytest-xdist python -m pytest tests/cli_go -n auto -q
