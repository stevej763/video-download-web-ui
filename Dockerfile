FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        cifs-utils \
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
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN chmod +x /app/yt-dlp /usr/local/bin/docker-entrypoint.sh \
    && mkdir -p /app/webui/downloads \
    && chown -R appuser:appuser /app

# No USER appuser here: the entrypoint needs root to mount the NAS share, then
# drops to appuser (via setpriv) before starting the server.
ENV YTDLP_WEBUI_HOST=0.0.0.0
ENV YTDLP_WEBUI_PORT=8787

EXPOSE 8787

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
