import os
import re
import asyncio
import logging
import tempfile
import shutil
from pathlib import Path

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

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN     = os.environ["BOT_TOKEN"]
COOKIES_FILE  = os.getenv("COOKIES_FILE", "/app/cookies.txt")
MAX_SIZE_MB   = int(os.getenv("MAX_SIZE_MB", "50"))
DOWNLOAD_DIR  = os.getenv("DOWNLOAD_DIR", "/tmp/yt_downloads")
ALLOWED_USERS = set(filter(None, os.getenv("ALLOWED_USERS", "").split(",")))

Path(DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)

YOUTUBE_REGEX = re.compile(
    r"(https?://)?(www\.)?"
    r"(youtube\.com/(watch\?v=|shorts/|embed/)|youtu\.be/)"
    r"[\w\-]{11}"
)

QUALITY_OPTIONS = [
    ("🎬 Best (≤1080p)", "bestvideo[height<=1080]+bestaudio/best[height<=1080]"),
    ("📺 720p",           "bestvideo[height<=720]+bestaudio/best[height<=720]"),
    ("📱 480p",           "bestvideo[height<=480]+bestaudio/best[height<=480]"),
    ("🔊 Audio only (MP3)", "bestaudio/best"),
]

# ── Player clients ────────────────────────────────────────────────────────────
# yt-dlp tries these in order until one returns a valid stream.
#
# Why these specific clients:
#   ios              — Apple iOS app API; not subject to bot-verification
#   mweb             — YouTube mobile web; minimal bot-checking
#   android_testsuite— internal test client; bypasses "not supported" error
#   android_vr       — VR client; also exempt from standard bot checks
#   web_creator      — YouTube Studio; less restricted than plain web
#   web              — standard fallback; uses cookies if available
#
# The "no longer supported" error happens when the android client (deprecated)
# or web client without valid cookies is tried first.  ios + mweb avoid this.
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
    return {"ok": False, "src": COOKIES_FILE,
            "exists": os.path.isfile(COOKIES_FILE),
            "size": os.path.getsize(COOKIES_FILE) if os.path.isfile(COOKIES_FILE) else 0}


# ── yt-dlp ────────────────────────────────────────────────────────────────────
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


def _download_opts(tmpdir: str, fmt: str, audio_only: bool) -> dict:
    opts = _base_opts()
    opts.update({
        "outtmpl":             os.path.join(tmpdir, "%(title).80s.%(ext)s"),
        "noprogress":          True,
        "format":              fmt,
        "merge_output_format": "mp4",
        "postprocessors":      [],
        "fragment_retries":    5,
        "continuedl":          True,
    })
    if audio_only:
        opts["postprocessors"].append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        })
        del opts["merge_output_format"]
    return opts


def fetch_info(url: str) -> dict:
    opts = _base_opts()
    opts["skip_download"] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def download_video(url: str, fmt: str, tmpdir: str) -> Path:
    audio_only = fmt.startswith("bestaudio")
    with yt_dlp.YoutubeDL(_download_opts(tmpdir, fmt, audio_only)) as ydl:
        ydl.extract_info(url, download=True)
    candidates = sorted(
        Path(tmpdir).iterdir(), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not candidates:
        raise FileNotFoundError("yt-dlp produced no output file")
    return candidates[0]


# ── Keyboards / guards ────────────────────────────────────────────────────────
def quality_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(lbl, callback_data=f"{fmt}|||{url}")]
        for lbl, fmt in QUALITY_OPTIONS
    ])


def is_allowed(update: Update) -> bool:
    return not ALLOWED_USERS or str(update.effective_user.id) in ALLOWED_USERS


# ── Command handlers ──────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 *YouTube Downloader Bot*\n\n"
        "Send me any YouTube link, pick a quality, and I'll send you the file.\n\n"
        "/start – this message\n/help – tips\n/cookies – auth status\n/refresh – reload cookies",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📖 *How to use*\n\n"
        "1. Paste a YouTube URL.\n"
        "2. Pick quality from the buttons.\n"
        "3. Receive your file!\n\n"
        f"⚠️ Files over *{MAX_SIZE_MB} MB* cannot be sent via Telegram.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_cookies(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    s = cookie_summary()
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
        await msg.edit_text(
            "❌ Reload failed. Check that `cookies.txt` has valid data.",
            parse_mode=ParseMode.MARKDOWN,
        )


# ── URL handler ───────────────────────────────────────────────────────────────
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
        await msg.edit_text(f"❌ Could not fetch info:\n`{e}`", parse_mode=ParseMode.MARKDOWN)
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


# ── Quality callback ──────────────────────────────────────────────────────────
async def handle_quality_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not is_allowed(update):
        await query.edit_message_text("⛔ Not authorised.")
        return

    try:
        fmt, url = query.data.split("|||", 1)
    except ValueError:
        await query.edit_message_text("❌ Invalid selection.")
        return

    label = next((lbl for lbl, f in QUALITY_OPTIONS if f == fmt), fmt)
    await query.edit_message_text(
        f"⬇️ Downloading *{label}*…", parse_mode=ParseMode.MARKDOWN
    )

    tmpdir = tempfile.mkdtemp(dir=DOWNLOAD_DIR)
    try:
        file_path: Path = await asyncio.get_event_loop().run_in_executor(
            None, download_video, url, fmt, tmpdir
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


# ── Error handler ─────────────────────────────────────────────────────────────
async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(ctx.error, Conflict):
        logger.critical(
            "Conflict error — another bot instance is running. "
            "Shut down all other instances before starting this one."
        )
        # Don't crash the process; log and let the loop recover
        return
    logger.error("Unhandled exception: %s", ctx.error, exc_info=ctx.error)


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    s = cookie_summary()
    if s["ok"]:
        logger.info("Cookie auth  : ACTIVE — %d entries", s["count"])
    else:
        logger.warning("Cookie auth  : DISABLED")
    logger.info("Player chain : %s", " → ".join(PLAYER_CLIENTS))

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

    logger.info("Starting polling (drop_pending_updates=True)…")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,   # discard stale messages from previous runs
        close_loop=False,
    )


if __name__ == "__main__":
    main()
