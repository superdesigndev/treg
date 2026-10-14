#!/usr/bin/env bash
# A disposable local database, with no dotenv file or external provider configuration.
# The port comes from frontend/playwright.config.ts, which picks a free one for each run.
set -euo pipefail
: "${TREG_E2E_PORT:?set by frontend/playwright.config.ts (or export one to run this by hand)}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_DIR="$(mktemp -d "${TMPDIR:-/tmp}/treg-browser.XXXXXX")"
trap 'rm -rf "$TEST_DIR"' EXIT
cd "$TEST_DIR"
export TREG_DATABASE_URL="sqlite+aiosqlite:///$TEST_DIR/test.db"
export TREG_PUBLIC_URL="http://127.0.0.1:$TREG_E2E_PORT"
export TREG_EMAIL_DEV_MODE=true
export TREG_FRONTEND_DEV=false
export TREG_PROMO_GRANT_MICRO=0
export TREG_RESEND_API_KEY=
export TREG_POSTHOG_KEY=
export TREG_INTERCOM_APP_ID=
export TREG_PLATFORM_PROVIDERS=
# The first-run flow for these addresses only (e2e/onboarding.spec.ts); everyone else, and so every
# other spec's signIn, keeps the team-name dialog.
export TREG_ONBOARDING_V2_EMAILS=@onboarding.test
export PORT="$TREG_E2E_PORT"
export TREG_SECRET_KEY="$(uv run --project "$ROOT" python -m treg keygen)"
export TREG_SESSION_SECRET=local-disposable-browser-tests
uv run --project "$ROOT" python -m treg
