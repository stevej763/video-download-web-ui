#!/usr/bin/env python3
"""Simple local web UI for yt-dlp. Stdlib only, no external dependencies."""
import json
import os
import re
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(BASE_DIR)
YT_DLP_BIN = os.path.join(REPO_DIR, 'yt-dlp')
DOWNLOAD_DIR = os.path.join(BASE_DIR, 'downloads')
ARCHIVE_FILE = os.path.join(DOWNLOAD_DIR, '.download-archive.txt')
STATIC_FILES = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/app.js': ('app.js', 'application/javascript; charset=utf-8'),
    '/style.css': ('style.css', 'text/css; charset=utf-8'),
}

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# job_id -> {status, lines, error, filepath, progress}
JOBS = {}
JOBS_LOCK = threading.Lock()

PROGRESS_RE = re.compile(r'(\d{1,3}\.\d)%')

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


def run_job(job_id, url, mode, quality=None, embed=False):
    args = [
        YT_DLP_BIN,
        '--newline',
        '--progress',
        '--no-playlist',
        '--download-archive', ARCHIVE_FILE,
        '-o', os.path.join(DOWNLOAD_DIR, '%(title).150B [%(id)s].%(ext)s'),
        '--print', 'after_move:filepath',
    ] + build_mode_args(mode, quality) + (EMBED_ARGS if embed else []) + [url]

    with JOBS_LOCK:
        JOBS[job_id]['status'] = 'running'

    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=DOWNLOAD_DIR,
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
                # after_move:filepath prints a bare absolute path line
                if line.startswith(DOWNLOAD_DIR):
                    filepath = line
        proc.wait()
        with JOBS_LOCK:
            job = JOBS[job_id]
            if proc.returncode == 0:
                job['status'] = 'finished'
                job['progress'] = 100
                job['filepath'] = filepath
            else:
                job['status'] = 'error'
                job['error'] = f'yt-dlp exited with code {proc.returncode}'
    except FileNotFoundError:
        with JOBS_LOCK:
            JOBS[job_id]['status'] = 'error'
            JOBS[job_id]['error'] = f'yt-dlp binary not found at {YT_DLP_BIN}'
    except Exception as exc:  # noqa: BLE001
        with JOBS_LOCK:
            JOBS[job_id]['status'] = 'error'
            JOBS[job_id]['error'] = str(exc)


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
                })
            return

        if path.startswith('/downloads/'):
            filename = os.path.basename(unquote(parsed.path[len('/downloads/'):]))
            if not filename or filename.startswith('.'):
                self.send_response(404)
                self.end_headers()
                return
            fpath = os.path.join(DOWNLOAD_DIR, filename)
            if not os.path.isfile(fpath):
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
            self.send_header('Content-Length', str(os.path.getsize(fpath)))
            self.end_headers()
            with open(fpath, 'rb') as f:
                self.wfile.write(f.read())
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
            job_id = uuid.uuid4().hex
            with JOBS_LOCK:
                JOBS[job_id] = {'status': 'queued', 'lines': [], 'progress': 0}
            threading.Thread(
                target=run_job, args=(job_id, url, mode, quality, embed), daemon=True,
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
