#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# export_cookies.sh — helper to extract YouTube cookies via yt-dlp
#
# Usage:
#   bash export_cookies.sh [browser]
#
# Supported browsers: chrome, firefox, edge, safari, opera, brave, chromium
# Default: chrome
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

BROWSER="${1:-chrome}"
OUTPUT="cookies.txt"

echo "Extracting YouTube cookies from ${BROWSER}…"

# yt-dlp must be installed locally (pip install yt-dlp)
yt-dlp \
  --cookies-from-browser "${BROWSER}" \
  --cookies "${OUTPUT}" \
  --skip-download \
  "https://www.youtube.com" \
  2>&1 | grep -v "^\[debug\]" || true

if [[ -f "${OUTPUT}" ]] && [[ -s "${OUTPUT}" ]]; then
  echo "✅  Cookies saved to ${OUTPUT}"
  echo "    Lines: $(wc -l < "${OUTPUT}")"
else
  echo "❌  Failed to export cookies. Make sure you are logged in to YouTube in ${BROWSER}."
  exit 1
fi
