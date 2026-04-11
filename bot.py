import os
import re
import asyncio
import logging
import tempfile
import shutil
import threading
from pathlib import Path

from flask import Flask, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
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

# ── Quality options ───────────────────────────────────────────────────────────
# Instead of format-filter strings (which fail when HLS/DASH streams don't
# match), we use format_sort to express the *preference* and always fall back
# to "best" so yt-dlp never hard-fails on missing streams.
#
# Each entry: (label, max_height_or_None, audio_only)
QUALITY_OPTIONS = [
    ("🎬 Best (≤1080p)", 1080,  False),
    ("📺 720p",           720,   False),
    ("📱 480p",           480,   False),
    ("🔊 Audio only MP3", None,  True),
]

# ── Player client chain ───────────────────────────────────────────────────────
# ios + mweb return HLS streams — no separate DASH video/audio tracks.
# That is fine because we no longer use bestvideo+bestaudio format strings.
PLAYER_CLIENTS = ["ios", "mweb", "android_testsuite", "android_vr", "web_creator", "web"]

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
    data_lines = [
        l for l in lines
        if l.strip() and not l.startswith("#") and len(l.split("\t")) == 7
    ]
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
        "ok":     False,
        "src":    COOKIES_FILE,
        "exists": os.path.isfile(COOKIES_FILE),
        "size":   os.path.getsize(COOKIES_FILE) if os.path.isfile(COOKIES_FILE) else 0,
    }


# ── yt-dlp helpers ────────────────────────────────────────────────────────────
def _base_opts() -> dict:
    opts: dict = {
        "quiet":          True,
        "no_warnings":    True,
        "socket_timeout": 30,
        "retries":        5,
        "extractor_args": {
            "youtube": {
                "player_client": PLAYER_CLIENTS,
            }
        },
    }
    cp = load_cookies()
    if cp:
        opts["cookiefile"] = cp
    return opts


def _build_download_opts(tmpdir: str, max_height: int | None, audio_only: bool) -> dict:
    """
    Build yt-dlp options that NEVER fail with 'format not available'.

    Key insight:
      - ios/mweb return HLS (m3u8) streams: single combined video+audio.
        bestvideo[height<=N]+bestaudio requires DASH separate streams → fails.
      - Solution: use format="best" + format_sort to express the preference.
        format_sort="res:N" picks the best stream whose height ≤ N.
        If no stream matches the sort hint, yt-dlp still picks the best available.
    """
    opts = _base_opts()
    opts.update({
        "outtmpl":          os.path.join(tmpdir, "%(title).80s.%(ext)s"),
        "noprogress":       True,
        "merge_output_format": "mp4",
        "postprocessors":   [],
        "fragment_retries": 5,
        "continuedl":       True,
    })

    if audio_only:
        # For audio: grab the best audio stream; ffmpeg converts to mp3
        opts["format"] = "bestaudio/best"
        opts["postprocessors"].append({
            "key":              "FFmpegExtractAudio",
            "preferredcodec":   "mp3",
            "preferredquality": "192",
        })
        del opts["merge_output_format"]

    elif max_height is not None:
        # Use format_sort to cap resolution — works with both HLS and DASH.
        # "res:N" = prefer streams up to N height; never hard-fails.
        # "ext:mp4" = prefer mp4 container when available.
        opts["format"]      = "best[ext=mp4]/best"
        opts["format_sort"] = [f"res:{max_height}", "ext:mp4", "tbr"]

    else:
        # Best quality — just let yt-dlp pick the top stream
        opts["format"]      = "best[ext=mp4]/best"
        opts["format_sort"] = ["res", "ext:mp4", "tbr"]

    return opts


def fetch_info(url: str) -> dict:
    opts = _base_opts()
    opts["skip_download"] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def download_video(url: str, max_height: int | None, audio_only: bool, tmpdir: str) -> Path:
    opts = _build_download_opts(tmpdir, max_height, audio_only)
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)
    candidates = sorted(
        Path(tmpdir).iterdir(), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not candidates:
        raise FileNotFoundError("yt-dlp produced no output file")
    return candidates[0]


# ── Flask health server ───────────────────────────────────────────────────────
flask_app = Flask(__name__)


@flask_app.get("/")
def index():
    s = cookie_summary()
    return jsonify({"status": "ok", "cookies": s, "clients": PLAYER_CLIENTS})


@flask_app.get("/health")
def health():
    return jsonify({"status": "ok"}), 200


def run_flask():
    logger.info("Flask health server on port %d", PORT)
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


# ── Keyboard / auth ───────────────────────────────────────────────────────────
def quality_keyboard(url: str) -> InlineKeyboardMarkup:
    # Encode max_height and audio_only into callback data
    def encode(label, max_h, audio):
        return f"{max_h or 'None'}|{int(audio)}|{url}"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=encode(label, max_h, audio))]
        for label, max_h, audio in QUALITY_OPTIONS
    ])


def decode_callback(data: str) -> tuple[int | None, bool, str]:
    parts = data.split("|", 2)
    max_h  = None if parts[0] == "None" else int(parts[0])
    audio  = bool(int(parts[1]))
    url    = parts[2]
    return max_h, audio, url


