import os
import re
import asyncio
import logging
import tempfile
import shutil
from pathlib import Path
from typing import List, Tuple, Optional, Any, Callable

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

# ----------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Environment variables
# ----------------------------------------------------------------------
BOT_TOKEN     = os.environ["BOT_TOKEN"]
MAX_SIZE_MB   = int(os.getenv("MAX_SIZE_MB", "50"))
DOWNLOAD_DIR  = os.getenv("DOWNLOAD_DIR", "/tmp/yt_downloads")
ALLOWED_USERS = set(filter(None, os.getenv("ALLOWED_USERS", "").split(",")))

# Optional manual cookies file (Netscape format)
COOKIES_FILE = os.getenv("COOKIES_FILE", "")  # e.g., "/app/cookies.txt"

# Webhook settings (for platforms that require a port)
PORT          = int(os.getenv("PORT", "0"))          # 0 = no webhook
WEBHOOK_URL   = os.getenv("WEBHOOK_URL", "")         # e.g., "https://your-app.com"
WEBHOOK_PATH  = os.getenv("WEBHOOK_PATH", "/webhook")# Telegram webhook path

Path(DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)

# ----------------------------------------------------------------------
# YouTube regex & quality options
# ----------------------------------------------------------------------
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

# ----------------------------------------------------------------------
# Cookie sources – strings for --cookies-from-browser
# ----------------------------------------------------------------------
def _expand_path(p: str) -> str:
    """Expand ~ to user's home directory."""
    return os.path.expanduser(p)

# Each entry is a string that yt-dlp understands for --cookies-from-browser
COOKIE_BROWSER_STRINGS = [
    f"chrome:{_expand_path('~/.var/app/com.google.Chrome')}",   # Flatpak Chrome
    "chrome",                                                    # default Chrome profile
    "firefox",                                                   # default Firefox
    "brave",                                                     # default Brave
]

