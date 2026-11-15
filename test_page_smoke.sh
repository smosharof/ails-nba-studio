#!/usr/bin/env bash
# Runs the page smoke test (macOS only: uses the system WebKit). Exits non-zero if anything fails.
cd "$(dirname "$0")" && osascript -l JavaScript test_page_smoke.js "$PWD/console_page.html" 2>&1 | grep -E "FAIL|checks passed|ERR" ; exit "${PIPESTATUS[0]}"