def is_allowed(update: Update) -> bool:
    return not ALLOWED_USERS or str(update.effective_user.id) in ALLOWED_USERS


# ── Handlers ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 *YouTube Downloader Bot*\n\n"
        "Send me any YouTube link, pick a quality, and I'll send the file.\n\n"
        "/start – this message\n/help – tips\n"
        "/cookies – auth status\n/refresh – reload cookies",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📖 *How to use*\n\n"
        "1. Paste a YouTube URL.\n"
        "2. Pick a quality from the buttons.\n"
        "3. Receive your file!\n\n"
        f"⚠️ Files over *{MAX_SIZE_MB} MB* cannot be sent via Telegram.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_cookies(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    s = cookie_summary()
    body = (
        f"✅ *Active* — {s['count']} cookies loaded" if s["ok"]
        else f"❌ *Not loaded*\nFile: `{s['src']}`  exists={s['exists']}  size={s['size']} B"
    )
    await update.message.reply_text(
        f"🍪 *Cookie Status*\n{body}\n\n"
        f"_Clients: {' → '.join(PLAYER_CLIENTS[:3])} …_",
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
        await msg.edit_text("❌ Reload failed. Check `cookies.txt` has valid data.",
                            parse_mode=ParseMode.MARKDOWN)


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
        info = await asyncio.get_event_loop().run_in_executor(None, fetch_info, text)
    except yt_dlp.utils.DownloadError as e:
        await msg.edit_text(f"❌ Could not fetch info:\n`{e}`",
                            parse_mode=ParseMode.MARKDOWN)
        return

    title   = info.get("title", "Unknown")
    channel = info.get("uploader", "Unknown")
    dur     = int(info.get("duration") or 0)
    m, s    = divmod(dur, 60)

    await msg.edit_text(
        f"🎬 *{title}*\n👤 {channel}  ⏱ {m}:{s:02d}\n\nChoose quality:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=quality_keyboard(text),
    )


async def handle_quality_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_allowed(update):
        await query.edit_message_text("⛔ Not authorised.")
        return

    try:
        max_height, audio_only, url = decode_callback(query.data)
    except Exception:
        await query.edit_message_text("❌ Invalid selection.")
        return

    # Find the label for display
    label = next(
        (lbl for lbl, mh, ao in QUALITY_OPTIONS
         if mh == max_height and ao == audio_only),
        "Selected quality"
    )
    await query.edit_message_text(
        f"⬇️ Downloading *{label}*…", parse_mode=ParseMode.MARKDOWN
    )

    tmpdir = tempfile.mkdtemp(dir=DOWNLOAD_DIR)
    try:
        file_path: Path = await asyncio.get_event_loop().run_in_executor(
            None, download_video, url, max_height, audio_only, tmpdir
        )

        size_mb = file_path.stat().st_size / (1024 * 1024)
        if size_mb > MAX_SIZE_MB:
            await query.edit_message_text(
                f"❌ File is *{size_mb:.1f} MB* — over the {MAX_SIZE_MB} MB Telegram limit.\n"
                "Try a lower quality.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        await query.edit_message_text(
            f"📤 Uploading *{file_path.name}* ({size_mb:.1f} MB)…",
            parse_mode=ParseMode.MARKDOWN,
        )
        await ctx.bot.send_chat_action(query.message.chat_id, ChatAction.UPLOAD_VIDEO)

        with open(file_path, "rb") as fh:
            if file_path.suffix.lower() == ".mp3":
                await ctx.bot.send_audio(
                    chat_id=query.message.chat_id, audio=fh,
                    caption=f"🎵 {file_path.stem}",
                    read_timeout=120, write_timeout=120, connect_timeout=30,
                )
            else:
                await ctx.bot.send_video(
                    chat_id=query.message.chat_id, video=fh,
                    caption=f"🎬 {file_path.stem}",
                    supports_streaming=True,
                    read_timeout=120, write_timeout=120, connect_timeout=30,
                )
        await query.edit_message_text("✅ Done! Enjoy 🎉")

    except yt_dlp.utils.DownloadError as e:
        logger.error("Download error: %s", e)
        await query.edit_message_text(
            f"❌ Download failed:\n`{e}`", parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.exception("Unexpected error")
        await query.edit_message_text(
            f"❌ Error: `{type(e).__name__}: {e}`", parse_mode=ParseMode.MARKDOWN
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(ctx.error, Conflict):
        logger.critical("Conflict — another bot instance is running. Stop it first.")
        return
    logger.error("Unhandled exception: %s", ctx.error, exc_info=ctx.error)


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    s = cookie_summary()
    logger.info("Cookie auth  : %s", f"ACTIVE — {s['count']} entries" if s["ok"] else "DISABLED")
    logger.info("Player chain : %s", " → ".join(PLAYER_CLIENTS))

    # Flask runs in a daemon thread — dies automatically when bot exits
    threading.Thread(target=run_flask, daemon=True).start()

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
    app.add_handler(CommandHandler("cookies", cmd_cookies))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(CallbackQueryHandler(handle_quality_choice))
    app.add_error_handler(error_handler)

    logger.info("Starting Telegram polling…")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        close_loop=False,
    )


if __name__ == "__main__":
    main()
