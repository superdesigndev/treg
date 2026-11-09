#!/bin/sh
# Signs a release's SHA256SUMS with the minisign secret key, then verifies the signature with the
# public key the binaries carry, so a release whose two halves do not match is never published.
#
#   sign-release.sh <secret key file> <public key> <SHA256SUMS> <signature file> <version>
set -eu
minisign -S -s "$1" -m "$3" -x "$4" -t "treg $5" </dev/null
minisign -V -P "$2" -m "$3" -x "$4" >/dev/null
