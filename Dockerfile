# ─────────────────────────────────────────────────────────────────────────────
# Stage 1: build dependencies in a slim image
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build tools needed for some Python wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip \
 && pip install --prefix=/install --no-cache-dir -r requirements.txt


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2: runtime image
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim

# Install ffmpeg (required by yt-dlp for merging audio/video and audio extraction)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder
COPY --from=builder /install /usr/local

WORKDIR /app

# Copy application source
COPY bot.py .

# cookies.txt is mounted at runtime — provide an empty placeholder so the
# container doesn't crash on a missing bind-mount path.
RUN touch /app/cookies.txt

# Tmp directory for downloads (can also be overridden via DOWNLOAD_DIR env var)
RUN mkdir -p /tmp/yt_downloads

# Non-root user for security
RUN useradd -m -u 1001 botuser \
 && chown -R botuser:botuser /app /tmp/yt_downloads
USER botuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

CMD ["python", "-u", "bot.py"]