def is_manual_cookies_available() -> bool:
    """Check if a manual cookies.txt file exists and is non‑empty."""
    return bool(COOKIES_FILE) and os.path.isfile(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0

def normalize_cookies_file() -> Optional[str]:
    """
    Ensure the manual cookies file is in valid Netscape format:
    - Strip BOM
    - Normalize line endings to LF
    - Ensure first line is exactly "# Netscape HTTP Cookie File"
    - Return the path to the normalized file (or None if invalid)
    """
    if not is_manual_cookies_available():
        return None
    raw = Path(COOKIES_FILE).read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    text = raw.decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.splitlines()
    # Remove any existing magic header lines (we'll add our own)
    lines = [l for l in lines if not l.startswith("# Netscape") and not l.startswith("# HTTP")]
    # Build final content
    clean = "# Netscape HTTP Cookie File\n" + "\n".join(lines) + "\n"
    # Check that there is at least one non‑empty, non‑comment line
    data_lines = [l for l in lines if l.strip() and not l.startswith("#")]
    if not data_lines:
        logger.warning("Manual cookies file has no cookie data lines")
        return None
    normalized_path = "/tmp/_yt_manual_cookies.txt"
    Path(normalized_path).write_text(clean, encoding="utf-8")
    logger.info("Manual cookies normalized: %s (%d entries)", normalized_path, len(data_lines))
    return normalized_path

def get_cookie_args() -> List[Tuple[str, Any]]:
    """
    Return a list of (method, argument) pairs to try, in order.
    For browsers: method='cookiesfrombrowser', argument is a string.
    For manual file: method='cookiefile', argument is the normalized path.
    """
    args = []
    # Browser methods
    for browser_str in COOKIE_BROWSER_STRINGS:
        args.append(("cookiesfrombrowser", browser_str))
    # Manual file method (if available and valid)
    manual_path = normalize_cookies_file()
    if manual_path:
        args.append(("cookiefile", manual_path))
    return args

# ----------------------------------------------------------------------
# Synchronous cookie fallback
# ----------------------------------------------------------------------
def run_ydl_with_cookie_fallback(
    opts_factory: Callable[[], dict],
    func: Callable[[yt_dlp.YoutubeDL], Any],
    *args,
    **kwargs
) -> Any:
    """
    Execute a yt-dlp operation with cookie fallback across browsers + manual file.
    - opts_factory: returns base yt-dlp options (without cookies)
    - func: receives a YoutubeDL instance and does the actual work
    - Returns result of func(ydl)
    - Raises if all cookie sources fail.
    """
    last_exception = None
    for method, arg in get_cookie_args():
        opts = opts_factory()
        try:
            opts[method] = arg
            with yt_dlp.YoutubeDL(opts) as ydl:
                if method == "cookiesfrombrowser":
                    logger.info("Trying cookie source: browser %s", arg)
                else:
                    logger.info("Trying cookie source: manual file %s", arg)
                result = func(ydl, *args, **kwargs)
                logger.info("Success with %s", method)
                return result
        except Exception as e:
            logger.warning("Failed with %s: %s", method, e)
            last_exception = e
            continue
    raise RuntimeError(f"All cookie sources failed. Last error: {last_exception}")

# ----------------------------------------------------------------------
# yt-dlp helpers
# ----------------------------------------------------------------------
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
    return run_ydl_with_cookie_fallback(lambda: base_ydl_opts(), _fetch)

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

# ----------------------------------------------------------------------
# Telegram Handlers
# ----------------------------------------------------------------------
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
        f"⚠️ Files over *{MAX_SIZE_MB} MB* cannot be sent via Telegram.\n\n"
        "🍪 *Cookies* – The bot automatically uses cookies from your installed browsers "
        "(Chrome, Firefox, Brave). Optionally, you can provide a manual cookies.txt file "
        "via the COOKIES_FILE environment variable.",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_cookies(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    lines = ["🍪 *Cookie Sources (fallback order)*\n"]
    for method, arg in get_cookie_args():
        if method == "cookiesfrombrowser":
            lines.append(f"• Browser: `{arg}`")
        else:
            lines.append(f"• Manual file: `{arg}`")
    lines.append("\n✅ The bot tries each source in order until one works.")
    lines.append("\n📘 *Guideline notes*:")
    lines.append("- Cookies are extracted live from browsers – no manual export needed.")
    lines.append("- Manual cookies must be in Netscape format with proper line endings.")
    lines.append("- To export cookies from a browser to a file, run:\n  `yt-dlp --cookies-from-browser chrome --cookies cookies.txt`")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def cmd_export_cookies(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Admin command to export cookies from the first working browser to a file.
    This follows the guideline's recommended export method.
    """
    if not is_allowed(update):
        await update.message.reply_text("⛔ Not authorised.")
        return
    owner_id = int(os.getenv("OWNER_ID", 0))
    if owner_id and update.effective_user.id != owner_id:
        await update.message.reply_text("⛔ Only the bot owner can export cookies.")
        return

    await update.message.reply_text("🍪 Attempting to export cookies from browser…")
    export_path = "/tmp/exported_cookies.txt"
    try:
        first_browser_str = COOKIE_BROWSER_STRINGS[0]
        def export():
            opts = {
                "cookiesfrombrowser": first_browser_str,
                "cookiefile": export_path,
                "quiet": True,
                "no_warnings": True,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info("https://www.youtube.com/watch?v=dQw4w9WgXcQ", download=False)
        await asyncio.get_event_loop().run_in_executor(None, export)
        if os.path.exists(export_path) and os.path.getsize(export_path) > 0:
            await update.message.reply_document(
                document=open(export_path, "rb"),
                filename="cookies.txt",
                caption="✅ Exported cookies (Netscape format). Handle with care!",
            )
        else:
            await update.message.reply_text("❌ Export failed – no cookies written.")
    except Exception as e:
        logger.exception("Export error")
        await update.message.reply_text(f"❌ Export failed: `{e}`", parse_mode=ParseMode.MARKDOWN)
    finally:
        if os.path.exists(export_path):
            os.unlink(export_path)

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
        await msg.edit_text(
            f"❌ Could not fetch info (all cookie sources failed):\n`{e}`",
            parse_mode=ParseMode.MARKDOWN
        )
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

# ----------------------------------------------------------------------
# Webhook & Health check
# ----------------------------------------------------------------------
async def health_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Simple health check command for uptime monitoring."""
    await update.message.reply_text("✅ Bot is alive and well!")

def setup_webhook(app: Application) -> None:
    """Configure webhook with a health check and the Telegram webhook path."""
    app.add_handler(CommandHandler("health", health_check))
    # Start the webhook
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=f"{WEBHOOK_URL}{WEBHOOK_PATH}"
    )

# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def main() -> None:
    logger.info("Cookie sources (fallback order):")
    for method, arg in get_cookie_args():
        if method == "cookiesfrombrowser":
            logger.info("  - browser: %s", arg)
        else:
            logger.info("  - manual file: %s", arg)

    # Build the Application
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(30)
        .build()
    )

    # Add all handlers
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("cookies", cmd_cookies))
    app.add_handler(CommandHandler("export_cookies", cmd_export_cookies))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(CallbackQueryHandler(handle_quality_choice))

    # Decide run mode: webhook if PORT is set and > 0
    if PORT > 0 and WEBHOOK_URL:
        logger.info("Starting in WEBHOOK mode on port %d, URL %s%s", PORT, WEBHOOK_URL, WEBHOOK_PATH)
        setup_webhook(app)
    else:
        logger.info("Starting in POLLING mode (no PORT or WEBHOOK_URL set).")
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()