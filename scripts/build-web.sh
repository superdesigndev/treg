#!/usr/bin/env bash
# Hosted Web service build. Workers keep the locked Python-only installation.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
bash scripts/build-dashboard.sh
uv sync --locked --no-dev --extra server --active
