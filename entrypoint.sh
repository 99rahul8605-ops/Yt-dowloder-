#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# entrypoint.sh — validate cookies then start the bot
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

COOKIES_SRC="${COOKIES_FILE:-/app/cookies.txt}"
COOKIES_CLEAN="/tmp/_yt_bot_cookies.txt"

echo "========================================"
echo " YouTube Telegram Bot — starting up"
echo "========================================"

# ── Cookie check & sanitize ───────────────────────────────────────────────────
if [[ ! -f "$COOKIES_SRC" ]]; then
    echo "[COOKIES] ⚠️  File not found: $COOKIES_SRC"
    echo "[COOKIES]    Bot will run WITHOUT cookies (public videos only)"

elif [[ ! -s "$COOKIES_SRC" ]]; then
    echo "[COOKIES] ⚠️  File is empty: $COOKIES_SRC"
    echo "[COOKIES]    Bot will run WITHOUT cookies (public videos only)"
    echo ""
    echo "[COOKIES] FIX: On your host machine run:"
    echo "          bash get_cookies.sh chrome"
    echo "          Then: docker compose restart yt-bot"

else
    # Count data lines (non-comment, non-blank)
    DATA_LINES=$(grep -v '^#' "$COOKIES_SRC" | grep -v '^[[:space:]]*$' | wc -l || echo 0)

    if [[ "$DATA_LINES" -eq 0 ]]; then
        echo "[COOKIES] ⚠️  File has no data lines (only comments/blanks)"
        echo "[COOKIES]    Bot will run WITHOUT cookies"
    else
        echo "[COOKIES] ✅ Found $DATA_LINES cookie entries in $COOKIES_SRC"

        # Sanitize: strip BOM, fix line endings, ensure correct magic header
        python3 - "$COOKIES_SRC" "$COOKIES_CLEAN" << 'PYEOF'
import sys
from pathlib import Path

src, dst = sys.argv[1], sys.argv[2]
MAGIC = "# Netscape HTTP Cookie File"

raw = Path(src).read_bytes()
if raw.startswith(b'\xef\xbb\xbf'):          # strip UTF-8 BOM
    raw = raw[3:]
    print("[COOKIES]    Stripped UTF-8 BOM")

text = raw.decode('utf-8', errors='replace')

crlf_count = text.count('\r')
text = text.replace('\r\n', '\n').replace('\r', '\n')  # CRLF → LF
if crlf_count:
    print(f"[COOKIES]    Converted {crlf_count} CRLF → LF")

lines = [l for l in text.splitlines() if 'HTTP Cookie File' not in l]
clean = MAGIC + '\n' + '\n'.join(lines) + '\n'

Path(dst).write_text(clean, encoding='utf-8')
print(f"[COOKIES]    Sanitized → {dst}")
PYEOF

        echo "[COOKIES] ✅ Cookies ready at $COOKIES_CLEAN"
    fi
fi

echo "========================================"
echo "[BOT] Starting bot.py …"
echo "========================================"

exec python -u /app/bot.py
