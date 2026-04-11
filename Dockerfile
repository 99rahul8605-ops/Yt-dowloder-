# ─────────────────────────────────────────────────────────────────────────────
# Stage 1: build Python dependencies
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends gcc libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip \
 && pip install --prefix=/install --no-cache-dir -r requirements.txt


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2: runtime
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

WORKDIR /app

COPY bot.py        .
COPY entrypoint.sh .
RUN chmod +x /app/entrypoint.sh

# Bake cookies into the image at build time.
# The docker-compose volume mount overrides this at runtime with a fresher file.
# This ensures auth works even if the bind-mount is missing or empty.
COPY cookies.txt /app/cookies.txt

RUN mkdir -p /tmp/yt_downloads

RUN useradd -m -u 1001 botuser \
 && chown -R botuser:botuser /app /tmp/yt_downloads
USER botuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    COOKIES_FILE=/app/cookies.txt \
    DOWNLOAD_DIR=/tmp/yt_downloads \
    MAX_SIZE_MB=50

ENTRYPOINT ["/app/entrypoint.sh"]
