"""Configuration, durable sync runs, project links, scheduling, and portable archives."""
import base64
import contextlib
import getpass
import hashlib
import json
import os
from pathlib import Path
import plistlib
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile

import ytclaw as y


def write_private_json(path, data):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2); f.write('\n')
        os.replace(temp, path)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def initialize(channel=None, interactive=True):
    path = y.HOME / 'config.json'
    data = json.loads(path.read_text()) if path.exists() else {}
    key = os.environ.get('YOUTUBE_API_KEY') or data.get('api_key')
    if interactive and sys.stdin.isatty():
        if not key:
            print('Enable YouTube Data API v3 and create an API key: https://console.cloud.google.com/apis/credentials')
            key = getpass.getpass('YouTube API key (hidden): ').strip()
        if not channel: channel = input('Channel handle, for example @CloudYeti: ').strip()
    if key: data['api_key'] = key
    if channel: data['default_channel'] = channel
    write_private_json(path, data)
    return {'config': str(path), 'api_key_configured': bool(key), 'default_channel': data.get('default_channel'),
            'next': 'ytclaw sync' if key and data.get('default_channel') else 'Set YOUTUBE_API_KEY and run ytclaw init --channel @handle'}


def default_channel():
    path = y.HOME / 'config.json'
    channel = (json.loads(path.read_text()) if path.exists() else {}).get('default_channel')
    if not channel: raise ValueError('specify @handle or run ytclaw init --channel @handle')
    return channel


def doctor(c, db):
    from importlib.util import find_spec
    from ytclaw_reports import coverage
    try: y.api_key(); has_key = True
    except SystemExit: has_key = False
    return {'database': str(Path(db).resolve()), 'integrity': c.execute('pragma quick_check').fetchone()[0],
            'api_key_configured': has_key, 'owner_auth_configured': (y.HOME / 'owner-token.json').exists(),
            'dependencies': {name: find_spec(name) is not None for name in ('yaml', 'youtube_transcript_api')},
            'coverage': coverage(c), 'network_checked': False,
            'legacy_timestamp_note': 'New timestamps are UTC. Pre-0.3 timestamps without an offset retain their original local clock values.'}


