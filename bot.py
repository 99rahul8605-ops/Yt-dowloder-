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

# Player client chain: mobile/TV clients bypass bot-detection entirely.
# Per yt-dlp docs, these clients do not require cookie authentication.
PLAYER_CLIENTS = ["android", "ios", "tv_embedded", "web_creator", "web"]

# ── Cookie manager ────────────────────────────────────────────────────────────
# Per yt-dlp docs:
#   • First line MUST be "# HTTP Cookie File" or "# Netscape HTTP Cookie File"
#   • Line endings MUST be LF (\n) on Linux — CRLF causes HTTP 400 errors
#   • Format is Mozilla/Netscape tab-separated (7 fields per line)

NETSCAPE_MAGIC = "# Netscape HTTP Cookie File"
_sanitized_path: str | None = None   # module-level cache; cleared by /refresh


def _sanitize_and_validate(src: str) -> str:
    """
    Read src, apply all corrections required by yt-dlp docs, write a clean
    copy to /tmp/_yt_bot_cookies.txt and return its path.

    Corrections applied:
      1. Strip UTF-8 BOM (causes header mismatch)
      2. Normalise CRLF → LF  (CRLF causes HTTP 400 per docs)
      3. Remove any duplicate/malformed magic header lines
      4. Prepend exactly one correct magic header as line 1
      5. Validate at least one 7-field tab-separated data line exists
    """
    raw = Path(src).read_bytes()

    # 1. Strip BOM
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
        logger.debug("Stripped UTF-8 BOM from cookies file")

    text = raw.decode("utf-8", errors="replace")

    # 2. Normalise line endings → LF only
    before = text.count("\r")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if before:
        logger.info("Converted %d CRLF → LF in cookies file", before)

    lines = text.splitlines()

    # 3. Remove any existing magic header lines (avoids duplicates)
    lines = [l for l in lines if "HTTP Cookie File" not in l]

    # 4. Validate data lines (must have 7 tab-separated fields)
    data_lines = []
    bad_lines  = []
    for l in lines:
        if not l.strip() or l.startswith("#"):
            continue
        fields = l.split("\t")
        if len(fields) == 7:
            data_lines.append(l)
        else:
            bad_lines.append(l)

    if bad_lines:
        logger.warning("Skipping %d malformed cookie lines (wrong field count)", len(bad_lines))

    if not data_lines:
        raise ValueError(
            "cookies.txt contains no valid 7-field Netscape cookie lines. "
            "Re-export using: bash get_cookies.sh"
        )

    # 5. Build clean output: magic header MUST be first
    clean = NETSCAPE_MAGIC + "\n" + "\n".join(lines) + "\n"

    dst = "/tmp/_yt_bot_cookies.txt"
    Path(dst).write_text(clean, encoding="utf-8")
    logger.info(
        "Cookies ready: %d valid entries, %d comments/blanks → %s",
        len(data_lines), len(lines) - len(data_lines), dst,
    )
    return dst


def load_cookies(force: bool = False) -> str | None:
    """
    Return path to sanitized cookies file, or None if unavailable.
    Uses module-level cache; pass force=True to reload (used by /refresh).
    """
    global _sanitized_path

    if _sanitized_path and not force:
        return _sanitized_path

    if not os.path.isfile(COOKIES_FILE):
        logger.warning("Cookies file not found: %s", COOKIES_FILE)
        return None

    if os.path.getsize(COOKIES_FILE) == 0:
        logger.warning("Cookies file is empty: %s", COOKIES_FILE)
        return None

    try:
        _sanitized_path = _sanitize_and_validate(COOKIES_FILE)
        return _sanitized_path
    except Exception as exc:
        logger.error("Cookie load failed: %s", exc)
        _sanitized_path = None
        return None


