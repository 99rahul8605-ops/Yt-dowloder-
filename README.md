# 🎬 YouTube Telegram Downloader Bot

A self-hosted Telegram bot that downloads YouTube videos (or audio) and sends them directly to users — powered by **yt-dlp**, **ffmpeg**, and **python-telegram-bot**, packaged in Docker.

---

## Features

| Feature | Details |
|---|---|
| Quality picker | Best (≤1080p) · 720p · 480p · Audio-only MP3 |
| Cookie auth | Netscape `cookies.txt` for age-restricted / members-only content |
| Size guard | Rejects files > 50 MB (Telegram Bot API hard limit) |
| User allowlist | Restrict access to specific Telegram user IDs |
| Docker ready | Multi-stage Dockerfile + Compose with resource limits |
| Streaming upload | Videos sent with `supports_streaming=True` |

---

## Quick Start

### 1. Clone & configure

```bash
git clone <your-repo>
cd yt-telegram-bot

cp .env.example .env
# Edit .env and set BOT_TOKEN
```

### 2. Get a Bot Token

1. Open Telegram and message **@BotFather**.
2. Send `/newbot` and follow the prompts.
3. Copy the token into `.env`:

```
BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrSTUvwxYZ
```

### 3. Set up cookies (recommended)

Cookies let the bot bypass age-restriction checks and avoid YouTube's bot throttling.

#### Option A — export from your browser (easiest)

Make sure you are **logged in to YouTube** in Chrome/Firefox, then run:

```bash
# Requires yt-dlp installed locally: pip install yt-dlp
bash export_cookies.sh chrome      # or firefox, edge, brave, …
```

This writes a `cookies.txt` in the project root.

#### Option B — browser extension

1. Install [**Get cookies.txt LOCALLY**](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) (Chrome) or the equivalent for Firefox.
2. Visit `https://www.youtube.com` while logged in.
3. Click the extension → **Export** → save as `cookies.txt` in the project root.

#### Option C — no cookies

Delete or leave `cookies.txt` empty. The bot still works for **public, non-age-restricted** videos.

### 4. Run with Docker Compose

```bash
docker compose up -d --build
```

Check logs:
```bash
docker compose logs -f
```

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `BOT_TOKEN` | ✅ | — | Telegram Bot API token |
| `COOKIES_FILE` | ❌ | `/app/cookies.txt` | Path inside container |
| `MAX_SIZE_MB` | ❌ | `50` | Reject files above this size |
| `ALLOWED_USERS` | ❌ | *(everyone)* | Comma-separated Telegram user IDs |
| `DOWNLOAD_DIR` | ❌ | `/tmp/yt_downloads` | Temp dir inside container |

---

## Usage

1. Send the bot any YouTube URL (regular video, Shorts, or playlist item).
2. The bot shows video info and a quality menu.
3. Tap a quality button.
4. The bot downloads, then uploads the file to your chat.

### Commands

| Command | Description |
|---|---|
| `/start` | Welcome message |
| `/help` | Usage tips |
| `/cookies` | Check whether cookies.txt is loaded |

---

## Cookie File Format

The file must be in **Netscape HTTP Cookie File** format:

```
# Netscape HTTP Cookie File
.youtube.com	TRUE	/	TRUE	1700000000	CONSENT	YES+...
.youtube.com	TRUE	/	TRUE	1700000000	SID	...
```

> **Tip:** cookies expire. If the bot starts returning 403 errors on restricted content, re-export your cookies.

---

## Architecture

```
User sends URL
      │
      ▼
 handle_url()        ← validate URL, fetch metadata via yt-dlp
      │
      ▼
 Quality keyboard    ← InlineKeyboardMarkup with 4 options
      │  (user taps)
      ▼
 handle_quality_choice()
      ├─ download_video()  ← yt-dlp + ffmpeg in thread executor
      ├─ size check
      └─ send_video() / send_audio()
```

---

## Updating yt-dlp

YouTube frequently changes its internals. Keep yt-dlp up to date:

```bash
# Rebuild the image with the latest yt-dlp
docker compose build --no-cache
docker compose up -d
```

Or update inside a running container (temporary, lost on restart):
```bash
docker compose exec yt-bot pip install -U yt-dlp
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `Sign in to confirm you're not a bot` | Export and mount fresh `cookies.txt` |
| File too large | Choose a lower quality |
| `DownloadError: HTTP 403` | Cookies expired — re-export |
| Bot doesn't respond | Check `BOT_TOKEN` and `docker compose logs` |
| ffmpeg not found | Rebuild the Docker image |

---

## Legal

This bot is for **personal use only**. Downloading copyrighted content may violate YouTube's Terms of Service and applicable law. Use responsibly.