@contextlib.contextmanager
def sync_lock(db):
    import fcntl
    lock_path = Path(str(Path(db).resolve()) + '.sync.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, 'a') as f:
        try: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError('another sync is running for this database') from None
        try: yield
        finally: fcntl.flock(f, fcntl.LOCK_UN)


def run_sync(c, db, handle, comments=False, transcripts=False, thumbnails=False,
             full=False, limit=200, comment_max_age=7, langs='en,en-US,en-GB'):
    with sync_lock(db):
        # Holding this lock means any unfinished predecessor is no longer running.
        c.execute("update sync_runs set status='interrupted',finished_at=?,error='process ended before completion' where status='running'", (y.now(),))
        run = c.execute("insert into sync_runs(handle,started_at,status) values(?,?,'running')", (handle, y.now())).lastrowid
        c.commit(); out = {'run_id': run}; yt = None; code = 0; status = 'success'; error = None
        try:
            yt = y.YT(c, y.api_key())
            ch = y.sync_channel(c, yt, handle)
            c.execute('update sync_runs set channel_id=? where run_id=?', (ch['channel_id'], run)); c.commit()
            out.update(channel=ch['title'], **y.sync_videos(c, yt, ch, full, thumbnails))
            if comments: out.update(y.sync_comments(c, yt, ch, comment_max_age, limit))
            if transcripts: out.update(y.sync_transcripts(c, ch, limit, langs.split(',')))
            if out.get('stopped'):
                status = 'partial'; code = 75
            elif any(out.get(k) for k in ('thumbnail_errors', 'comment_errors')):
                status = 'partial'; code = 75
        except y.QuotaExhausted as e:
            error = str(e); status = 'partial'; code = 75
        except (y.ApiError, OSError, ValueError, SystemExit) as e:
            error = str(e) if isinstance(e, (y.ApiError, ValueError, SystemExit)) else type(e).__name__
            status = 'failed'; code = 1
        except (KeyboardInterrupt, Exception) as e:
            status = 'interrupted' if isinstance(e, KeyboardInterrupt) else 'failed'
            error = type(e).__name__; code = 130 if isinstance(e, KeyboardInterrupt) else 1
            # Save the run record before propagating unexpected programming failures.
            c.commit()
            c.execute('update sync_runs set status=?,finished_at=?,error=? where run_id=?', (status, y.now(), error, run)); c.commit()
            raise
        out.update(status=status, quota_units_this_run=yt.used if yt else 0,
                   quota_units_today=(y.cache_get(c, f'quota:{y.today()}') or {}).get('units', 0))
        if error: out['stopped'] = error
        c.commit()
        c.execute('update sync_runs set status=?,finished_at=?,summary_json=?,error=? where run_id=?',
                  (status, y.now(), json.dumps(out), error or out.get('stopped'), run)); c.commit()
        return out, code


def runs(c, channel=None, n=20):
    from ytclaw_reports import channel_id
    cid = channel_id(c, channel) if channel else None
    return [dict(r) for r in c.execute('select * from sync_runs' + (' where channel_id=? or lower(handle)=lower(?)' if cid else '') +
                                      ' order by run_id desc limit ?', [cid, channel, n] if cid else [n])]


def load_project(manifest):
    path = Path(manifest).expanduser().resolve()
    data = json.loads(path.read_text())
    if not isinstance(data, dict): raise ValueError('project manifest must be a JSON object')
    expected = data.get('metadata', {})
    if not isinstance(expected, dict): raise ValueError('metadata must be an object')
    expected = expected.copy()
    allowed = set(y.METADATA_FIELDS) - {'thumbnails', 'thumbnail_asset'}
    if expected.keys() - allowed: raise ValueError('unsupported metadata fields: ' + ', '.join(sorted(expected.keys() - allowed)))
    def read(name):
        return (path.parent / data[name]).read_text()
    for field in ('title', 'description'):
        if field + '_file' in data:
            # Ignore one editor-added final newline, retaining internal whitespace.
            expected[field] = read(field + '_file').removesuffix('\n').removesuffix('\r')
    if 'tags_file' in data: expected['tags'] = json.loads(read('tags_file'))
    if 'tags' in expected:
        if not isinstance(expected['tags'], list) or any(not isinstance(t, str) for t in expected['tags']):
            raise ValueError('tags must be a JSON array of strings')
        expected['tags'] = sorted(set(expected['tags']))
    thumbnail = None
    if data.get('thumbnail_file'):
        content = (path.parent / data['thumbnail_file']).read_bytes()
        thumbnail = {'sha256': hashlib.sha256(content).hexdigest(), 'path': str(path.parent / data['thumbnail_file'])}
    if not expected and not thumbnail: raise ValueError('manifest needs metadata, a metadata file mapping, or thumbnail_file')
    return path, expected, thumbnail


def link_project(c, vid, manifest):
    if not c.execute('select 1 from videos where video_id=?', (vid,)).fetchone():
        raise ValueError('sync or import this video before linking files')
    path, _, _ = load_project(manifest)
    c.execute('insert or replace into project_links values(?,?)', (vid, str(path))); c.commit()
    return {'video_id': vid, 'manifest': str(path)}


def project_drift(c, vid):
    link = c.execute('select manifest_path from project_links where video_id=?', (vid,)).fetchone()
    if not link: raise ValueError('no project link; run ytclaw project link VIDEO_ID manifest.json')
    path, expected, image = load_project(link[0])
    # Compare against the last API observation, not a subsequent YAML import.
    row = c.execute("select * from metadata_versions where video_id=? and source='api' order by version_id desc limit 1", (vid,)).fetchone()
    if not row: raise ValueError('no API metadata observation; sync this channel first')
    actual = json.loads(row['metadata_json'])
    differences = y.metadata_diff(expected, {k: actual.get(k) for k in expected})
    result = {'video_id': vid, 'manifest': str(path), 'observed_at': row['last_seen_at'],
              'changes': differences, 'drifted': bool(differences)}
    if image:
        cs = c.execute('select * from collection_status where video_id=?', (vid,)).fetchone()
        asset = actual.get('thumbnail_asset')
        result['thumbnail'] = {'local_sha256': image['sha256'], 'archived_sha256': asset.get('sha256') if asset else None,
                               'checked_at': cs['thumbnails_synced_at'] if cs else None,
                               'error': cs['thumbnail_error'] if cs else None,
                               'status': 'unknown' if not asset or not cs or cs['thumbnail_error'] else
                                         ('bytes_match' if asset['sha256'] == image['sha256'] else 'bytes_differ'),
                               'note': 'YouTube can resize or recompress uploads. Different bytes do not prove a different design.'}
    return result


def record_experiment(c, vid, note, result='', started_at=None, ended_at=None, source_url=None):
    from ytclaw_reports import date_arg
    if not c.execute('select 1 from videos where video_id=?', (vid,)).fetchone(): raise ValueError('video not local')
    start = date_arg(started_at) if started_at else None
    end = date_arg(ended_at) if ended_at else None
    if start and end and start > end: raise ValueError('experiment end must follow start')
    eid = c.execute('insert into experiments(video_id,recorded_at,started_at,ended_at,note,result,source_url) values(?,?,?,?,?,?,?)',
                    (vid, y.now(), start, end, note, result, source_url)).lastrowid
    c.commit(); return {'experiment_id': eid, 'video_id': vid, 'source': 'user note'}


def backup(c, output):
    path = Path(output).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600); os.close(fd)
    try:
        with contextlib.closing(sqlite3.connect(path)) as dest:
            c.backup(dest)
            if dest.execute('pragma integrity_check').fetchone()[0] != 'ok': raise ValueError('backup integrity check failed')
        return {'output': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'integrity': 'ok'}
    except BaseException:
        path.unlink(missing_ok=True); raise


