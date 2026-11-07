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

This builds from `Dockerfile` and exposes port 8787. The entrypoint (`docker-entrypoint.sh`) starts as root, tries to mount the NAS share at `/app/webui/downloads`, then drops to non-root `appuser` (via `setpriv`) to run the server.

There is no build step, package manifest, linter, or test suite in this repo — it's plain stdlib Python + vanilla JS/HTML/CSS with no dependencies to install.

### Downloads storage: SMB/CIFS NAS, mounted inside the container

The share is mounted **by `docker-entrypoint.sh` inside the container**, deliberately *not* as a Docker CIFS volume: a Docker-managed volume mount failure aborts container start, whereas this way an unreachable NAS just means local-only mode.

- `docker-compose.yml` grants `SYS_ADMIN` + `DAC_READ_SEARCH` and `apparmor:unconfined` (AppArmor's default Docker profile blocks `mount(2)`). `cifs-utils` is installed in the image; the host doesn't need it.
- Credentials (`NAS_HOST`, `NAS_SHARE`, `NAS_USER`, `NAS_PASS`) come from a local `.env` (gitignored) via `env_file`. The entrypoint writes them to a root-only `/run/nas-credentials` and strips `NAS_USER`/`NAS_PASS` from the server's environment. `NAS_HOST` must resolve *inside the container* — mDNS names like `*.local` won't, so use an IP (or `extra_hosts`).
- If the initial mount fails, a background root loop retries every `NAS_RETRY_SECONDS` (default 60).
- Mount options force `uid=1000,gid=1000` (matching `appuser`) and `soft` so I/O errors out rather than hanging if the NAS disappears.
- **NAS availability in the server:** when `NAS_HOST` is set, `nas_monitor` re-checks every 15s that `DOWNLOAD_DIR` is a CIFS mount (`/proc/mounts`) and that port 445 answers, and publishes `NAS_STATUS` via `GET /api/nas-status`. `/api/download` rejects `save_to_nas` jobs with 503 while unavailable; the UI shows a banner and forces "Download to my device only". When `NAS_HOST` is unset (local, non-Docker runs), `DOWNLOAD_DIR` is treated as plain local storage and always available.
- NAS subdirs (`video/`, `mp3/`, `.in-progress/`) are created by `ensure_download_dirs()` only once the NAS checks pass — never at import time — so nothing is written to the container filesystem underneath the mount point.

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
