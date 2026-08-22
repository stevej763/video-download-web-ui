# video-download

A simple web frontend for [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) — paste a URL, pick video or audio, and download it, without typing out `yt-dlp` flags on the CLI every time. Downloads are stored on a NAS share over SMB/CIFS rather than local disk.

## Features

- Paste a URL, preview the title/thumbnail, and download as MP4 (video) or MP3 (audio only)
- Cap video download quality (e.g. 1080p, 720p) via a dropdown populated from the video's actual available formats
- Optionally embed thumbnail, metadata (title/artist/etc.), and chapters into the downloaded file
- Live progress bar and log while a download runs
- Skips re-downloading a video you've already grabbed before
- Runs in Docker, storing files directly on a NAS share instead of the container's local disk

## Running it

```bash
cp .env.example .env   # fill in your NAS host/share/credentials
docker compose up -d --build
```

Then open `http://<host>:8787`.

### NAS storage

Downloads are written to a Docker volume backed by a CIFS/SMB mount (see `docker-compose.yml`), not the container's local filesystem. Configure the share in `.env`:

```
NAS_HOST=apollo.local
NAS_SHARE=yt
NAS_USER=ytdl
NAS_PASS=...
```

The CIFS mount happens on whatever machine runs the Docker daemon (the host itself, or the VM it's running in) — that machine needs `cifs-utils` installed and network access to the NAS.

### Running locally without Docker

```bash
python3 webui/server.py
```

Stdlib Python only — no dependencies to install. Requires `ffmpeg` on `PATH` for merging/audio extraction, and downloads go to `webui/downloads/` on local disk.

## Requirements

- Docker + Docker Compose (for the normal setup), or Python 3 + `ffmpeg` for running the server directly
- `cifs-utils` on the Docker host, for the NAS-backed volume
