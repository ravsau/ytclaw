"""Read-only owner analytics, Studio CSV imports, and observational comparisons."""
import csv
from datetime import date, timedelta
import json
import math
from pathlib import Path
import time
import urllib.parse
import urllib.request

import ytclaw as y
from ytclaw_ops import write_private_json
from ytclaw_reports import channel_id, date_arg

SCOPE = 'https://www.googleapis.com/auth/yt-analytics.readonly'
REPORTS = {
    'daily': ('day', 'views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage'),
    'retention': ('elapsedVideoTimeRatio', 'audienceWatchRatio,relativeRetentionPerformance'),
    'traffic': ('insightTrafficSourceType', 'views,estimatedMinutesWatched'),
}


def auth_login(client_file):
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        raise ValueError('install owner support: pip install "ytclaw[owner]" (or uv tool install ".[owner]")') from None
    config = json.loads(Path(client_file).read_text())
    if 'installed' not in config: raise ValueError('use a Desktop app OAuth client JSON from Google Cloud')
    flow = InstalledAppFlow.from_client_config(config, [SCOPE], autogenerate_code_verifier=True)
    credentials = flow.run_local_server(host='127.0.0.1', port=0, timeout_seconds=180,
                                       access_type='offline', prompt='consent',
                                       success_message='ytclaw is connected. You can close this tab.')
    write_private_json(y.HOME / 'owner-token.json', json.loads(credentials.to_json()))
    return {'authenticated': True, 'scope': SCOPE, 'token_file': str(y.HOME / 'owner-token.json')}


def auth_status():
    path = y.HOME / 'owner-token.json'
    if not path.exists(): return {'configured': False}
    data = json.loads(path.read_text())
    return {'configured': True, 'expiry': data.get('expiry'), 'scopes': data.get('scopes'), 'network_checked': False}


def owner_token():
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
    except ImportError:
        raise ValueError('install the optional owner dependencies first; see docs/owner-analytics.md') from None
    path = y.HOME / 'owner-token.json'
    if not path.exists(): raise ValueError('run ytclaw auth login --client-secret desktop-client.json first')
    credentials = Credentials.from_authorized_user_file(str(path), [SCOPE])
    if not credentials.valid:
        try: credentials.refresh(Request())
        except Exception: raise ValueError('owner token refresh failed; run ytclaw auth login again') from None
        write_private_json(path, json.loads(credentials.to_json()))
    return credentials.token


class Analytics:
    def __init__(self, c):
        self.c = c; self.calls = 0

    def get(self, **params):
        for attempt in range(3):
            token = owner_token()
            request = urllib.request.Request('https://youtubeanalytics.googleapis.com/v2/reports?' + urllib.parse.urlencode(params),
                                             headers={'Authorization': 'Bearer ' + token})
            self.calls += 1
            key = 'analytics-calls:' + y.today()
            y.cache_set(self.c, key, (y.cache_get(self.c, key) or 0) + 1); self.c.commit()
            try:
                with urllib.request.urlopen(request, timeout=30) as response: return json.load(response)
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(2 ** attempt); continue
                raise ValueError(f'owner Analytics API HTTP {e.code}; check channel ownership, API enablement, and report availability') from None
            except OSError:
                if attempt < 2: time.sleep(2 ** attempt); continue
                raise ValueError('owner Analytics API network request failed') from None


def save_report(c, cid, vid, kind, start, end, columns, rows, source):
    if len(set(columns)) != len(columns) or any(len(r) != len(columns) for r in rows):
        raise ValueError('invalid report columns or row widths')
    c.execute('''insert into owner_reports(channel_id,video_id,kind,start_date,end_date,fetched_at,source,columns_json,rows_json)
      values(?,?,?,?,?,?,?,?,?) on conflict(channel_id,video_id,kind,start_date,end_date,source) do update set
      fetched_at=excluded.fetched_at,columns_json=excluded.columns_json,rows_json=excluded.rows_json''',
      (cid, vid, kind, start, end, y.now(), source, json.dumps(columns), json.dumps(rows)))
    c.commit()


