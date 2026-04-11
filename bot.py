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
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
from telegram.constants import ParseMode, ChatAction
from telegram.error import Conflict
import yt_dlp

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

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

# (label, max_height_or_None, audio_only)
QUALITY_OPTIONS = [
    ("🎬 Best (≤1080p)", 1080, False),
    ("📺 720p",           720,  False),
    ("📱 480p",           480,  False),
    ("🔊 Audio only MP3", None, True),
]

PLAYER_CLIENTS = ["ios", "mweb", "android_testsuite", "android_vr", "web_creator", "web"]

# ── Cookies ───────────────────────────────────────────────────────────────────
NETSCAPE_MAGIC = "# Netscape HTTP Cookie File"
_sanitized_path: str | None = None

def _sanitize(src: str) -> str:
    raw = Path(src).read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    lines = [l for l in text.splitlines() if "HTTP Cookie File" not in l]
    data  = [l for l in lines if l.strip() and not l.startswith("#") and len(l.split("\t")) == 7]
    if not data:
        raise ValueError("No valid cookie lines")
    dst = "/tmp/_yt_bot_cookies.txt"
    Path(dst).write_text(NETSCAPE_MAGIC + "\n" + "\n".join(lines) + "\n", encoding="utf-8")
    logger.info("Cookies ready: %d entries → %s", len(data), dst)
    return dst

def load_cookies(force=False) -> str | None:
    global _sanitized_path
    if _sanitized_path and not force:
        return _sanitized_path
    if not os.path.isfile(COOKIES_FILE) or os.path.getsize(COOKIES_FILE) == 0:
        logger.warning("No cookies at %s", COOKIES_FILE)
        return None
    try:
        _sanitized_path = _sanitize(COOKIES_FILE)
        return _sanitized_path
    except Exception as e:
        logger.error("Cookie load failed: %s", e)
        _sanitized_path = None
        return None

def cookie_summary() -> dict:
    cp = load_cookies()
    if cp:
        n = sum(1 for l in Path(cp).read_text().splitlines() if l.strip() and not l.startswith("#"))
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
        "extractor_args": {"youtube": {"player_client": PLAYER_CLIENTS}},
    }
    cp = load_cookies()
    if cp:
        opts["cookiefile"] = cp
    return opts

def _download_opts(tmpdir: str, max_height: int | None, audio_only: bool) -> dict:
    opts = _base_opts()
    opts.update({
        "outtmpl":          os.path.join(tmpdir, "%(title).80s.%(ext)s"),
        "noprogress":       True,
        "fragment_retries": 5,
        "continuedl":       True,
        "postprocessors":   [],
    })

    if audio_only:
        # No -f, sort by audio codec preference only
        opts["format_sort"] = ["acodec:aac", "abr"]
        opts["postprocessors"].append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        })
    else:
        # Equivalent of: yt-dlp -S vcodec:h264,res:N,acodec:aac
        # NO "format" key — let format_sort pick freely
        sort_keys = ["vcodec:h264", f"res:{max_height}" if max_height else "res", "acodec:aac"]
        opts["format_sort"]        = sort_keys
        opts["merge_output_format"] = "mp4"

    return opts

def fetch_info(url: str) -> dict:
    opts = _base_opts()
    opts["skip_download"] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)

def download_video(url: str, max_height: int | None, audio_only: bool, tmpdir: str) -> Path:
    with yt_dlp.YoutubeDL(_download_opts(tmpdir, max_height, audio_only)) as ydl:
        ydl.extract_info(url, download=True)
    candidates = sorted(Path(tmpdir).iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError("yt-dlp produced no output file")
    return candidates[0]

# ── Flask ─────────────────────────────────────────────────────────────────────
flask_app = Flask(__name__)

@flask_app.get("/")
def index():
    return jsonify({"status": "ok", "cookies": cookie_summary(), "clients": PLAYER_CLIENTS})

@flask_app.get("/health")
def health():
    return jsonify({"status": "ok"}), 200

def run_flask():
    logger.info("Flask on port %d", PORT)
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# ── Keyboards ─────────────────────────────────────────────────────────────────
def quality_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(lbl, callback_data=f"{mh or 'None'}|{int(ao)}|{url}")]
        for lbl, mh, ao in QUALITY_OPTIONS
    ])

def decode_cb(data: str) -> tuple[int | None, bool, str]:
    h, a, url = data.split("|", 2)
    return (None if h == "None" else int(h)), bool(int(a)), url

def is_allowed(u: Update) -> bool:
    return not ALLOWED_USERS or str(u.effective_user.id) in ALLOWED_USERS

