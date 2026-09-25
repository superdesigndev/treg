#!/usr/bin/env bash
# A disposable local database, with no dotenv file or external provider configuration.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_DIR="$(mktemp -d "${TMPDIR:-/tmp}/treg-browser.XXXXXX")"
trap 'rm -rf "$TEST_DIR"' EXIT
cd "$TEST_DIR"
export TREG_DATABASE_URL="sqlite+aiosqlite:///$TEST_DIR/test.db"
export TREG_PUBLIC_URL=http://127.0.0.1:18791
export TREG_EMAIL_DEV_MODE=true
export TREG_FRONTEND_DEV=false
export TREG_PROMO_GRANT_MICRO=0
export TREG_RESEND_API_KEY=
export TREG_POSTHOG_KEY=
export TREG_INTERCOM_APP_ID=
export TREG_PLATFORM_PROVIDERS=
export PORT=18791
export TREG_SECRET_KEY="$(uv run --project "$ROOT" python -m treg keygen)"
export TREG_SESSION_SECRET=local-disposable-browser-tests
uv run --project "$ROOT" python -m treg
