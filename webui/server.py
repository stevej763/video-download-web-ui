#!/usr/bin/env python3
"""Simple local web UI for yt-dlp. Stdlib only, no external dependencies."""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote, unquote

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(BASE_DIR)
YT_DLP_BIN = os.path.join(REPO_DIR, 'yt-dlp')
DOWNLOAD_DIR = os.path.join(BASE_DIR, 'downloads')
ARCHIVE_FILE = os.path.join(DOWNLOAD_DIR, '.download-archive.txt')
# NAS-backed but hidden: yt-dlp writes/merges/embeds here first, one subdir per
# job, so nobody browsing the NAS share sees .part files or half-written output.
INPROGRESS_DIR = os.path.join(DOWNLOAD_DIR, '.in-progress')
# Not NAS-backed at all: local scratch space for "download to my device" jobs,
# one subdir per job, served once then deleted.
TEMP_DIR = os.path.join(BASE_DIR, 'temp-downloads')
STALE_TEMP_SECONDS = 6 * 3600
MODE_SUBFOLDER = {'video': 'video', 'audio': 'mp3'}
STATIC_FILES = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/app.js': ('app.js', 'application/javascript; charset=utf-8'),
    '/style.css': ('style.css', 'text/css; charset=utf-8'),
}

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(INPROGRESS_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)
for _sub in MODE_SUBFOLDER.values():
    os.makedirs(os.path.join(DOWNLOAD_DIR, _sub), exist_ok=True)


def cleanup_stale_dirs(base_dir):
    """Reap orphaned per-job scratch dirs (e.g. a crash, or the browser never fetched the file)."""
    now = time.time()
    try:
        names = os.listdir(base_dir)
    except OSError:
        return
    for name in names:
        path = os.path.join(base_dir, name)
        try:
            if now - os.path.getmtime(path) > STALE_TEMP_SECONDS:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass

# job_id -> {status, lines, error, filepath, progress}
JOBS = {}
JOBS_LOCK = threading.Lock()

PROGRESS_RE = re.compile(r'(\d{1,3}\.\d)%')
STAGE_RE = re.compile(r'^\[(\w+)\]')
# yt-dlp postprocessor tag -> human label. Tags are each PP class's name with
# the "FFmpeg" prefix and "PP" suffix stripped (see PostProcessor.pp_key()).
STAGE_LABELS = {
    'Merger': 'Merging video and audio',
    'ExtractAudio': 'Extracting audio',
    'VideoConvertor': 'Converting video',
    'EmbedSubtitle': 'Embedding subtitles',
    'Metadata': 'Embedding metadata',
    'EmbedThumbnail': 'Embedding thumbnail',
    'SponsorBlock': 'Applying SponsorBlock',
    'ThumbnailsConvertor': 'Converting thumbnail',
    'SubtitlesConvertor': 'Converting subtitles',
    'SplitChapters': 'Splitting chapters',
    'FixupM3u8': 'Fixing up stream',
    'FixupM4a': 'Fixing up audio container',
    'FixupStretched': 'Fixing up video',
    'FixupTimestamp': 'Fixing up timestamps',
    'CopyStream': 'Finalizing stream',
    'Concat': 'Concatenating files',
    'MoveFilesAfterDownload': 'Moving file into place',
}

VALID_MODES = ('video', 'audio')

EMBED_ARGS = ['--embed-metadata', '--embed-thumbnail', '--embed-chapters']


def build_mode_args(mode, quality):
    if mode == 'audio':
        return ['-x', '--audio-format', 'mp3']
    # video
    if quality:
        fmt = f'bv*[height<={quality}]+ba/b[height<={quality}]'
    else:
        fmt = 'bv*+ba/b'
    return ['-f', fmt, '--merge-output-format', 'mp4']