def export_archive(c, output):
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / 'database.sqlite'; backup(c, db)
        # Both the database and readable JSONL come from the same SQLite snapshot.
        with contextlib.closing(sqlite3.connect(db)) as snap:
            snap.row_factory = sqlite3.Row
            tables = [r[0] for r in snap.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name")
                      if not any(r[0].startswith(prefix) for prefix in ('videos_fts', 'transcript_fts', 'comments_fts'))]
            manifest = {'format': 'ytclaw-archive-v1', 'created_at': y.now(), 'database_sha256': hashlib.sha256(db.read_bytes()).hexdigest(),
                        'tables': {}, 'credentials_included': False}
            with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED) as z:
                z.write(db, 'database.sqlite')
                for name in tables:
                    rows = [dict(r) for r in snap.execute('select * from "' + name.replace('"', '""') + '"')]
                    body = ''.join(json.dumps(row, default=lambda b: {'base64': base64.b64encode(b).decode()}) + '\n' for row in rows)
                    z.writestr('tables/' + name + '.jsonl', body)
                    manifest['tables'][name] = {'rows': len(rows), 'sha256': hashlib.sha256(body.encode()).hexdigest()}
                z.writestr('manifest.json', json.dumps(manifest, indent=2))
    return {'output': str(output), 'format': manifest['format'], 'tables': len(tables)}


def restore(source, output):
    path = Path(output).expanduser().resolve()
    if path.exists(): raise ValueError('restore destination exists; choose a new --db path')
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=path.parent) as tmp:
        incoming = Path(source).expanduser().resolve()
        if zipfile.is_zipfile(incoming):
            with zipfile.ZipFile(incoming) as z:
                manifest = json.loads(z.read('manifest.json'))
                if manifest.get('format') != 'ytclaw-archive-v1': raise ValueError('unsupported archive format')
                payload = z.read('database.sqlite')
                if hashlib.sha256(payload).hexdigest() != manifest['database_sha256']: raise ValueError('archive database checksum mismatch')
                for name, info in manifest['tables'].items():
                    if hashlib.sha256(z.read('tables/' + name + '.jsonl')).hexdigest() != info['sha256']:
                        raise ValueError('archive table checksum mismatch: ' + name)
                incoming = Path(tmp) / 'incoming.sqlite'; incoming.write_bytes(payload)
        with contextlib.closing(sqlite3.connect(incoming.as_uri() + '?mode=ro', uri=True)) as src:
            if src.execute('pragma integrity_check').fetchone()[0] != 'ok': raise ValueError('source database integrity check failed')
            if not src.execute("select 1 from sqlite_master where type='table' and name='videos'").fetchone(): raise ValueError('not a ytclaw database')
            result = backup(src, path)
    return result


