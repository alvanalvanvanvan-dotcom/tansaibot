# ============================================================
# tansaibot — Dockerfile (#19)
# ============================================================
# Multi-stage build: keeps final image lean (~150MB).
#
# Build:
#   docker build -t tansaibot .
#
# Run (standalone):
#   docker run -d --env-file .env --name tansaibot tansaibot
# ============================================================

# ---------- Build stage ----------
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Install into a prefix dir so we can copy cleanly
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---------- Runtime stage ----------
FROM python:3.11-slim

LABEL maintainer="tansaibot"
LABEL description="Telegram bot frontend for Tans AI API Gateway"

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy source
COPY . .

# Non-root user for security
RUN adduser --disabled-password --gecos "" botuser \
    && chown -R botuser:botuser /app
USER botuser

# Data directory (SQLite will be stored here when DB_PATH points inside)
VOLUME ["/app/data"]

# Health check via Python import (fast, no extra deps)
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import bot" 2>/dev/null || exit 1

CMD ["python", "-u", "bot.py"]