def cookie_summary() -> dict:
    """Return a dict with status info for display to the user."""
    src_exists = os.path.isfile(COOKIES_FILE)
    src_size   = os.path.getsize(COOKIES_FILE) if src_exists else 0
    cp         = load_cookies()

    if cp:
        lines      = Path(cp).read_text().splitlines()
        data_count = sum(1 for l in lines if l.strip() and not l.startswith("#"))
        return {
            "ok": True,
            "data_count": data_count,
            "src": COOKIES_FILE,
            "src_size": src_size,
        }
    return {
        "ok": False,
        "src": COOKIES_FILE,
        "src_exists": src_exists,
        "src_size": src_size,
    }


# ── yt-dlp builders ───────────────────────────────────────────────────────────
def _base_ydl_opts() -> dict:
    """
    Core options applied to every yt-dlp call.

    extractor_args explanation (per yt-dlp docs + YouTube extractor source):
      player_client = list of clients tried in order.
        "android" / "ios" — mobile API; YouTube does NOT apply bot-checks here.
        "tv_embedded"     — embedded TV client; also exempt.
        "web_creator"     — YouTube Studio client; less restricted than plain web.
        "web"             — standard web; uses cookies for auth if available.
    """
    opts: dict = {
        "quiet":          True,
        "no_warnings":    True,
        "socket_timeout": 30,
        "retries":        5,
        "extractor_args": {
            "youtube": {
                # Try mobile/TV clients first — they bypass bot-verification
                "player_client": PLAYER_CLIENTS,
            }
        },
    }
    cp = load_cookies()
    if cp:
        opts["cookiefile"] = cp   # used by the web fallback client
    return opts


def _download_opts(tmpdir: str, fmt: str, audio_only: bool) -> dict:
    opts = _base_ydl_opts()
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
    opts = _base_ydl_opts()
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
        "/start – this message\n"
        "/help – usage tips\n"
        "/cookies – cookie auth status\n"
        "/refresh – reload cookies without restarting",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📖 *How to use*\n\n"
        "1. Paste a YouTube URL (video, Shorts, playlist item).\n"
        "2. Pick quality from the buttons.\n"
        "3. Receive your file!\n\n"
        f"⚠️ Files over *{MAX_SIZE_MB} MB* cannot be sent via Telegram.\n\n"
        "*Cookie tips:*\n"
        "Run `bash get_cookies.sh chrome` on the host to refresh cookies, "
        "then send `/refresh` to the bot — no restart needed.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_cookies(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    s = cookie_summary()
    if s["ok"]:
        body = (
            f"✅ *Active* — {s['data_count']} cookies loaded\n"
            f"Source: `{s['src']}` ({s['src_size']:,} bytes)"
        )
    else:
        body = (
            f"❌ *Not loaded*\n"
            f"Source: `{s['src']}`\n"
            f"Exists: {s.get('src_exists', False)}  Size: {s.get('src_size', 0):,} bytes\n\n"
            "Run `bash get_cookies.sh chrome` on the host, then send /refresh"
        )
    await update.message.reply_text(
        f"🍪 *Cookie Status*\n{body}\n\n"
        f"_Player clients: {' → '.join(PLAYER_CLIENTS)}_",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_refresh(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Reload cookies.txt from disk without restarting the bot."""
    if not is_allowed(update):
        return
    msg = await update.message.reply_text("🔄 Reloading cookies…")
    s_before = cookie_summary()
    result = load_cookies(force=True)
    s_after  = cookie_summary()

    if result:
        await msg.edit_text(
            f"✅ Cookies reloaded!\n"
            f"Before: {'active' if s_before['ok'] else 'inactive'}\n"
            f"After : {s_after['data_count']} cookies active",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await msg.edit_text(
            "❌ Cookie reload failed.\n"
            "Make sure `cookies.txt` is mounted correctly and re-run `get_cookies.sh`.",
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
        await msg.edit_text(
            f"❌ Could not fetch info:\n`{e}`", parse_mode=ParseMode.MARKDOWN
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


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    cp = load_cookies()
    s  = cookie_summary()
    if s["ok"]:
        logger.info("Cookie auth  : ACTIVE — %d entries", s["data_count"])
    else:
        logger.warning("Cookie auth  : DISABLED (only public videos available)")
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

    logger.info("Bot polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