def run_job(job_id, url, mode, quality=None, embed=False, save_to_nas=True):
    # Both branches stage into a private per-job scratch dir first, so
    # nothing partially-written is ever visible at its final, browsable path.
    if save_to_nas:
        work_dir = os.path.join(INPROGRESS_DIR, job_id)
        archive_args = ['--download-archive', ARCHIVE_FILE]
    else:
        work_dir = os.path.join(TEMP_DIR, job_id)
        archive_args = []
    os.makedirs(work_dir, exist_ok=True)

    args = [
        YT_DLP_BIN,
        '--newline',
        '--progress',
        '--no-quiet',
        '--no-playlist',
    ] + archive_args + [
        '-o', os.path.join(work_dir, '%(title).150B [%(id)s].%(ext)s'),
        '--print', 'after_move:filepath',
    ] + build_mode_args(mode, quality) + (EMBED_ARGS if embed else []) + [url]

    with JOBS_LOCK:
        JOBS[job_id]['status'] = 'running'

    def fail(message):
        with JOBS_LOCK:
            JOBS[job_id]['status'] = 'error'
            JOBS[job_id]['error'] = message
        shutil.rmtree(work_dir, ignore_errors=True)

    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=work_dir,
        )
        filepath = None
        for line in proc.stdout:
            line = line.rstrip('\n')
            with JOBS_LOCK:
                job = JOBS[job_id]
                job['lines'].append(line)
                job['lines'] = job['lines'][-300:]
                m = PROGRESS_RE.search(line)
                if m:
                    job['progress'] = float(m.group(1))
                    job['stage'] = 'downloading'
                else:
                    # Only flip to a known postprocessor tag; ignore everything
                    # else (e.g. [youtube]/[info] extraction chatter unlocked
                    # by --no-quiet) so we don't show "Post-processing" before
                    # the download has even started.
                    sm = STAGE_RE.match(line)
                    label = sm and STAGE_LABELS.get(sm.group(1))
                    if label:
                        job['stage'] = label
                # after_move:filepath prints a bare absolute path line
                if line.startswith(work_dir):
                    filepath = line
        proc.wait()
        if proc.returncode != 0:
            fail(f'yt-dlp exited with code {proc.returncode}')
            return

        final_path = filepath
        if save_to_nas:
            if filepath:
                with JOBS_LOCK:
                    JOBS[job_id]['stage'] = 'Finalizing'
                # Same CIFS mount on both sides, so this is a fast rename,
                # not a second copy over the network.
                dest_dir = os.path.join(DOWNLOAD_DIR, MODE_SUBFOLDER[mode])
                dest_path = os.path.join(dest_dir, os.path.basename(filepath))
                shutil.move(filepath, dest_path)
                final_path = dest_path
            shutil.rmtree(work_dir, ignore_errors=True)

        with JOBS_LOCK:
            job = JOBS[job_id]
            job['status'] = 'finished'
            job['progress'] = 100
            job['filepath'] = final_path
    except FileNotFoundError:
        fail(f'yt-dlp binary not found at {YT_DLP_BIN}')
    except Exception as exc:  # noqa: BLE001
        fail(str(exc))