def watch_jobs(c):
    return [dict(r) for r in c.execute('select * from watch_jobs order by handle')]


def watch_add(c, handle, hours=24, **options):
    import math
    if not math.isfinite(hours) or hours < 0.1: raise ValueError('interval must be finite and at least 0.1 hours')
    if options.get('comment_max_age', 7) < 0: raise ValueError('comment max age cannot be negative')
    c.execute('''insert into watch_jobs(handle,interval_seconds,next_run,options_json) values(?,?,?,?)
      on conflict(handle) do update set interval_seconds=excluded.interval_seconds,options_json=excluded.options_json''',
      (handle, int(hours * 3600), 0, json.dumps(options))); c.commit()
    return {'handle': handle, 'every_hours': hours, 'next': 'ytclaw watch run --once, or ytclaw watch install'}


def watch_run(c, db, once=False):
    with sync_lock(str(db) + ".watch"):
        return _watch_loop(c, db, once)


def _watch_loop(c, db, once):
    while True:
        for job in watch_jobs(c):
            if job['next_run'] > time.time(): continue
            try:
                result, code = run_sync(c, db, job['handle'], **json.loads(job['options_json']))
            except ValueError as e:
                result, code = {'status': 'deferred', 'reason': str(e)}, 75
            delay = job['interval_seconds'] if code == 0 else max(3600, min(job['interval_seconds'], 21600))
            c.execute('update watch_jobs set next_run=?,last_status=? where handle=?', (time.time() + delay, result['status'], job['handle'])); c.commit()
            print(json.dumps(result), flush=True)
        if once: return
        time.sleep(30)


def schedule(db, action):
    """Install an OS timer that runs due jobs. Called only by explicit CLI action."""
    db = str(Path(db).resolve())
    suffix = hashlib.sha256(db.encode()).hexdigest()[:10]
    label = 'local.ytclaw.' + suffix
    command = [sys.executable, str(Path(y.__file__).resolve()), '--db', db, 'watch', 'run', '--once']
    if sys.platform == 'darwin':
        path = Path.home() / 'Library/LaunchAgents' / (label + '.plist')
        if action == 'remove':
            subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{label}'], capture_output=True)
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True); y.HOME.mkdir(parents=True, exist_ok=True)
            with open(path, 'wb') as f:
                plistlib.dump({'Label': label, 'ProgramArguments': command, 'StartInterval': 300, 'RunAtLoad': True,
                               'EnvironmentVariables': {'YTCLAW_HOME': str(y.HOME.resolve())},
                               'StandardOutPath': str(y.HOME / (label + '.log')), 'StandardErrorPath': str(y.HOME / (label + '.error.log'))}, f)
            subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{label}'], capture_output=True)
            subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(path)], check=True, capture_output=True)
    elif sys.platform.startswith('linux'):
        folder = Path.home() / '.config/systemd/user'; folder.mkdir(parents=True, exist_ok=True)
        path = folder / (label + '.timer'); service = folder / (label + '.service')
        if action == 'remove':
            subprocess.run(['systemctl', '--user', 'disable', '--now', label + '.timer'], check=True, capture_output=True)
            path.unlink(missing_ok=True); service.unlink(missing_ok=True)
        else:
            # systemd uses its own quoting, not shell quoting.
            escape = lambda text: '"' + text.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'
            service.write_text('[Unit]\nDescription=ytclaw due channel syncs\n[Service]\nType=oneshot\nEnvironment=' + escape('YTCLAW_HOME=' + str(y.HOME.resolve())) + '\nExecStart=' + ' '.join(escape(x) for x in command) + '\n')
            path.write_text('[Unit]\nDescription=ytclaw monitor\n[Timer]\nOnBootSec=1min\nOnUnitActiveSec=5min\n[Install]\nWantedBy=timers.target\n')
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True, capture_output=True)
        if action != 'remove': subprocess.run(['systemctl', '--user', 'enable', '--now', label + '.timer'], check=True, capture_output=True)
    else:
        raise ValueError('OS timer installation supports macOS and Linux; run ytclaw watch run in your scheduler')
    return {'action': action, 'timer': str(path), 'database': db}