def sync(c, channel, since, until, vid=None, kind='all', limit=50, refresh=False, client=None):
    cid = channel_id(c, channel)
    if not cid: raise ValueError('--channel is required')
    start, end = date_arg(since)[:10], date_arg(until)[:10]
    if start > end: raise ValueError('--since must be before --until')
    if vid:
        videos = c.execute('select video_id from videos where channel_id=? and video_id=?', (cid, vid)).fetchall()
        if not videos: raise ValueError('video does not belong to the selected local channel')
    else:
        videos = c.execute('select video_id from videos where channel_id=? order by published_at desc', (cid,)).fetchall()
    client = client or Analytics(c); saved = skipped = processed = 0
    kinds = list(REPORTS) if kind == 'all' else [kind]
    for (video_id,) in videos:
        pending = []
        for report_kind in kinds:
            existing = c.execute("select 1 from owner_reports where channel_id=? and video_id=? and kind=? and start_date=? and end_date=? and source='analytics_api'",
                                 (cid, video_id, report_kind, start, end)).fetchone()
            if existing and not refresh: skipped += 1
            else: pending.append(report_kind)
        if not pending: continue
        if processed >= limit: break
        processed += 1
        for report_kind in pending:
            dimension, metrics = REPORTS[report_kind]
            rows = []; columns = None; index = 1
            while True:
                response = client.get(ids='channel==' + cid, startDate=start, endDate=end,
                                      dimensions=dimension, metrics=metrics, filters='video==' + video_id,
                                      sort=dimension, startIndex=index, maxResults=200)
                page_columns = [r['name'] for r in response.get('columnHeaders', [])]
                if columns is None: columns = page_columns
                if columns != page_columns: raise ValueError('report columns changed during pagination')
                page = response.get('rows', []); rows.extend(page)
                if len(page) < 200: break
                index += len(page)
            save_report(c, cid, video_id, report_kind, start, end, columns, rows, 'analytics_api'); saved += 1
    return {'reports_saved': saved, 'reports_skipped': skipped, 'videos_processed': processed,
            'api_calls': client.calls, 'note': 'Rerun to resume skipped batches. Use --refresh to replace saved reports when YouTube revises data.'}


CSV_COLUMNS = {
    'Date': 'day', 'day': 'day', 'Views': 'views', 'views': 'views',
    'Impressions': 'impressions', 'impressions': 'impressions',
    'Impressions click-through rate (%)': 'impressions_ctr_percent', 'impressions_ctr_percent': 'impressions_ctr_percent',
    'Watch time (hours)': 'watch_time_hours', 'watch_time_hours': 'watch_time_hours',
    'Estimated revenue (USD)': 'estimated_revenue_usd', 'estimated_revenue_usd': 'estimated_revenue_usd',
}


def import_csv(c, vid, path):
    video = c.execute('select channel_id from videos where video_id=?', (vid,)).fetchone()
    if not video or not video[0]: raise ValueError('video must exist locally with a channel')
    with open(path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        selected = [h for h in headers if h in CSV_COLUMNS]
        columns = [CSV_COLUMNS[h] for h in selected]
        if 'day' not in columns or len(columns) < 2:
            raise ValueError('CSV needs Date/day and a supported metric. Export a single video by date; see docs/owner-analytics.md')
        rows = []; seen = set()
        for number, row in enumerate(reader, 2):
            day_value = row[selected[columns.index('day')]]
            if day_value in ('Total', ''): continue
            day = date.fromisoformat(day_value).isoformat()
            if day in seen: raise ValueError(f'duplicate date at CSV row {number}')
            seen.add(day); values = []
            for header, name in zip(selected, columns):
                if name == 'day': values.append(day); continue
                raw = row[header].strip().replace(',', '').removesuffix('%')
                value = None if raw in ('', '-', '--') else float(raw)
                if value is not None and not math.isfinite(value): raise ValueError(f'invalid number at CSV row {number}')
                if name == 'impressions_ctr_percent' and value is not None and not 0 <= value <= 100:
                    raise ValueError('CTR must be a percent from 0 to 100')
                values.append(value)
            rows.append(values)
    if not rows: raise ValueError('CSV contains no dated rows')
    save_report(c, video[0], vid, 'studio', min(seen), max(seen), columns, rows, 'studio_csv')
    return {'video_id': vid, 'rows': len(rows), 'columns': columns, 'source': 'studio_csv'}


def show(c, vid):
    out = []
    for r in c.execute('select * from owner_reports where video_id=? order by fetched_at,report_id', (vid,)):
        row = dict(r); row['columns'] = json.loads(row.pop('columns_json')); row['rows'] = json.loads(row.pop('rows_json')); out.append(row)
    return out


def compare(c, vid, version, days=7):
    if days < 1: raise ValueError('days must be positive')
    change = c.execute('select * from metadata_versions where video_id=? and version_id=?', (vid, version)).fetchone()
    if not change: raise ValueError('metadata version not found for this video')
    pivot = date.fromisoformat(change['observed_at'][:10]); daily = {}
    for report in show(c, vid):
        if report['kind'] != 'daily' or report['source'] != 'analytics_api': continue
        for values in report['rows']:
            row = dict(zip(report['columns'], values)); daily[row['day']] = row
    def window(start, end):
        dates = [(start + timedelta(days=i)).isoformat() for i in range((end-start).days+1)]
        rows = [daily[d] for d in dates if d in daily]
        return {'start': start.isoformat(), 'end': end.isoformat(), 'days_available': len(rows), 'days_requested': len(dates),
                'missing_days': [d for d in dates if d not in daily],
                'views': sum(r['views'] for r in rows) if rows else None,
                'watch_minutes': sum(r['estimatedMinutesWatched'] for r in rows) if rows else None}
    return {'video_id': vid, 'version_id': version, 'change_observed_at': change['observed_at'],
            'before': window(pivot-timedelta(days=days), pivot-timedelta(days=1)),
            'after': window(pivot+timedelta(days=1), pivot+timedelta(days=days)),
            'note': 'Observational comparison, not an A/B test. Excludes the observation date. API day boundaries and the actual edit time may differ; missing days are not zero.'}
