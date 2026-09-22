# syntax=docker/dockerfile:1.7
# Multi-stage image for Cove: runtime deps + ffmpeg/ffprobe + yt-dlp + aria2c.
# Optional NVENC requires a host with NVIDIA Container Toolkit; CPU encode works without it.

FROM python:3.12-slim-bookworm AS deps

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        aria2 \
        ca-certificates \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && yt-dlp --version

FROM deps AS test

COPY requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY bot.py cove_attribution.py conftest.py ./
COPY tests ./tests

# bot.py exits at import without these; tests never talk to Discord.
ENV DISCORD_TOKEN=test-token-not-real \
    GUILD_ID=1 \
    FRIEND_GUILD_ID=0 \
    PERSISTENT_CACHE=0

CMD ["pytest", "-q"]

FROM deps AS runtime

ARG APP_VERSION=dev
ARG GIT_SHA=unknown

LABEL org.opencontainers.image.title="cove-video-downloader-bot" \
      org.opencontainers.image.description="Discord bot that downloads and re-uploads chat media via yt-dlp/ffmpeg" \
      org.opencontainers.image.source="https://github.com/Sin213/cove-video-downloader-bot" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}"

ENV APP_VERSION=${APP_VERSION} \
    GIT_SHA=${GIT_SHA} \
    COVE_DATA_DIR=/data \
    # Prefer a writable tmp volume over the default tiny Docker /dev/shm.
    TMPDIR=/tmp/cove \
    # Safe CPU defaults; override in compose for NVENC hosts.
    USE_NVENC=0 \
    USE_HWACCEL=0 \
    USE_ARIA2C=1 \
    PERSISTENT_CACHE=1

RUN groupadd --gid 1000 cove \
    && useradd --uid 1000 --gid cove --create-home --home-dir /home/cove --shell /usr/sbin/nologin cove \
    && mkdir -p /data /tmp/cove \
    && chown -R cove:cove /data /tmp/cove /app

WORKDIR /app

COPY --chown=cove:cove bot.py cove_attribution.py ./

USER cove

VOLUME ["/data"]

# Discord gateway reachability; the bot has no HTTP listener of its own.
HEALTHCHECK --interval=60s --timeout=10s --start-period=45s --retries=3 \
    CMD python -c "import socket; s=socket.create_connection(('discord.com', 443), 5); s.close()" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-u", "bot.py"]
