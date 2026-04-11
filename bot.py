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

# ── YouTube player client chain ───────────────────────────────────────────────
# yt-dlp tries these player clients in order until one works.
# android / ios / tv_embedded do NOT trigger bot-detection even without cookies.
# web_creator is tried last as a fallback that uses cookies.
PLAYER_CLIENTS = ["android", "ios", "tv_embedded", "web_creator", "web"]

# ── Cookie sanitizer ──────────────────────────────────────────────────────────
NETSCAPE_MAGIC = "# Netscape HTTP Cookie File"
_SANITIZED_COOKIES = None


def _sanitize_cookies(src: str) -> str:
    raw = Path(src).read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):     # strip UTF-8 BOM
        raw = raw[3:]
    text = raw.decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [l for l in text.splitlines() if NETSCAPE_MAGIC not in l]
    data_lines = [l for l in lines if l.strip() and not l.startswith("#")]
    if not data_lines:
        raise ValueError("cookies.txt has no cookie data lines")
    clean = NETSCAPE_MAGIC + "\n" + "\n".join(lines) + "\n"
    dst = "/tmp/_yt_bot_cookies.txt"
    Path(dst).write_text(clean, encoding="utf-8")
    logger.info("Cookies sanitized → %s  (%d entries)", dst, len(data_lines))
    return dst


def get_cookies_path():
    global _SANITIZED_COOKIES
    if _SANITIZED_COOKIES:
        return _SANITIZED_COOKIES
    if not os.path.isfile(COOKIES_FILE) or os.path.getsize(COOKIES_FILE) == 0:
        logger.warning("No usable cookies file at %s", COOKIES_FILE)
        return None
    try:
        _SANITIZED_COOKIES = _sanitize_cookies(COOKIES_FILE)
        return _SANITIZED_COOKIES
    except Exception as exc:
        logger.error("Cookie sanitization failed: %s", exc)
        return None


# ── yt-dlp option builder ─────────────────────────────────────────────────────
def _base_opts(extra: dict = None) -> dict:
    """
    Core yt-dlp options shared by info-fetch and download.
    Key: extractor_args selects mobile/TV player clients that bypass
         YouTube's 'Sign in to confirm you're not a bot' check.
    """
    opts = {
        "quiet":       True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries":     5,
        # --- THE FIX ---
        # Use non-web player clients; these are exempt from bot verification.
        "extractor_args": {
            "youtube": {
                "player_client": PLAYER_CLIENTS,
                # Skip age-gate check (cookies handle it if needed)
                "skip": ["webpage", "configs"],
            }
        },
        "http_headers": {
            "User-Agent": (
                "com.google.android.youtube/19.09.37 "
                "(Linux; U; Android 11) gzip"
            ),
        },
    }
    cp = get_cookies_path()
    if cp:
        opts["cookiefile"] = cp
    if extra:
        opts.update(extra)
    return opts


def _download_opts(tmpdir: str, fmt: str, audio_only: bool) -> dict:
    opts = _base_opts({
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
    opts = _base_opts({"skip_download": True})
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def download_video(url: str, fmt: str, tmpdir: str) -> Path:
    audio_only = fmt.startswith("bestaudio")
    opts = _download_opts(tmpdir, fmt, audio_only)
    with yt_dlp.YoutubeDL(opts) as ydl:
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
        [InlineKeyboardButton(label, callback_data=f"{fmt}|||{url}")]
        for label, fmt in QUALITY_OPTIONS
    ])


def is_allowed(update: Update) -> bool:
    return not ALLOWED_USERS or str(update.effective_user.id) in ALLOWED_USERS


# ── Handlers ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 *YouTube Downloader Bot*\n\n"
        "Send me any YouTube link, pick a quality, and I'll send you the file.\n\n"
        "/start – this message\n/help – tips\n/cookies – auth status",
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
    cp = get_cookies_path()
    if cp:
        n = sum(
            1 for l in Path(cp).read_text().splitlines()
            if l.strip() and not l.startswith("#")
        )
        status = f"✅ Active — {n} cookies loaded"
    else:
        status = f"❌ Not loaded  (checked: {COOKIES_FILE})"
    await update.message.reply_text(
        f"🍪 *Cookie Status*\n{status}\n\n"
        "Note: bot-detection bypass via Android/TV player clients is always active "
        "regardless of cookie status.",
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


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    cp = get_cookies_path()
    logger.info("Cookie auth  : %s", f"ACTIVE ({cp})" if cp else "DISABLED")
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(CallbackQueryHandler(handle_quality_choice))

    logger.info("Bot polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
