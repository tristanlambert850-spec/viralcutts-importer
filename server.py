"""Small, bounded YouTube import service for a single-instance demo."""
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

MAX_BYTES = 150 * 1024 * 1024
ROOT = Path(tempfile.mkdtemp(prefix='viralcutts-'))
JOBS = {}
LOCK = threading.Lock()
WORKER = threading.Semaphore(1)
ACCESS_CODE = os.environ.get('DEMO_ACCESS_CODE', '')
ORIGIN = os.environ.get('ALLOWED_ORIGIN', 'https://tax1234-viralcutts.static.hf.space').rstrip('/')


def canonical_youtube_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError('Paste a valid YouTube video link.')
    p = urlsplit(value.strip())
    if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443):
        raise ValueError('Use an https:// YouTube video link.')
    host = (p.hostname or '').lower()
    if host == 'youtu.be':
        video_id = p.path.strip('/')
    elif host in ('youtube.com', 'www.youtube.com', 'm.youtube.com'):
        if p.path == '/watch':
            video_id = parse_qs(p.query).get('v', [''])[0]
        elif p.path.startswith(('/shorts/', '/embed/')):
            video_id = p.path.split('/')[2]
        else:
            video_id = ''
    else:
        video_id = ''
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
        raise ValueError('Paste a single YouTube watch, Shorts, or youtu.be link.')
    # Never hand arbitrary URLs or supplied query parameters to the downloader.
    return 'https://www.youtube.com/watch?v=' + video_id


def cleanup():
    with LOCK:
        expired = [key for key, job in JOBS.items() if job['expires'] < time.time() and job['state'] != 'loading']
        for key in expired:
            JOBS.pop(key, None)
            target = ROOT / key
            if target.parent == ROOT and target.is_dir():
                shutil.rmtree(target)


def execute(args, timeout):
    result = subprocess.run([sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-playlist', '--js-runtimes', 'node', '--socket-timeout', '15', '--retries', '1', *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise ValueError('YouTube did not allow this import. The video may be restricted, or YouTube may be blocking this server. Try another public video or upload the file.')
    return result.stdout


def import_video(job_id, url):
    folder = ROOT / job_id
    folder.mkdir()
    try:
        metadata = json.loads(execute(['--skip-download', '--dump-single-json', url], 45))
        duration = metadata.get('duration')
        if metadata.get('is_live') or not isinstance(duration, (int, float)) or not 0 < duration <= 900:
            raise ValueError('The free demo accepts recorded videos up to 15 minutes long.')
        execute(['--no-progress', '--max-filesize', str(MAX_BYTES), '--format', 'best[ext=mp4][height<=480][vcodec^=avc1][acodec!=none]/bestvideo[ext=mp4][height<=480][vcodec^=avc1]+bestaudio[ext=m4a]', '--merge-output-format', 'mp4', '--output', str(folder / 'source.%(ext)s'), url], 240)
        media = folder / 'source.mp4'
        if not media.is_file() or not 0 < media.stat().st_size <= MAX_BYTES:
            raise ValueError('This video exceeds the 150 MB demo limit or has no compatible MP4 format. Upload the file instead.')
        with LOCK:
            JOBS[job_id].update(state='ready', title=str(metadata.get('title') or 'YouTube video')[:160], size=media.stat().st_size)
    except subprocess.TimeoutExpired:
        with LOCK:
            JOBS[job_id].update(state='error', message='Import timed out. Try a shorter video or upload the file.')
    except Exception as exc:
        message = str(exc) if isinstance(exc, ValueError) else 'Import failed. Try another video or upload the file.'
        with LOCK:
            JOBS[job_id].update(state='error', message=message)
    finally:
        with LOCK:
            JOBS[job_id]['expires'] = time.time() + 600
        if JOBS[job_id]['state'] == 'error':
            shutil.rmtree(folder, ignore_errors=True)
        WORKER.release()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Do not log private job URLs or access codes.

    def common(self, status, content_type='application/json'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Access-Control-Allow-Origin', ORIGIN)
        self.send_header('Vary', 'Origin')
        self.send_header('X-Content-Type-Options', 'nosniff')

    def reply(self, status, obj):
        payload = json.dumps(obj).encode()
        self.common(status)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def authorized(self):
        if not ACCESS_CODE:
            self.reply(503, {'message': 'The owner must configure DEMO_ACCESS_CODE before importing.'})
            return False
        supplied = self.headers.get('Authorization', '').removeprefix('Bearer ')
        if not hmac.compare_digest(supplied.encode(), ACCESS_CODE.encode()):
            self.reply(401, {'message': 'Enter the demo access code provided by the owner.'})
            return False
        origin = self.headers.get('Origin')
        if origin and origin != ORIGIN:
            self.reply(403, {'message': 'This origin is not allowed.'})
            return False
        return True

    def do_OPTIONS(self):
        self.common(204)
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type')
        self.end_headers()

    def do_POST(self):
        if self.path != '/imports':
            return self.reply(404, {'message': 'Not found.'})
        if not self.authorized():
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length < 4096:
                return self.reply(413, {'message': 'Request is too large or empty.'})
            url = canonical_youtube_url(json.loads(self.rfile.read(length)).get('url'))
        except (ValueError, AttributeError, TypeError):
            return self.reply(400, {'message': 'Paste a valid HTTPS YouTube video link.'})
        cleanup()
        with LOCK:
            if len(JOBS) >= 3:
                return self.reply(429, {'message': 'Demo storage is full. Wait 10 minutes before another import.'})
        if not WORKER.acquire(blocking=False):
            return self.reply(429, {'message': 'Another import is running. Try again shortly.'})
        job_id = secrets.token_urlsafe(24)
        with LOCK:
            JOBS[job_id] = {'state': 'loading', 'expires': time.time() + 600}
        threading.Thread(target=import_video, args=(job_id, url), daemon=True).start()
        self.reply(202, {'id': job_id, 'state': 'loading'})

    def do_GET(self):
        if self.path == '/health':
            return self.reply(200, {'status': 'ok', 'configured': bool(ACCESS_CODE)})
        if not self.authorized():
            return
        cleanup()
        match = re.fullmatch(r'/imports/([A-Za-z0-9_-]{32})(/file)?', self.path)
        if not match:
            return self.reply(404, {'message': 'Not found.'})
        job_id, file_request = match.groups()
        with LOCK:
            job = dict(JOBS.get(job_id, {}))
        if not job:
            return self.reply(404, {'message': 'Import expired or server restarted. Import the link again.'})
        if not file_request:
            return self.reply(200, {key: value for key, value in job.items() if key != 'expires'})
        if job['state'] != 'ready':
            return self.reply(409, {'message': 'Video is not ready.'})
        try:
            with (ROOT / job_id / 'source.mp4').open('rb') as media:
                self.common(200, 'video/mp4')
                self.send_header('Content-Length', str(job['size']))
                self.end_headers()
                shutil.copyfileobj(media, self.wfile, 64 * 1024)
        except (FileNotFoundError, BrokenPipeError, ConnectionResetError):
            return


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '10000'))), Handler).serve_forever()
