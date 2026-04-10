import os
import re
import asyncio
import logging
import tempfile
import shutil
from pathlib import Path
from typing import List, Tuple, Optional, Callable, Any

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

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN     = os.environ["BOT_TOKEN"]
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

# ── Multi‑browser cookie fallback ─────────────────────────────────────────────
# Each entry: (browser_name, profile_path_or_None)
# yt-dlp will try them in order until one succeeds.
COOKIE_BROWSERS: List[Tuple[str, Optional[str]]] = [
    ("chrome", "~/.var/app/com.google.Chrome"),   # Flatpak Chrome
    ("chrome", None),                             # default Chrome profile
    ("firefox", None),                            # default Firefox
    ("brave", None),                              # default Brave
]

def _browser_to_ydl_arg(browser: str, profile: Optional[str]) -> Any:
    """Convert (browser, profile) to format accepted by yt-dlp cookiesfrombrowser."""
    if profile:
        return (browser, profile)
    return browser

async def run_ydl_with_cookie_fallback(
    opts_factory: Callable[[], dict],
    func: Callable[[yt_dlp.YoutubeDL], Any],
    *args,
    **kwargs
) -> Any:
    """
    Execute a yt-dlp operation with cookie fallback.
    - opts_factory: function that returns base yt-dlp options (without cookiesfrombrowser)
    - func: function that receives a YoutubeDL instance and does the actual work
    - Returns the result of func(ydl)
    - Raises the last exception if all browsers fail.
    """
    last_exception = None
    for browser, profile in COOKIE_BROWSERS:
        opts = opts_factory()
        try:
            # Add cookiesfrombrowser to this attempt
            opts["cookiesfrombrowser"] = _browser_to_ydl_arg(browser, profile)
            with yt_dlp.YoutubeDL(opts) as ydl:
                logger.info("Trying cookies from browser: %s %s", browser, profile or "")
                result = func(ydl, *args, **kwargs)
                logger.info("Success with %s %s", browser, profile or "")
                return result
        except Exception as e:
            logger.warning("Failed with %s %s: %s", browser, profile or "", e)
            last_exception = e
            continue
    raise RuntimeError(f"All cookie browsers failed. Last error: {last_exception}")

# ── Helpers ───────────────────────────────────────────────────────────────────
def quality_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=f"{fmt}|||{url}")]
        for label, fmt in QUALITY_OPTIONS
    ])

def is_allowed(update: Update) -> bool:
    return not ALLOWED_USERS or str(update.effective_user.id) in ALLOWED_USERS

def base_ydl_opts(tmpdir: str = None, fmt: str = None, audio_only: bool = False) -> dict:
    """Return base yt-dlp options (without cookies)."""
    opts = {
        "quiet":               True,
        "no_warnings":         True,
        "noprogress":          True,
        "socket_timeout":      30,
        "retries":             5,
        "fragment_retries":    5,
        "continuedl":          True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        },
    }
    if tmpdir and fmt is not None:
        opts["outtmpl"] = os.path.join(tmpdir, "%(title).80s.%(ext)s")
        opts["format"] = fmt
        opts["merge_output_format"] = "mp4"
        if audio_only:
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
            opts.pop("merge_output_format", None)
        else:
            opts["postprocessors"] = []
    return opts

def fetch_info_sync(url: str) -> dict:
    """Synchronous fetch info using yt-dlp (will be called via executor)."""
    def _fetch(ydl: yt_dlp.YoutubeDL):
        return ydl.extract_info(url, download=False)

    return run_ydl_with_cookie_fallback(
        lambda: base_ydl_opts(),  # no tmpdir/fmt needed for info
        _fetch
    )

def download_video_sync(url: str, fmt: str, tmpdir: str) -> Path:
    """Synchronous download using yt-dlp."""
    audio_only = fmt.startswith("bestaudio")

    def _download(ydl: yt_dlp.YoutubeDL):
        ydl.extract_info(url, download=True)
        candidates = sorted(Path(tmpdir).iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            raise FileNotFoundError("yt-dlp produced no output file")
        return candidates[0]

    opts_factory = lambda: base_ydl_opts(tmpdir, fmt, audio_only)
    return run_ydl_with_cookie_fallback(opts_factory, _download)

# ── Handlers ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 *YouTube Downloader Bot*\n\n"
        "Send me any YouTube link and I'll let you pick a quality, "
        "then download and send the file.\n\n"
        "/start – this message\n/help – tips\n/cookies – auth status",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📖 *How to use*\n\n"
        "1. Paste a YouTube URL.\n2. Pick a quality.\n3. Wait for your file!\n\n"
        f"⚠️ Files over *{MAX_SIZE_MB} MB* cannot be sent via Telegram.",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_cookies(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    lines = ["🍪 *Cookie Sources (fallback order)*\n"]
    for browser, profile in COOKIE_BROWSERS:
        if profile:
            lines.append(f"• `{browser}:{profile}`")
        else:
            lines.append(f"• `{browser}` (default profile)")
    lines.append("\n✅ The bot tries each in order until one works.")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

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
        info = await asyncio.get_event_loop().run_in_executor(None, fetch_info_sync, text)
    except Exception as e:
        await msg.edit_text(f"❌ Could not fetch info (all cookie sources failed):\n`{e}`", parse_mode=ParseMode.MARKDOWN)
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
            None, download_video_sync, url, fmt, tmpdir
        )

        size_mb = file_path.stat().st_size / (1024 * 1024)
        if size_mb > MAX_SIZE_MB:
            await query.edit_message_text(
                f"❌ File is *{size_mb:.1f} MB* — over the {MAX_SIZE_MB} MB limit.\n"
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
        await query.edit_message_text(f"❌ Download failed:\n`{e}`", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.exception("Unexpected error")
        await query.edit_message_text(
            f"❌ Error: `{type(e).__name__}: {e}`", parse_mode=ParseMode.MARKDOWN
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    logger.info("Cookie fallback browsers: %s", COOKIE_BROWSERS)

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