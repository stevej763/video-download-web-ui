# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A minimal self-hosted web UI for `yt-dlp`. It wraps the `yt-dlp` binary in a stdlib-only Python HTTP server so a user can paste a video URL, preview it, and download it as MP4 (video) or MP3 (audio) from a browser.

## Running it

Local (no Docker):

```bash
python3 webui/server.py
```

Env vars (both optional, read in `webui/server.py`):
- `YTDLP_WEBUI_HOST` (default `127.0.0.1`)
- `YTDLP_WEBUI_PORT` (default `8787`)

Docker:

```bash
docker compose up --build
```

This builds from `Dockerfile`, exposes port 8787, and mounts a named Docker volume at `/app/webui/downloads`: by default the NAS-backed `nas-downloads` (so downloaded files land on a network share), or a plain local `local-downloads` volume if the NAS is disabled (see below). The container runs as non-root `appuser`.

There is no build step, package manifest, linter, or test suite in this repo — it's plain stdlib Python + vanilla JS/HTML/CSS with no dependencies to install.

### Downloads storage: SMB/CIFS NAS volume

**Disabling the NAS:** `docker-compose.yml` on its own uses the local `local-downloads` volume; the NAS volume lives in `docker-compose.override.yml`, which compose loads automatically. Setting `COMPOSE_FILE=docker-compose.yml` in `.env` skips the override, so the container runs without any CIFS mount (and the `NAS_*` vars are ignored). Compose can't toggle a volume from a boolean env var, which is why the switch is `COMPOSE_FILE` rather than e.g. `NAS_ENABLED=false`.

`docker-compose.override.yml` defines `nas-downloads` as a `local`-driver volume with `driver_opts: {type: cifs, ...}`, pointing at `//${NAS_HOST}/${NAS_SHARE}`. This means **the CIFS mount happens on the Docker host itself** (via `mount.cifs`), not inside the container — the container just sees a normal directory at `/app/webui/downloads`.

- Requires `cifs-utils` installed on the Docker host (provides `mount.cifs`). Without it, `docker compose up` fails to create the volume.
- Credentials (`NAS_HOST`, `NAS_SHARE`, `NAS_USER`, `NAS_PASS`) are read from a local `.env` file (gitignored, never committed) via compose's `${VAR}` substitution. `.env.example` documents the required keys.
- The mount options force `uid=1000,gid=1000` (matching the `appuser` created in `Dockerfile`) so files written by the container are owned correctly from the NAS's perspective, regardless of what UID the SMB server reports.
- If `docker compose up` errors that it can't create/mount the volume, check: `cifs-utils` is installed on the host, `.env` has the real password (not the `changeme` placeholder), and the NAS hostname resolves (mDNS hostnames like `*.local` need Avahi/mDNS support on the Docker host).

## Architecture

Three pieces, all under `webui/`:

- **`server.py`** — a `ThreadingHTTPServer` using only `http.server` (no Flask/etc). It serves the static frontend, and exposes:
  - `POST /api/probe` — runs `yt-dlp -J --no-playlist` to fetch metadata (title, uploader, duration, thumbnail) for URL preview, without downloading.
  - `POST /api/download` — validates `mode` (`video` or `audio`), creates a job in the in-memory `JOBS` dict keyed by a UUID, and spawns a background thread (`run_job`) that shells out to the `yt-dlp` binary.
  - `GET /api/status?job_id=...` — polled by the frontend; returns job status/progress/tail of output lines.
  - `GET /downloads/<filename>` — serves a completed file for download from `webui/downloads/`.
  - Job state (`JOBS`) is a process-local dict guarded by `JOBS_LOCK` — it is **not persisted**, so it resets on server restart and does not scale across multiple processes/replicas.
  - Download mode → yt-dlp CLI args mapping lives in `MODE_ARGS`. Progress is parsed by scraping stdout with `PROGRESS_RE` (matches yt-dlp's `NN.N%` output) — there's no structured progress API, so this regex is the only way progress is known.
  - **`--progress` is required alongside `--print`.** yt-dlp implicitly sets `quiet=True` (and thus `noprogress=True`) whenever `--print`/`--forceprint` is used, since `--print` is normally meant for clean, undecorated output — this silently suppresses all `[download] NN.N%` lines despite `--newline` being present, so progress looked "stuck at 0%" for the whole job (nothing printed until the process exited). `--progress` explicitly overrides `noprogress` back to `False`. If progress reporting ever breaks again after touching this arg list, check this interaction first.
  - The final output path is captured via `--print after_move:filepath`; `run_job` looks for a stdout line that starts with `DOWNLOAD_DIR` to identify it.
- **`app.js`** — vanilla JS frontend: debounced URL probing (`/api/probe`) as the user types, form submit kicks off `/api/download`, then polls `/api/status` every second until `finished`/`error`, updating a progress bar and log `<pre>`.
- **`index.html` / `style.css`** — single-page form UI, no framework/build tooling.

## The `yt-dlp` binary

`yt-dlp` at the repo root is the actual `yt-dlp` self-contained executable (a zipapp, not a script Claude should edit), invoked via `subprocess` from `server.py` (`YT_DLP_BIN = <repo>/yt-dlp`). Do not attempt to modify it — treat it as a vendored binary dependency. If it needs updating, that means replacing the file with a newer release, not editing it.

## Security notes specific to this app

- `/downloads/<filename>` and the download output template both use `os.path.basename`/filename templating to avoid path traversal — preserve that pattern if touching those code paths.
- URLs passed to `yt-dlp` go straight from the request body to `subprocess.Popen`/`subprocess.run` as an argv list (not through a shell), which avoids shell injection — keep using argv lists, not `shell=True`, if modifying invocation code.
