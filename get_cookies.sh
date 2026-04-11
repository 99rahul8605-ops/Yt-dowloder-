#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# get_cookies.sh — export YouTube cookies using yt-dlp's built-in extractor
#
# This is the RECOMMENDED method per yt-dlp docs:
#   yt-dlp --cookies-from-browser <browser> --cookies cookies.txt
#
# Usage:
#   bash get_cookies.sh [browser] [profile_path]
#
# Examples:
#   bash get_cookies.sh                            # Chrome (default)
#   bash get_cookies.sh firefox
#   bash get_cookies.sh chrome                     # standard Chrome path
#   bash get_cookies.sh chrome:~/.var/app/com.google.Chrome   # Flatpak Chrome
#   bash get_cookies.sh edge
#   bash get_cookies.sh brave
#   bash get_cookies.sh chromium
#   bash get_cookies.sh safari                     # macOS only
#
# Requirements:
#   pip install yt-dlp
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

BROWSER="${1:-chrome}"
OUTPUT="cookies.txt"
BACKUP="cookies.txt.bak"

# ── Validate yt-dlp is available ──────────────────────────────────────────────
if ! command -v yt-dlp &>/dev/null; then
    echo "❌  yt-dlp not found. Install it first:"
    echo "    pip install yt-dlp"
    exit 1
fi

# ── Backup existing cookies ───────────────────────────────────────────────────
if [[ -f "$OUTPUT" && -s "$OUTPUT" ]]; then
    cp "$OUTPUT" "$BACKUP"
    echo "📦  Backed up existing cookies to $BACKUP"
fi

echo "🌐  Extracting cookies from browser: $BROWSER"
echo "    Make sure you are LOGGED IN to YouTube in that browser."
echo ""

# ── Export via yt-dlp ─────────────────────────────────────────────────────────
# --cookies-from-browser extracts live browser cookies (all sites)
# --cookies writes them to a Netscape-format file
# We pass a YouTube URL so yt-dlp filters only youtube.com cookies
yt-dlp \
    --cookies-from-browser "$BROWSER" \
    --cookies "$OUTPUT" \
    --skip-download \
    --quiet \
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ" \
    2>&1 || {
        echo "❌  yt-dlp failed. Make sure:"
        echo "    1. You are logged in to YouTube in $BROWSER"
        echo "    2. The browser is closed (some browsers lock the cookie DB)"
        echo "    3. yt-dlp version is recent: yt-dlp -U"
        exit 1
    }

# ── Validate output ───────────────────────────────────────────────────────────
if [[ ! -f "$OUTPUT" || ! -s "$OUTPUT" ]]; then
    echo "❌  Output file is missing or empty."
    exit 1
fi

# Confirm magic header is present
FIRST_LINE=$(head -1 "$OUTPUT")
if [[ "$FIRST_LINE" != "# HTTP Cookie File" && "$FIRST_LINE" != "# Netscape HTTP Cookie File" ]]; then
    echo "⚠️  Warning: unexpected first line: $FIRST_LINE"
    echo "   Adding Netscape header…"
    TMP=$(mktemp)
    echo "# Netscape HTTP Cookie File" > "$TMP"
    grep -v "^# Netscape\|^# HTTP Cookie" "$OUTPUT" >> "$TMP"
    mv "$TMP" "$OUTPUT"
fi

# Confirm LF line endings (required for Linux/Docker)
if file "$OUTPUT" | grep -qi "CRLF"; then
    echo "⚠️  CRLF line endings detected — converting to LF…"
    sed -i 's/\r//' "$OUTPUT"
fi

# Count YouTube cookies
YT_COOKIES=$(grep -c "youtube\.com" "$OUTPUT" 2>/dev/null || echo 0)
TOTAL_LINES=$(grep -cv "^#" "$OUTPUT" 2>/dev/null || echo 0)

echo ""
echo "✅  Done!"
echo "   File       : $OUTPUT"
echo "   Total lines: $TOTAL_LINES"
echo "   YT cookies : $YT_COOKIES"
echo ""
echo "📋  Next steps:"
echo "   The cookies.txt is already in the project folder."
echo "   If the bot is running, send it /refresh to reload without restart."
echo "   Or: docker compose restart yt-bot"
