import os
import re
import asyncio
import logging
import tempfile
import shutil
import threading
from pathlib import Path
import io
from contextlib import redirect_stderr

from flask import Flask, jsonify
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode, ChatAction
from telegram.error import Conflict
import yt_dlp

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN     = os.environ["BOT_TOKEN"]
COOKIES_FILE  = os.getenv("COOKIES_FILE", "/app/cookies.txt")
MAX_SIZE_MB   = int(os.getenv("MAX_SIZE_MB", "50"))
DOWNLOAD_DIR  = os.getenv("DOWNLOAD_DIR", "/tmp/yt_downloads")
ALLOWED_USERS = set(filter(None, os.getenv("ALLOWED_USERS", "").split(",")))
PORT          = int(os.getenv("PORT", "8080"))

Path(DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)

YOUTUBE_REGEX = re.compile(
    r"(https?://)?(www\.)?"
    r"(youtube\.com/(watch\?v=|shorts/|embed/)|youtu\.be/)"
    r"[\w\-]{11}"
)

# ── Cookie manager ────────────────────────────────────────────────────────────
NETSCAPE_MAGIC = "# Netscape HTTP Cookie File"
_sanitized_path: str | None = None

def _sanitize_and_validate(src: str) -> str:
    raw = Path(src).read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    text = raw.decode("utf-8", errors="replace")
    crlf = text.count("\r")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if crlf:
        logger.info("Converted %d CRLF → LF in cookies", crlf)
    lines = [l for l in text.splitlines() if "HTTP Cookie File" not in l]
    data_lines = [l for l in lines if l.strip() and not l.startswith("#")
                  and len(l.split("\t")) == 7]
    if not data_lines:
        raise ValueError("No valid 7-field cookie lines found")
    clean = NETSCAPE_MAGIC + "\n" + "\n".join(lines) + "\n"
    dst = "/tmp/_yt_bot_cookies.txt"
    Path(dst).write_text(clean, encoding="utf-8")
    logger.info("Cookies sanitized → %s (%d entries)", dst, len(data_lines))
    return dst

def load_cookies(force: bool = False) -> str | None:
    global _sanitized_path
    if _sanitized_path and not force:
        return _sanitized_path
    if not os.path.isfile(COOKIES_FILE) or os.path.getsize(COOKIES_FILE) == 0:
        logger.warning("No usable cookies file at %s", COOKIES_FILE)
        return None
    try:
        _sanitized_path = _sanitize_and_validate(COOKIES_FILE)
        return _sanitized_path
    except Exception as e:
        logger.error("Cookie load failed: %s", e)
        _sanitized_path = None
        return None

def cookie_summary() -> dict:
    cp = load_cookies()
    if cp:
        lines = Path(cp).read_text().splitlines()
        n = sum(1 for l in lines if l.strip() and not l.startswith("#"))
        return {"ok": True, "count": n}
    return {
        "ok": False,
        "src": COOKIES_FILE,
        "exists": os.path.isfile(COOKIES_FILE),
        "size": os.path.getsize(COOKIES_FILE) if os.path.isfile(COOKIES_FILE) else 0,
    }

# ── yt-dlp options – fixed: no forced client, robust format ──────────────────
def _base_opts() -> dict:
    """Base options: ignore config, verbose, no forced player client."""
    opts: dict = {
        "ignoreconfig":   True,       # critical: prevent external -f
        "verbose":        True,
        "quiet":          False,
        "no_warnings":    False,
        "socket_timeout": 30,
        "retries":        5,
        # No extractor_args -> let yt-dlp choose its default clients (android, web)
    }
    cp = load_cookies()
    if cp:
        opts["cookiefile"] = cp
    return opts

def _download_opts(tmpdir: str) -> dict:
    opts = _base_opts()
    opts.update({
        "outtmpl":             os.path.join(tmpdir, "%(title).80s.%(ext)s"),
        "noprogress":          False,
        # The robust format selector that always works
        "format":              "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "fragment_retries":    5,
        "continuedl":          True,
        "postprocessors":      [],
    })
    return opts

def fetch_info_with_logs(url: str) -> tuple[dict, str]:
    opts = _base_opts()
    opts["skip_download"] = True
    log_capture = io.StringIO()
    with redirect_stderr(log_capture):
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    return info, log_capture.getvalue()

