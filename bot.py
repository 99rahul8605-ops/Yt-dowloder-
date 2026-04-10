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
import yt_dlp

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN    = os.environ["BOT_TOKEN"]
COOKIES_FILE = os.getenv("COOKIES_FILE", "/app/cookies.txt")
MAX_SIZE_MB  = int(os.getenv("MAX_SIZE_MB", "50"))   # Telegram Bot API limit
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/tmp/yt_downloads")
ALLOWED_USERS = set(filter(None, os.getenv("ALLOWED_USERS", "").split(",")))  # CSV of user IDs; empty = everyone

Path(DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)

YOUTUBE_REGEX = re.compile(
    r"(https?://)?(www\.)?"
    r"(youtube\.com/(watch\?v=|shorts/|embed/)|youtu\.be/)"
    r"[\w\-]{11}"
)

# ── Quality keyboard ──────────────────────────────────────────────────────────
QUALITY_OPTIONS = [
    ("🎬 Best (≤1080p)", "bestvideo[height<=1080]+bestaudio/best[height<=1080]"),
    ("📺 720p",           "bestvideo[height<=720]+bestaudio/best[height<=720]"),
    ("📱 480p",           "bestvideo[height<=480]+bestaudio/best[height<=480]"),
    ("🔊 Audio only (MP3)", "bestaudio/best"),
]


def quality_keyboard(url: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(label, callback_data=f"{fmt}|||{url}")]
        for label, fmt in QUALITY_OPTIONS
    ]
    return InlineKeyboardMarkup(buttons)


# ── Auth guard ────────────────────────────────────────────────────────────────
def is_allowed(update: Update) -> bool:
    if not ALLOWED_USERS:
        return True
    return str(update.effective_user.id) in ALLOWED_USERS


# ── yt-dlp helpers ────────────────────────────────────────────────────────────
def _base_ydl_opts(tmpdir: str, fmt: str, audio_only: bool) -> dict:
    opts: dict = {
        "outtmpl": os.path.join(tmpdir, "%(title).80s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "format": fmt,
        "merge_output_format": "mp4",
        "postprocessors": [],
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "continuedl": True,
    }

    if audio_only:
        opts["postprocessors"].append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        })
        opts.pop("merge_output_format", None)

    # cookies
    if os.path.isfile(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
        logger.info("Using cookies from %s", COOKIES_FILE)
    else:
        logger.warning("cookies.txt not found at %s – proceeding without auth", COOKIES_FILE)

    return opts


def fetch_info(url: str) -> dict:
    """Return video metadata without downloading."""
    ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    if os.path.isfile(COOKIES_FILE):
        ydl_opts["cookiefile"] = COOKIES_FILE
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)


def download_video(url: str, fmt: str, tmpdir: str) -> Path:
    """Download video/audio and return the output file path."""
    audio_only = fmt.startswith("bestaudio")
    opts = _base_ydl_opts(tmpdir, fmt, audio_only)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        # yt-dlp may merge into a different filename; find it
        expected = ydl.prepare_filename(info)

    # Resolve the actual output path (post-processors may change extension)
    candidates = sorted(Path(tmpdir).iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError("yt-dlp did not produce any output file")
    return candidates[0]


# ── Telegram handlers ─────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 *YouTube Downloader Bot*\n\n"
        "Send me any YouTube link and I'll ask which quality you want, "
        "then download and send the file right here.\n\n"
        "Commands:\n"
        "/start – show this message\n"
        "/help  – usage tips\n"
        "/cookies – check cookie auth status",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📖 *How to use*\n\n"
        "1. Paste a YouTube URL (regular video, Shorts, or playlist item).\n"
        "2. Choose a quality from the buttons.\n"
        "3. Wait while I download and send the file.\n\n"
        f"⚠️ Files larger than *{MAX_SIZE_MB} MB* cannot be sent via Telegram bots.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_cookies(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    exists = os.path.isfile(COOKIES_FILE)
    size   = os.path.getsize(COOKIES_FILE) if exists else 0
    status = f"✅ Found ({size:,} bytes)" if exists else "❌ Not found"
    await update.message.reply_text(
        f"🍪 *Cookie file status*\nPath: `{COOKIES_FILE}`\nStatus: {status}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("⛔ You are not authorised to use this bot.")
        return

    text = update.message.text.strip()
    if not YOUTUBE_REGEX.search(text):
        await update.message.reply_text("❌ Please send a valid YouTube URL.")
        return

    url = text  # keep the full URL with query params
    status_msg = await update.message.reply_text("🔍 Fetching video info…")

    try:
        info = await asyncio.get_event_loop().run_in_executor(None, fetch_info, url)
    except yt_dlp.utils.DownloadError as e:
        await status_msg.edit_text(f"❌ Could not fetch video info:\n`{e}`", parse_mode=ParseMode.MARKDOWN)
        return

    title    = info.get("title", "Unknown")
    duration = info.get("duration", 0)
    channel  = info.get("uploader", "Unknown")
    mins, secs = divmod(duration or 0, 60)

    caption = (
        f"🎬 *{title}*\n"
        f"👤 {channel}\n"
        f"⏱ {mins}:{secs:02d}\n\n"
        "Choose a quality:"
    )

    await status_msg.edit_text(caption, parse_mode=ParseMode.MARKDOWN, reply_markup=quality_keyboard(url))


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
    await query.edit_message_text(f"⬇️ Downloading: *{label}*…\nThis may take a moment.", parse_mode=ParseMode.MARKDOWN)

    tmpdir = tempfile.mkdtemp(dir=DOWNLOAD_DIR)
    try:
        # Download in executor so we don't block the event loop
        file_path: Path = await asyncio.get_event_loop().run_in_executor(
            None, download_video, url, fmt, tmpdir
        )

        size_mb = file_path.stat().st_size / (1024 * 1024)
        if size_mb > MAX_SIZE_MB:
            await query.edit_message_text(
                f"❌ File is *{size_mb:.1f} MB* — exceeds the {MAX_SIZE_MB} MB Telegram limit.\n"
                "Try a lower quality.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        await query.edit_message_text(f"📤 Uploading *{file_path.name}* ({size_mb:.1f} MB)…", parse_mode=ParseMode.MARKDOWN)
        await ctx.bot.send_chat_action(query.message.chat_id, ChatAction.UPLOAD_VIDEO)

        is_audio = file_path.suffix.lower() == ".mp3"

        with open(file_path, "rb") as fh:
            if is_audio:
                await ctx.bot.send_audio(
                    chat_id=query.message.chat_id,
                    audio=fh,
                    caption=f"🎵 {file_path.stem}",
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=30,
                )
            else:
                await ctx.bot.send_video(
                    chat_id=query.message.chat_id,
                    video=fh,
                    caption=f"🎬 {file_path.stem}",
                    supports_streaming=True,
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=30,
                )

        await query.edit_message_text("✅ Done! Enjoy your video 🎉")

    except yt_dlp.utils.DownloadError as e:
        logger.error("Download error: %s", e)
        await query.edit_message_text(f"❌ Download failed:\n`{e}`", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.exception("Unexpected error during download/upload")
        await query.edit_message_text(f"❌ Unexpected error:\n`{type(e).__name__}: {e}`", parse_mode=ParseMode.MARKDOWN)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    logger.info("Starting bot…")
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(CallbackQueryHandler(handle_quality_choice))

    logger.info("Bot is polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
