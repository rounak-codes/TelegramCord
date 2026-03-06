# ── Dockerfile ────────────────────────────────────────────────────────────────
# Build:  docker build -t tg-discord-bot .
# Run:    docker run --env-file .env -v $(pwd)/data:/app/data -v $(pwd)/logs:/app/logs tg-discord-bot
#
# Volumes explained:
#   /app/data  — persists the dedup cache (seen_messages.json) across restarts
#   /app/logs  — persists log files across restarts
#
# Raspberry Pi (ARM) build:
#   docker buildx build --platform linux/arm64 -t tg-discord-bot .

FROM python:3.13-slim

# System dependencies needed by Pillow
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libc6-dev \
    libjpeg-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user for security
RUN useradd -m botuser
WORKDIR /app

# Install Python dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY . .

# Create runtime directories and set ownership
RUN mkdir -p /app/data /app/logs /app/sessions \
    && chown -R botuser:botuser /app

# Switch to non-root user
USER botuser

# Health-check: verify the process is alive
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD pgrep -f "python main.py" || exit 1

CMD ["python", "main.py"]