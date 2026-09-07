"""Loopback-only reading desk. Network syncs are explicit, authenticated POSTs."""
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sqlite3
import threading
import urllib.parse
import webbrowser

import ytclaw as y
import ytclaw_ops as ops
import ytclaw_reports as reports


class DeskServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, db, port=8765):
        self.db = str(Path(db).resolve()); self.token = secrets.token_urlsafe(32)
        self.sync_guard = threading.Lock(); self.task = None
        super().__init__(('127.0.0.1', port), DeskHandler)

    def connection(self, writable=False):
        uri = Path(self.db).as_uri() + ('?mode=rw' if writable else '?mode=ro')
        c = sqlite3.connect(uri, uri=True, timeout=30); c.row_factory = sqlite3.Row
        return c


class DeskHandler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def send(self, body, mime='application/json', status=200, download=None):
        if not isinstance(body, bytes): body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', mime); self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store'); self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header('Referrer-Policy', 'no-referrer')
        if download: self.send_header('Content-Disposition', 'attachment; filename="' + download + '"')
        self.end_headers(); self.wfile.write(body)

    def valid_host(self):
        port = self.server.server_port
        return self.headers.get('Host') in (f'127.0.0.1:{port}', f'localhost:{port}')

    def do_GET(self):
        if not self.valid_host(): self.send({'error': 'invalid host'}, status=403); return
        parsed = urllib.parse.urlparse(self.path); params = urllib.parse.parse_qs(parsed.query)
        get = lambda key, default=None: params.get(key, [default])[0]
        route = parsed.path
        try:
            static = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/styles.css': ('styles.css', 'text/css; charset=utf-8')}
            if route in static:
                name, mime = static[route]; self.send((Path(__file__).parent / 'web' / name).read_bytes(), mime); return
            with contextlib.closing(self.server.connection()) as c:
                channel, since, until = get('channel'), get('since'), get('until')
                if route == '/api/overview':
                    data = reports.report(c, channel, since, until, 100)
                    data.update(videos=reports.top(c, n=200, channel=channel), channels=reports.coverage(c),
                                demo=bool(y.cache_get(c, 'demo')), token=self.server.token, task=self.server.task,
                                runs=ops.runs(c, channel, 10), watch_jobs=ops.watch_jobs(c))
                    for v in data['videos']:
                        row = c.execute('select metadata_json from metadata_versions where video_id=? order by version_id desc limit 1', (v['video_id'],)).fetchone()
                        v['thumbnail_asset'] = json.loads(row[0]).get('thumbnail_asset') if row else None
                    self.send(data)
                elif route == '/api/search':
                    self.send(reports.search(c, get('q', ''), get('scope', 'all'), 50, channel, since, until))
                elif route == '/api/video':
                    vid = get('id'); data = y.video(c, vid)
                    if data is None: self.send({'error': 'video not local'}, status=404); return
                    from ytclaw_analytics import show
                    data['owner_reports'] = show(c, vid)
                    data['transcript'] = [dict(r) for r in c.execute('select start,duration,text from transcript_segments where video_id=? order by idx', (vid,))]
                    data['experiments'] = [dict(r) for r in c.execute('select * from experiments where video_id=? order by experiment_id desc', (vid,))]
                    pinned = c.execute('select version_id from metadata_baselines where video_id=?', (vid,)).fetchone()
                    data['baseline'] = y.drift(c, vid) if pinned else None
                    data['project'] = None
                    if c.execute('select 1 from project_links where video_id=?', (vid,)).fetchone():
                        try: data['project'] = ops.project_drift(c, vid)
                        except (OSError, ValueError) as e: data['project'] = {'error': str(e)}
                    self.send(data)
                elif route == '/api/report.md':
                    self.send(reports.markdown_report(reports.report(c, channel, since, until, 100)).encode(), 'text/markdown; charset=utf-8', download='ytclaw-report.md')
                elif route.startswith('/assets/'):
                    sha = route.removeprefix('/assets/')
                    row = c.execute('select content_type,data from thumbnail_assets where sha256=?', (sha,)).fetchone()
                    if not row: self.send({'error': 'image not archived'}, status=404); return
                    mime = row['content_type']
                    if mime not in ('image/png','image/jpeg','image/webp','image/gif'): mime = 'application/octet-stream'
                    self.send(row['data'], mime)
                else: self.send({'error': 'not found'}, status=404)
        except (ValueError, OSError, sqlite3.Error) as e:
            self.send({'error': str(e)}, status=400)

    def do_POST(self):
        origin = self.headers.get('Origin')
        allowed = [f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}']
        if not self.valid_host() or (origin and origin not in allowed) or not secrets.compare_digest(self.headers.get('X-Ytclaw-Token', ''), self.server.token):
            self.send({'error': 'local session token required'}, status=403); return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 65536: raise ValueError('invalid request size')
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict): raise ValueError('expected a JSON object')
            if self.path == '/api/sync':
                with contextlib.closing(self.server.connection()) as c:
                    if y.cache_get(c, 'demo'): raise ValueError('demo is offline; use a separate database for a real channel')
                    cid = reports.channel_id(c, data.get('channel'))
                    if not cid: raise ValueError('select one channel to sync')
                    ch = c.execute('select handle from channels where channel_id=?', (cid,)).fetchone()[0]
                if not self.server.sync_guard.acquire(blocking=False): self.send({'error':'sync already running'}, status=409); return
                self.server.task = {'status': 'running', 'channel': ch}
                def work():
                    try:
                        with contextlib.closing(self.server.connection(writable=True)) as c:
                            result, _ = ops.run_sync(c, self.server.db, ch, thumbnails=True, comments=True, limit=30)
                            self.server.task = result
                    except Exception as e:
                        self.server.task = {'status': 'failed', 'error': type(e).__name__}
                    finally: self.server.sync_guard.release()
                threading.Thread(target=work, daemon=True).start()
                self.send({'status':'running'}, status=202)
            elif self.path == '/api/baseline':
                with contextlib.closing(self.server.connection(writable=True)) as c:
                    self.send(y.baseline(c, data.get('video_id')))
            elif self.path == '/api/experiment':
                note = data.get('note', '')
                if not isinstance(note, str) or not note.strip(): raise ValueError('a note is required')
                with contextlib.closing(self.server.connection(writable=True)) as c:
                    self.send(ops.record_experiment(c, data.get('video_id'), note.strip(), data.get('result', '')))
            else: self.send({'error': 'not found'}, status=404)
        except (ValueError, OSError, sqlite3.Error, SystemExit) as e:
            self.send({'error': str(e)}, status=400)


def serve(db, port=8765, open_browser=True):
    with DeskServer(db, port) as server:
        url = f'http://127.0.0.1:{server.server_port}'
        print(f'ytclaw reading desk: {url}', flush=True)
        if open_browser: webbrowser.open(url)
        server.serve_forever(poll_interval=0.5)
