#!/usr/bin/env bash
# Build the browser app into the Python package. Node is needed only at build time.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
npm --prefix "$ROOT/frontend" ci --include=dev
npm --prefix "$ROOT/frontend" run build
