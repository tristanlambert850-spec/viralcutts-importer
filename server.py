"""Small, bounded YouTube import service for a single-instance demo."""
import json
import os
import math
import signal
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
        elif p.path.startswith(('/shorts/', '/embed/', '/live/')):
            video_id = p.path.split('/')[2]
        else:
            video_id = ''
    else:
        video_id = ''
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
        raise ValueError('Paste a single YouTube watch, Shorts, or youtu.be link.')
    # Never hand arbitrary URLs or supplied query parameters to the downloader.
    return 'https://www.youtube.com/watch?v=' + video_id


def cleanup(make_room=False):
    """Bound disk usage without making completed attempts block new imports."""
    with LOCK:
        removable = sorted(
            ((key, job) for key, job in JOBS.items()
             if job['state'] != 'loading' and not job.get('readers')),
            key=lambda item: item[1]['expires'])
        for key, job in removable:
            if job['expires'] < time.time() or (make_room and len(JOBS) >= 3):
                target = ROOT / key
                if target.parent == ROOT and target.is_dir():
                    shutil.rmtree(target)
                JOBS.pop(key, None)



def parse_options(data):
    mode = data.get('mode', 'video')
    start, seconds = data.get('start', 0), data.get('seconds', 30)
    if mode not in ('video', 'live', 'replay'):
        raise ValueError('Choose video, live capture, or replay section.')
    if any(isinstance(n, bool) or not isinstance(n, (float, int)) or not math.isfinite(n) for n in (start, seconds)):
        raise ValueError('Enter valid numeric times.')
    if not 0 <= start <= 604800 or not 15 <= seconds <= 120:
        raise ValueError('Choose a start within seven days and a clip length from 15 to 120 seconds.')
    return mode, start, seconds


class Cancelled(Exception):
    pass