# ── Handlers ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 *YouTube Downloader Bot*\n\nSend me a YouTube link, pick quality, get the file.\n\n"
        "/start /help /cookies /refresh",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📖 Paste a YouTube URL → pick quality → receive file.\n\n"
        f"⚠️ Max file size: *{MAX_SIZE_MB} MB*",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_cookies(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update): return
    s = cookie_summary()
    body = f"✅ Active — {s['count']} cookies" if s["ok"] else f"❌ Not loaded ({s['src']})"
    await update.message.reply_text(f"🍪 *Cookies:* {body}", parse_mode=ParseMode.MARKDOWN)

async def cmd_refresh(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update): return
    msg = await update.message.reply_text("🔄 Reloading…")
    load_cookies(force=True)
    s = cookie_summary()
    await msg.edit_text(f"✅ {s['count']} cookies loaded" if s["ok"] else "❌ Failed")

async def handle_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("⛔ Not authorised.")
        return
    text = update.message.text.strip()
    if not YOUTUBE_REGEX.search(text):
        await update.message.reply_text("❌ Send a valid YouTube URL.")
        return

    msg = await update.message.reply_text("🔍 Fetching info…")
    try:
        info = await asyncio.get_event_loop().run_in_executor(None, fetch_info, text)
    except yt_dlp.utils.DownloadError as e:
        await msg.edit_text(f"❌ `{e}`", parse_mode=ParseMode.MARKDOWN)
        return

    title = info.get("title", "Unknown")
    ch    = info.get("uploader", "?")
    m, s  = divmod(int(info.get("duration") or 0), 60)
    await msg.edit_text(
        f"🎬 *{title}*\n👤 {ch}  ⏱ {m}:{s:02d}\n\nChoose quality:",
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
        max_h, audio, url = decode_cb(query.data)
    except Exception:
        await query.edit_message_text("❌ Invalid selection.")
        return

    label = next((l for l, mh, ao in QUALITY_OPTIONS if mh == max_h and ao == audio), "?")
    await query.edit_message_text(f"⬇️ Downloading *{label}*…", parse_mode=ParseMode.MARKDOWN)

    tmpdir = tempfile.mkdtemp(dir=DOWNLOAD_DIR)
    try:
        fp: Path = await asyncio.get_event_loop().run_in_executor(
            None, download_video, url, max_h, audio, tmpdir
        )
        size_mb = fp.stat().st_size / (1024 * 1024)

        if size_mb > MAX_SIZE_MB:
            await query.edit_message_text(
                f"❌ *{size_mb:.1f} MB* — too large. Try lower quality.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        await query.edit_message_text(
            f"📤 Uploading *{fp.name}* ({size_mb:.1f} MB)…", parse_mode=ParseMode.MARKDOWN
        )
        await ctx.bot.send_chat_action(query.message.chat_id, ChatAction.UPLOAD_VIDEO)

        with open(fp, "rb") as fh:
            if fp.suffix.lower() == ".mp3":
                await ctx.bot.send_audio(
                    chat_id=query.message.chat_id, audio=fh,
                    caption=f"🎵 {fp.stem}",
                    read_timeout=120, write_timeout=120, connect_timeout=30,
                )
            else:
                await ctx.bot.send_video(
                    chat_id=query.message.chat_id, video=fh,
                    caption=f"🎬 {fp.stem}", supports_streaming=True,
                    read_timeout=120, write_timeout=120, connect_timeout=30,
                )
        await query.edit_message_text("✅ Done! Enjoy 🎉")

    except yt_dlp.utils.DownloadError as e:
        logger.error("DL error: %s", e)
        await query.edit_message_text(f"❌ `{e}`", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.exception("Unexpected")
        await query.edit_message_text(f"❌ `{type(e).__name__}: {e}`", parse_mode=ParseMode.MARKDOWN)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(ctx.error, Conflict):
        logger.critical("Conflict — stop all other bot instances first.")
        return
    logger.error("Error: %s", ctx.error, exc_info=ctx.error)

# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    s = cookie_summary()
    logger.info("Cookies : %s", f"ACTIVE {s['count']} entries" if s["ok"] else "DISABLED")
    logger.info("Clients : %s", " → ".join(PLAYER_CLIENTS))

    threading.Thread(target=run_flask, daemon=True).start()

    app = (
        Application.builder().token(BOT_TOKEN)
        .read_timeout(60).write_timeout(60).connect_timeout(30)
        .build()
    )
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("cookies", cmd_cookies))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(CallbackQueryHandler(handle_quality_choice))
    app.add_error_handler(error_handler)

    logger.info("Polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True, close_loop=False)

if __name__ == "__main__":
    main()