def probe_url(url):
    args = [YT_DLP_BIN, '-J', '--no-playlist', '--no-warnings', url]
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[-500:] or 'Failed to fetch info')
    data = json.loads(result.stdout)
    heights = sorted({
        f['height'] for f in data.get('formats', [])
        if f.get('vcodec') not in (None, 'none') and f.get('height')
    }, reverse=True)
    return {
        'title': data.get('title'),
        'uploader': data.get('uploader'),
        'duration_string': data.get('duration_string'),
        'thumbnail': data.get('thumbnail'),
        'webpage_url': data.get('webpage_url', url),
        'heights': heights,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = 'ytdlp-webui/1.0'

    def log_message(self, fmt, *args):
        pass  # keep the terminal quiet

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get('Content-Length', 0))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw or b'{}')

    def _stream_file(self, fpath, filename):
        self.send_response(200)
        self.send_header('Content-Type', 'application/octet-stream')
        # http.server encodes headers as strict latin-1, so a raw non-ASCII
        # title (em dash, curly quote, emoji...) would raise mid-response.
        # Send an ASCII fallback plus the RFC 5987 UTF-8 form browsers prefer.
        ascii_name = filename.encode('ascii', 'replace').decode('ascii')
        ascii_name = ascii_name.replace('\\', '_').replace('"', "'")
        self.send_header(
            'Content-Disposition',
            f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename, safe="")}',
        )
        self.send_header('Content-Length', str(os.path.getsize(fpath)))
        self.end_headers()
        with open(fpath, 'rb') as f:
            shutil.copyfileobj(f, self.wfile, length=1024 * 1024)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in STATIC_FILES:
            filename, content_type = STATIC_FILES[path]
            fpath = os.path.join(BASE_DIR, filename)
            try:
                with open(fpath, 'rb') as f:
                    body = f.read()
            except FileNotFoundError:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == '/api/status':
            qs = parse_qs(parsed.query)
            job_id = (qs.get('job_id') or [None])[0]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                if not job:
                    self._send_json({'error': 'unknown job_id'}, 404)
                    return
                filename = os.path.basename(job['filepath']) if job.get('filepath') else None
                self._send_json({
                    'status': job['status'],
                    'progress': job.get('progress', 0),
                    'lines': job['lines'][-40:],
                    'error': job.get('error'),
                    'filename': filename,
                    'subfolder': job.get('subfolder'),
                    'save_to_nas': job.get('save_to_nas', True),
                    'stage': job.get('stage', 'downloading'),
                })
            return

        if path.startswith('/downloads/'):
            rest = unquote(parsed.path[len('/downloads/'):])
            subfolder, _, filename = rest.rpartition('/')
            subfolder = os.path.basename(subfolder)
            filename = os.path.basename(filename)
            if subfolder not in MODE_SUBFOLDER.values() or not filename or filename.startswith('.'):
                self.send_response(404)
                self.end_headers()
                return
            fpath = os.path.join(DOWNLOAD_DIR, subfolder, filename)
            if not os.path.isfile(fpath):
                self.send_response(404)
                self.end_headers()
                return
            self._stream_file(fpath, filename)
            return

        if path.startswith('/temp-downloads/'):
            rest = unquote(parsed.path[len('/temp-downloads/'):])
            job_id, _, filename = rest.partition('/')
            job_id = os.path.basename(job_id)
            filename = os.path.basename(filename)
            if not job_id or not filename or filename.startswith('.'):
                self.send_response(404)
                self.end_headers()
                return
            job_dir = os.path.join(TEMP_DIR, job_id)
            fpath = os.path.join(job_dir, filename)
            if not os.path.isfile(fpath):
                self.send_response(404)
                self.end_headers()
                return
            try:
                self._stream_file(fpath, filename)
            finally:
                shutil.rmtree(job_dir, ignore_errors=True)
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == '/api/probe':
            try:
                body = self._read_json_body()
                url = (body.get('url') or '').strip()
                if not url:
                    self._send_json({'error': 'url is required'}, 400)
                    return
                info = probe_url(url)
                self._send_json(info)
            except Exception as exc:  # noqa: BLE001
                self._send_json({'error': str(exc)}, 400)
            return

        if parsed.path == '/api/download':
            body = self._read_json_body()
            url = (body.get('url') or '').strip()
            mode = body.get('mode')
            if not url:
                self._send_json({'error': 'url is required'}, 400)
                return
            if mode not in VALID_MODES:
                self._send_json({'error': 'mode must be "video" or "audio"'}, 400)
                return
            quality = body.get('quality')
            quality = quality if (isinstance(quality, str) and quality.isdigit()) else None
            embed = bool(body.get('embed'))
            save_to_nas = body.get('save_to_nas')
            save_to_nas = True if save_to_nas is None else bool(save_to_nas)
            job_id = uuid.uuid4().hex
            cleanup_stale_dirs(TEMP_DIR)
            cleanup_stale_dirs(INPROGRESS_DIR)
            with JOBS_LOCK:
                JOBS[job_id] = {
                    'status': 'queued', 'lines': [], 'progress': 0, 'save_to_nas': save_to_nas,
                    'subfolder': MODE_SUBFOLDER[mode] if save_to_nas else None,
                }
            threading.Thread(
                target=run_job, args=(job_id, url, mode, quality, embed, save_to_nas), daemon=True,
            ).start()
            self._send_json({'job_id': job_id})
            return

        self.send_response(404)
        self.end_headers()


def main():
    host = os.environ.get('YTDLP_WEBUI_HOST', '127.0.0.1')
    port = int(os.environ.get('YTDLP_WEBUI_PORT', '8787'))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f'yt-dlp web UI running at http://{host}:{port}')
    print(f'Downloads are saved to: {DOWNLOAD_DIR}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
