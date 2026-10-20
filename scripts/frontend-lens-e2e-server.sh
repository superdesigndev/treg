#!/usr/bin/env bash
# Real dashboard/auth/storage/billing; only provider relay is replaced in the test process.
set -euo pipefail
: "${TREG_E2E_PORT:?set by the Lens Playwright config}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_DIR="$(mktemp -d "${TMPDIR:-/tmp}/treg-lens-browser.XXXXXX")"
trap 'rm -rf "$TEST_DIR"' EXIT
cd "$TEST_DIR"
export TREG_DATABASE_URL="sqlite+aiosqlite:///$TEST_DIR/test.db"
export TREG_PUBLIC_URL="http://127.0.0.1:$TREG_E2E_PORT"
export TREG_EMAIL_DEV_MODE=true
export TREG_FRONTEND_DEV=false
export TREG_PROMO_GRANT_MICRO=1000000
export TREG_RESEND_API_KEY=
export TREG_POSTHOG_KEY=
export TREG_INTERCOM_APP_ID=
export TREG_ONBOARDING_V2_EMAILS=
export TREG_SECRET_KEY="$(uv run --project "$ROOT" python -m treg keygen)"
export TREG_SESSION_SECRET=local-disposable-lens-browser-tests
uv run --project "$ROOT" python "$ROOT/tests/lens_browser_server.py"
