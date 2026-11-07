FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        curl \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp needs a JS runtime to deobfuscate YouTube's player signatures.
ENV DENO_INSTALL=/usr/local
RUN curl -fsSL https://deno.land/install.sh | sh

RUN useradd --create-home --uid 1000 appuser
WORKDIR /app

COPY yt-dlp /app/yt-dlp
COPY webui /app/webui

RUN chmod +x /app/yt-dlp \
    && mkdir -p /app/webui/downloads \
    && chown -R appuser:appuser /app

USER appuser

ENV YTDLP_WEBUI_HOST=0.0.0.0
ENV YTDLP_WEBUI_PORT=8787

EXPOSE 8787

CMD ["python3", "/app/webui/server.py"]
