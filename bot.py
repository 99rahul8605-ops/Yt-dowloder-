import os
import re
import asyncio
import logging
import tempfile
import shutil
import threading
from pathlib import Path
import io
from contextlib import redirect_stderr, redirect_stdout

from flask import Flask, jsonify
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.constants import ParseMode, ChatAction
from telegram.error import Conflict
import yt_dlp

# ── Logging ─────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not set")

MAX_SIZE_MB = int(os.getenv("MAX_SIZE_MB", "2000"))  # Telegram supports up to 2GB
DOWNLOAD_DIR = "/tmp/yt_downloads"
PORT = int(os.getenv("PORT", "8080"))

Path(DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)

# Better regex
YOUTUBE_REGEX = re.compile(r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/.+")

# ── Check ffmpeg ───────────────────────────────────────────────
if not shutil.which("ffmpeg"):
    logger.warning("⚠️ ffmpeg not installed — merging may fail!")

# ── yt-dlp options (FIXED CORE) ────────────────────────────────
def _base_opts():
    return {
        "quiet": False,
        "no_warnings": False,
        "retries": 5,
        "socket_timeout": 30,
    }

def _download_opts(tmpdir):
    opts = _base_opts()
    opts.update({
        "outtmpl": os.path.join(tmpdir, "%(title).80s.%(ext)s"),

        # 🔥 FIXED FORMAT LOGIC
        "format": "bv*+ba/best",

        "merge_output_format": "mp4",
        "noplaylist": True,
        "continuedl": True,
    })
    return opts

# ── yt-dlp helpers ─────────────────────────────────────────────
def fetch_info(url):
    opts = _base_opts()
    opts["skip_download"] = True

    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)

def download_video(url, tmpdir):
    opts = _download_opts(tmpdir)

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)

    files = list(Path(tmpdir).glob("*"))
    if not files:
        raise Exception("No file downloaded")

    return max(files, key=lambda f: f.stat().st_mtime)

# ── Flask health ───────────────────────────────────────────────
flask_app = Flask(__name__)

@flask_app.route("/")
def health():
    return jsonify({"status": "ok"})

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT)

# ── Handlers ──────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send YouTube link 🎬")

async def handle_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    if not YOUTUBE_REGEX.search(url):
        await update.message.reply_text("❌ Invalid URL")
        return

    msg = await update.message.reply_text("🔍 Fetching info...")

    try:
        info = await asyncio.get_event_loop().run_in_executor(None, fetch_info, url)
    except Exception as e:
        await msg.edit_text(f"❌ Error fetching info:\n{e}")
        return

    title = info.get("title", "Unknown")

    await msg.edit_text(f"⬇️ Downloading:\n{title}")

    tmpdir = tempfile.mkdtemp(dir=DOWNLOAD_DIR)

    try:
        file_path = await asyncio.get_event_loop().run_in_executor(
            None, download_video, url, tmpdir
        )

        size_mb = file_path.stat().st_size / (1024 * 1024)

        if size_mb > MAX_SIZE_MB:
            await msg.edit_text(f"❌ File too large: {size_mb:.1f} MB")
            return

        await msg.edit_text("📤 Uploading...")
        await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_VIDEO)

        with open(file_path, "rb") as f:
            await ctx.bot.send_video(
                chat_id=update.effective_chat.id,
                video=f,
                caption=title,
                supports_streaming=True,
            )

        await msg.edit_text("✅ Done")

    except Exception as e:
        await msg.edit_text(f"❌ Download failed:\n{e}")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# ── Error handler ─────────────────────────────────────────────
async def error_handler(update, ctx):
    if isinstance(ctx.error, Conflict):
        logger.error("Bot already running somewhere else")
        return
    logger.error(ctx.error)

# ── Main ──────────────────────────────────────────────────────
def main():
    threading.Thread(target=run_flask, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_error_handler(error_handler)

    app.run_polling()

if __name__ == "__main__":
    main()
