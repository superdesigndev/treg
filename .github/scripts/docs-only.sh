#!/usr/bin/env bash
# Writes only=true to GITHUB_OUTPUT when a direct push to main changes nothing but docs/ and *.md,
# so the test jobs can skip their work. Pull requests and merge queue builds always run: a skipped
# required check would block the merge forever.
set -euo pipefail

only=false
if [ "${GITHUB_EVENT_NAME:-}" = "push" ]; then
  changed=$(git diff --name-only HEAD^ HEAD || true)
  if [ -n "$changed" ] && ! echo "$changed" | grep -qvE '^docs/|\.md$'; then
    only=true
  fi
fi
echo "docs-only: $only"
echo "only=$only" >> "$GITHUB_OUTPUT"