def execute(args, timeout, job_id=None):
    command = [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-playlist', '--js-runtimes', 'node', '--socket-timeout', '15', '--retries', '1', *args]
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=os.name != 'nt')
    deadline = time.monotonic() + timeout
    try:
        while True:
            if job_id and JOBS[job_id].get('cancel'):
                raise Cancelled()
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(command, timeout)
            if job_id and sum(p.stat().st_size for p in (ROOT / job_id).glob('*') if p.is_file()) > MAX_BYTES * 2:
                raise ValueError('Import exceeds the storage limit. Choose a shorter section.')
            try:
                stdout, stderr = proc.communicate(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                continue
        if proc.returncode:
            reason = stderr.lower()
            # Short diagnostic without signed URLs, source IDs, or job tokens.
            diagnostic = next((line for line in reversed(stderr.splitlines()) if 'error' in line.lower()), 'Downloader failed')
            diagnostic = re.sub(r'https?://\S+', '[URL]', diagnostic)
            diagnostic = re.sub(r'\b[A-Za-z0-9_-]{11,}\b', '[redacted]', diagnostic)
            print('Importer: ' + diagnostic[:400], flush=True)
            if 'confirm you' in reason or 'bot' in reason:
                raise ValueError('YouTube blocked this hosting server with a sign-in check. Upload a video file instead; changing the link may not help.')
            if 'requested format is not available' in reason:
                raise ValueError('This source has no compatible MP4 stream. Upload a video file instead.')
            raise ValueError('YouTube could not provide this video. It may be unavailable or restricted. Try another public video or upload a file.')
        return stdout
    finally:
        if proc.poll() is None:
            if os.name != 'nt':
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                proc.kill()
            proc.communicate()


def import_video(job_id, url, mode='video', start=0, seconds=30):
    folder = ROOT / job_id
    folder.mkdir()
    try:
        metadata = json.loads(execute(['--skip-download', '--dump-single-json', url], 60, job_id))
        duration = metadata.get('duration')
        live = bool(metadata.get('is_live'))
        if mode == 'live' and not live:
            raise ValueError('This video is not live now. Choose Replay section or Full video.')
        if mode != 'live' and live:
            raise ValueError('This stream is live. Choose Live capture to record a short clip.')
        if mode == 'video' and (not isinstance(duration, (int, float)) or not 0 < duration <= 900):
            raise ValueError('The free demo accepts recorded videos up to 15 minutes long.')
        if mode == 'replay' and (not isinstance(duration, (int, float)) or start >= duration):
            raise ValueError('The replay is not available yet, or the start is past its end.')
        extra = []
        if mode == 'live':
            extra = ['--no-live-from-start', '--downloader', 'ffmpeg', '--downloader-args', f'ffmpeg_o:-t {seconds} -fs {MAX_BYTES}']
        elif mode == 'replay':
            extra = ['--download-sections', f'*{start}-{min(start + seconds, duration)}', '--downloader-args', f'ffmpeg_o:-fs {MAX_BYTES}']
        with LOCK:
            JOBS[job_id]['message'] = f'Capturing {seconds:g} seconds near the live edge…' if live else 'Downloading your selected video…'
        execute(['--no-progress', '--max-filesize', str(MAX_BYTES), '--format', 'best[ext=mp4][height<=480][vcodec^=avc1][acodec!=none]/bestvideo[ext=mp4][height<=480][vcodec^=avc1]+bestaudio[ext=m4a]', '--merge-output-format', 'mp4', '--remux-video', 'mp4', '--output', str(folder / 'source.%(ext)s'), *extra, url], 240, job_id)
        if JOBS[job_id].get('cancel'):
            raise Cancelled()
        media = folder / 'source.mp4'
        if not media.is_file() or not 0 < media.stat().st_size <= MAX_BYTES:
            raise ValueError('This video exceeds the 150 MB demo limit or has no compatible MP4 format. Upload the file instead.')
        with LOCK:
            JOBS[job_id].update(state='ready', title=str(metadata.get('title') or 'YouTube video')[:160], size=media.stat().st_size)
    except Cancelled:
        with LOCK:
            JOBS[job_id].update(state='cancelled', message='Import cancelled.')
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
        if JOBS[job_id]['state'] in ('error', 'cancelled'):
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
        origin = self.headers.get('Origin')
        if origin and origin != ORIGIN:
            self.reply(403, {'message': 'This origin is not allowed.'})
            return False
        return True

    def do_OPTIONS(self):
        self.common(204)
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_POST(self):
        cancel = re.fullmatch(r'/imports/([A-Za-z0-9_-]{32})/cancel', self.path)
        if cancel:
            if not self.authorized():
                return
            with LOCK:
                job = JOBS.get(cancel[1])
                if job and job['state'] == 'loading':
                    job['cancel'] = True
            return self.reply(202, {'message': 'Cancellation requested.'})
        if self.path != '/imports':
            return self.reply(404, {'message': 'Not found.'})
        if not self.authorized():
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length < 4096:
                return self.reply(413, {'message': 'Request is too large or empty.'})
            data = json.loads(self.rfile.read(length))
            url = canonical_youtube_url(data.get('url'))
            mode, start, seconds = parse_options(data)
        except (ValueError, AttributeError, TypeError):
            return self.reply(400, {'message': 'Paste a valid HTTPS YouTube video link.'})
        if not WORKER.acquire(blocking=False):
            return self.reply(429, {'message': 'Another import is running. Try again shortly.'})
        try:
            cleanup(make_room=True)
        except OSError:
            WORKER.release()
            return self.reply(503, {'message': 'Temporary storage is unavailable. Please retry.'})
        with LOCK:
            if len(JOBS) >= 3:
                WORKER.release()
                return self.reply(429, {'message': 'Video transfers are still running. Retry when they finish.'})
        job_id = secrets.token_urlsafe(24)
        with LOCK:
            JOBS[job_id] = {'state': 'loading', 'message': 'Checking YouTube source…', 'expires': time.time() + 600}
        threading.Thread(target=import_video, args=(job_id, url, mode, start, seconds), daemon=True).start()
        self.reply(202, {'id': job_id, 'state': 'loading'})

    def do_GET(self):
        if self.path == '/health':
            return self.reply(200, {'status': 'ok', 'access': 'public'})
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
            return self.reply(200, {key: value for key, value in job.items() if key not in ('expires', 'cancel')})
        if job['state'] != 'ready':
            return self.reply(409, {'message': 'Video is not ready.'})
        with LOCK:
            if job_id not in JOBS:
                return self.reply(404, {'message': 'Import expired. Import the link again.'})
            JOBS[job_id]['readers'] = JOBS[job_id].get('readers', 0) + 1
        try:
            with (ROOT / job_id / 'source.mp4').open('rb') as media:
                self.common(200, 'video/mp4')
                self.send_header('Content-Length', str(job['size']))
                self.end_headers()
                shutil.copyfileobj(media, self.wfile, 64 * 1024)
        except (FileNotFoundError, BrokenPipeError, ConnectionResetError):
            return
        finally:
            with LOCK:
                if job_id in JOBS:
                    JOBS[job_id]['readers'] -= 1


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '10000'))), Handler).serve_forever()