def list_formats_with_logs(url: str) -> tuple[str, str]:
    """Return formatted format list (like -F) and verbose logs."""
    opts = _base_opts()
    opts["listformats"] = True
    log_capture = io.StringIO()
    out_capture = io.StringIO()
    with redirect_stderr(log_capture), redirect_stdout(out_capture):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info(url, download=False)
        except SystemExit:
            pass  # yt-dlp exits after listing formats
    return out_capture.getvalue(), log_capture.getvalue()

def download_video_with_logs(url: str, tmpdir: str) -> tuple[Path, str]:
    opts = _download_opts(tmpdir)
    log_capture = io.StringIO()
    with redirect_stderr(log_capture):
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)
    candidates = sorted(
        Path(tmpdir).iterdir(), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not candidates:
        raise FileNotFoundError("yt-dlp produced no output file")
    return candidates[0], log_capture.getvalue()

# ── Flask health server ───────────────────────────────────────────────────────
flask_app = Flask(__name__)

@flask_app.route("/")
def health():
    s = cookie_summary()
    return jsonify({
        "status":  "ok",
        "bot":     "running",
        "cookies": s,
    })

@flask_app.route("/health")
def health_check():
    return jsonify({"status": "ok"}), 200

def run_flask():
    logger.info("Flask health server listening on port %d", PORT)
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# ── Guards ────────────────────────────────────────────────────────────────────
def is_allowed(update: Update) -> bool:
    return not ALLOWED_USERS or str(update.effective_user.id) in ALLOWED_USERS

# ── Handlers ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 *YouTube Downloader Bot*\n\n"
        "Send me any YouTube link – I'll download the best quality.\n\n"
        "/start – this message\n/help – tips\n/formats <url> – list available formats\n/cookies – auth status\n/refresh – reload cookies",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📖 *How to use*\n\n"
        "1. Paste a YouTube URL.\n"
        "2. Wait for download and upload.\n"
        "3. Enjoy your video!\n\n"
        f"⚠️ Files over *{MAX_SIZE_MB} MB* cannot be sent.\n\n"
        "If you get errors:\n"
        "• Try `/refresh` (cookies)\n"
        "• Use `/formats <url>` to see available qualities\n"
        "• Update yt‑dlp on the server (see /cookies for version info)",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_formats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """List all available formats for a given YouTube URL."""
    if not is_allowed(update):
        await update.message.reply_text("⛔ Not authorised.")
        return
    args = ctx.args
    if not args:
        await update.message.reply_text("❌ Usage: `/formats <youtube_url>`", parse_mode=ParseMode.MARKDOWN)
        return
    url = args[0]
    if not YOUTUBE_REGEX.search(url):
        await update.message.reply_text("❌ That doesn't look like a valid YouTube URL.")
        return

    msg = await update.message.reply_text("🔍 Fetching format list...")
    try:
        formats_output, logs = await asyncio.get_event_loop().run_in_executor(
            None, list_formats_with_logs, url
        )
        logger.info(f"Formats list for {url}:\n{logs[:500]}")
    except Exception as e:
        await msg.edit_text(f"❌ Failed to list formats:\n`{e}`", parse_mode=ParseMode.MARKDOWN)
        return

    if not formats_output.strip():
        await msg.edit_text("❌ No formats returned. Check server logs.")
        return

    # Telegram message limit: 4096 chars
    if len(formats_output) > 4000:
        # Send as a file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write(formats_output)
            tmp_path = f.name
        with open(tmp_path, 'rb') as fh:
            await update.message.reply_document(
                document=fh,
                filename="formats.txt",
                caption=f"📋 Available formats for {url}"
            )
        os.unlink(tmp_path)
        await msg.delete()
    else:
        await msg.edit_text(f"```\n{formats_output}\n```", parse_mode=ParseMode.MARKDOWN)

async def cmd_cookies(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    s = cookie_summary()
    # Try to get yt-dlp version
    import yt_dlp.version
    version = getattr(yt_dlp.version, '__version__', 'unknown')
    if s["ok"]:
        body = f"✅ *Active* — {s['count']} cookies loaded"
    else:
        body = (
            f"❌ *Not loaded*\n"
            f"File: `{s['src']}`  exists={s['exists']}  size={s['size']} bytes\n"
            "Run `bash get_cookies.sh chrome` then send /refresh"
        )
    await update.message.reply_text(
        f"🍪 *Cookie Status*\n{body}\n\n"
        f"📦 *yt-dlp version*: `{version}`\n"
        f"_If version is older than 2025.04, update it._\n\n"
        f"_Expired cookies cause 'Precondition check failed' errors._",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_refresh(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    msg = await update.message.reply_text("🔄 Reloading cookies…")
    result = load_cookies(force=True)
    s = cookie_summary()
    if result:
        await msg.edit_text(f"✅ Reloaded — {s['count']} cookies active")
    else:
        await msg.edit_text(
            "❌ Reload failed. Check that `cookies.txt` has valid data.",
            parse_mode=ParseMode.MARKDOWN,
        )

async def handle_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("⛔ Not authorised.")
        return
    text = update.message.text.strip()
    if not YOUTUBE_REGEX.search(text):
        await update.message.reply_text("❌ Please send a valid YouTube URL.")
        return

    msg = await update.message.reply_text("🔍 Fetching video info…")
    try:
        info, ytdlp_logs = await asyncio.get_event_loop().run_in_executor(
            None, fetch_info_with_logs, text
        )
        logger.info("yt-dlp fetch logs (first 2000 chars):\n%s", ytdlp_logs[:2000])
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Fetch info failed: {error_msg}")
        await msg.edit_text(
            f"❌ Could not fetch video info.\n"
            f"Error: `{error_msg}`\n\n"
            "Possible fixes:\n"
            "• Update yt‑dlp on the server\n"
            "• Refresh cookies: `/refresh`\n"
            "• Use `/formats <url>` to see available formats\n\n"
            "Check server logs for full yt-dlp output.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    title   = info.get("title", "Unknown")
    channel = info.get("uploader", "Unknown")
    dur     = int(info.get("duration") or 0)
    m, s    = divmod(dur, 60)

    await msg.edit_text(
        f"🎬 *{title}*\n👤 {channel}  ⏱ {m}:{s:02d}\n\n⬇️ Downloading best quality…",
        parse_mode=ParseMode.MARKDOWN,
    )

    tmpdir = tempfile.mkdtemp(dir=DOWNLOAD_DIR)
    try:
        file_path, dl_logs = await asyncio.get_event_loop().run_in_executor(
            None, download_video_with_logs, text, tmpdir
        )
        logger.info("yt-dlp download logs (first 2000 chars):\n%s", dl_logs[:2000])

        size_mb = file_path.stat().st_size / (1024 * 1024)
        if size_mb > MAX_SIZE_MB:
            await msg.edit_text(
                f"❌ File is *{size_mb:.1f} MB* — over the {MAX_SIZE_MB} MB Telegram limit.\n"
                "Use `/formats <url>` to pick a lower quality and download manually.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        await msg.edit_text(
            f"📤 Uploading *{file_path.name}* ({size_mb:.1f} MB)…",
            parse_mode=ParseMode.MARKDOWN,
        )
        await ctx.bot.send_chat_action(msg.chat_id, ChatAction.UPLOAD_VIDEO)

        with open(file_path, "rb") as fh:
            await ctx.bot.send_video(
                chat_id=msg.chat_id, video=fh,
                caption=f"🎬 {file_path.stem}",
                supports_streaming=True,
                read_timeout=120, write_timeout=120, connect_timeout=30,
            )
        await msg.edit_text("✅ Done! Enjoy 🎉")

    except yt_dlp.utils.DownloadError as e:
        logger.error("Download error: %s", e)
        await msg.edit_text(f"❌ Download failed:\n`{e}`\n\nTry `/formats {text}` to see available formats.", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.exception("Unexpected error")
        await msg.edit_text(f"❌ Error: `{type(e).__name__}: {e}`", parse_mode=ParseMode.MARKDOWN)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(ctx.error, Conflict):
        logger.critical("Conflict — another bot instance is running. Stop it first.")
        return
    logger.error("Unhandled exception: %s", ctx.error, exc_info=ctx.error)

def main() -> None:
    s = cookie_summary()
    if s["ok"]:
        logger.info("Cookie auth  : ACTIVE — %d entries", s["count"])
    else:
        logger.warning("Cookie auth  : DISABLED")

    import yt_dlp.version
    logger.info("yt-dlp version: %s", getattr(yt_dlp.version, '__version__', 'unknown'))

    t = threading.Thread(target=run_flask, daemon=True)
    t.start()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(30)
        .build()
    )

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("formats", cmd_formats))
    app.add_handler(CommandHandler("cookies", cmd_cookies))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_error_handler(error_handler)

    logger.info("Starting Telegram polling…")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        close_loop=False,
    )

if __name__ == "__main__":
    main()