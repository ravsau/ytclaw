"""Local, source-linked queries used by the CLI and browser."""
import json
from datetime import datetime, timezone
from urllib.parse import quote

from ytclaw import days_ago, metadata_diff, now


def date_arg(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('use an ISO date or timestamp, such as 2026-09-01') from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def end_date_arg(value):
    return date_arg(value + 'T23:59:59Z' if len(value) == 10 else value)


def channel_id(c, channel):
    if not channel:
        return None
    row = c.execute('select channel_id from channels where channel_id=? or lower(handle)=lower(?)',
                    (channel, channel)).fetchone()
    if not row:
        raise ValueError(f'channel not local: {channel}; sync it first')
    return row[0]


def filters(c, channel=None, since=None, until=None, time_column='v.published_at'):
    parts, args = [], []
    if channel:
        parts.append('v.channel_id=?'); args.append(channel_id(c, channel))
    if since:
        parts.append(f'julianday({time_column})>=julianday(?)'); args.append(date_arg(since))
    if until:
        parts.append(f'julianday({time_column})<=julianday(?)'); args.append(end_date_arg(until))
    if since and until and date_arg(since) > end_date_arg(until):
        raise ValueError('--since must be before --until')
    return ''.join(' and ' + part for part in parts), args


def coverage(c, channel=None):
    cid = channel_id(c, channel)
    rows = c.execute('select * from channels' + (' where channel_id=?' if cid else '') + ' order by title',
                     (cid,) if cid else ()).fetchall()
    out = []
    for ch in rows:
        cid = ch['channel_id']
        totals = dict(c.execute('''select count(*) videos,
          sum(case when cs.metadata_synced_at is not null then 1 else 0 end) metadata_observed,
          sum(case when exists(select 1 from transcript_segments t where t.video_id=v.video_id) then 1 else 0 end) with_transcripts,
          sum(case when cs.comments_complete_at is not null and cs.comments_error is null then 1 else 0 end) comments_complete,
          sum(case when cs.thumbnails_synced_at is not null and cs.thumbnail_error is null then 1 else 0 end) thumbnails_checked,
          min(cs.metadata_synced_at) oldest_metadata_at, max(cs.metadata_synced_at) newest_metadata_at
          from videos v left join collection_status cs using(video_id) where v.channel_id=?''', (cid,)).fetchone())
        for key in ('metadata_observed', 'with_transcripts', 'comments_complete', 'thumbnails_checked'):
            totals[key] = totals[key] or 0
        totals['pending_videos'] = c.execute('select count(*) from pending_videos where channel_id=?', (cid,)).fetchone()[0]
        totals['comments_local'] = c.execute('select count(*) from comments join videos using(video_id) where channel_id=?', (cid,)).fetchone()[0]
        totals['unavailable_videos'] = c.execute("select count(*) from unresolved u join videos v on v.video_id=u.entity_id where u.kind='video' and v.channel_id=?", (cid,)).fetchone()[0]
        totals['latest_run'] = dict(c.execute('select * from sync_runs where channel_id=? or lower(handle)=lower(?) order by run_id desc limit 1', (cid, ch['handle'])).fetchone() or {})
        totals['last_success_at'] = c.execute("select max(finished_at) from sync_runs where channel_id=? and status='success'", (cid,)).fetchone()[0]
        totals['reported_public_videos'] = c.execute('select video_count from channel_snapshots where channel_id=? order by observed_at desc limit 1', (cid,)).fetchone()
        totals['reported_public_videos'] = totals['reported_public_videos'][0] if totals['reported_public_videos'] else None
        out.append({**dict(ch), **totals})
    return out


def search(c, term, scope='all', n=20, channel=None, since=None, until=None):
    from ytclaw import q
    extra, args = filters(c, channel, since, until)
    out = []
    queries = {
        'videos': ('''select v.video_id,v.title,v.url,v.published_at,v.last_seen_at observed_at,v.views,
          snippet(videos_fts,2,'[',']','...',20) snip from videos_fts join videos v using(video_id)
          where videos_fts match ?''', 'video'),
        'transcripts': ('''select v.video_id,v.title,v.url,v.published_at,v.transcript_synced_at observed_at,
          t.idx,s.start,snippet(transcript_fts,2,'[',']','...',20) snip
          from transcript_fts t join transcript_segments s on s.video_id=t.video_id and s.idx=t.idx
          join videos v on v.video_id=t.video_id where transcript_fts match ?''', 'transcript'),
        'comments': ('''select v.video_id,v.title,v.url,cm.comment_id,cm.author,cm.likes,cm.published_at,
          v.comments_synced_at observed_at,snippet(comments_fts,1,'[',']','...',20) snip
          from comments_fts f join comments cm on cm.comment_id=f.comment_id
          join videos v on v.video_id=cm.video_id where comments_fts match ?''', 'comment')}
    for name, (query, kind) in queries.items():
        if scope not in ('all', name):
            continue
        for r in c.execute(query + extra + ' order by rank limit ?', [q(term), *args, n]):
            row = {'kind': kind, **dict(r)}
            if kind == 'transcript':
                row['url'] += f"&t={int(row['start'] or 0)}s"
                row['context'] = [dict(x) for x in c.execute('select idx,start,duration,text from transcript_segments where video_id=? and idx between ? and ? order by idx',
                                                           (row['video_id'], max(0, int(row['idx'])-2), int(row['idx'])+2))]
            elif kind == 'comment':
                row['url'] += '&lc=' + quote(row['comment_id'])
            out.append(row)
    return out


def top(c, by='views', n=20, channel=None, since=None, until=None):
    if by not in ('views', 'likes', 'comments'):
        raise ValueError('rank by views, likes, or comments')
    extra, args = filters(c, channel, since, until)
    return [dict(r) for r in c.execute(f'''select v.video_id,v.title,v.url,v.published_at,v.last_seen_at observed_at,
      v.views,v.likes,v.comments from videos v where v.title is not null{extra} order by v.{by} desc limit ?''', [*args, n])]


def changes(c, channel=None, since=None, until=None, n=100):
    extra, args = filters(c, channel, since, until, 'm.observed_at')
    rows = c.execute('''select m.*,v.title,v.url,
      (select p.metadata_json from metadata_versions p where p.video_id=m.video_id and p.version_id<m.version_id
       order by p.version_id desc limit 1) previous_json
      from metadata_versions m join videos v using(video_id) where 1=1''' + extra + ' order by m.version_id desc limit ?', [*args, n]).fetchall()
    result = []
    for r in rows:
        row = dict(r); before = json.loads(row.pop('previous_json') or '{}'); after = json.loads(row.pop('metadata_json'))
        row.update(initial=not bool(before), changes=metadata_diff(before, after),
                   before_thumbnail=before.get('thumbnail_asset'), after_thumbnail=after.get('thumbnail_asset'))
        result.append(row)
    return result


def questions(c, channel=None, since=None, until=None, n=20, include_unverified=False):
    extra, args = filters(c, channel, since, until, 'cm.published_at')
    verified = '' if include_unverified else ' and cs.comments_complete_at is not null and cs.comments_error is null'
    rows = c.execute('''select v.video_id,v.title,v.url,cm.comment_id,cm.text,cm.author,cm.likes,cm.published_at,
      cs.comments_complete_at observed_at,
      case when cs.comments_complete_at is not null and cs.comments_error is null then 'complete scan' else 'unverified' end coverage
      from comments cm join videos v using(video_id) left join collection_status cs using(video_id)
      where cm.parent_id is null and cm.text like '%?%'
      and not exists(select 1 from comments reply where reply.parent_id=cm.comment_id)''' + extra + verified +
      ' order by cm.published_at desc limit ?', [*args, n])
    return [{**dict(r), 'url': r['url'] + '&lc=' + quote(r['comment_id'])} for r in rows]


def growth(c, channel=None, since=None, until=None, n=20):
    since, until = date_arg(since or days_ago(7)), end_date_arg(until or now())
    extra, args = filters(c, channel)
    if since > until:
        raise ValueError('--since must be before --until')
    rows = c.execute('select v.video_id,v.title,v.url from videos v where 1=1' + extra, args).fetchall()
    result = []
    for v in rows:
        observations = c.execute("""select * from stats_observations where video_id=? and views is not null
          and source in ('api','demo') and julianday(observed_at)<=julianday(?) order by julianday(observed_at),observation_id""", (v['video_id'], until)).fetchall()
        if len(observations) < 2:
            continue
        before = [r for r in observations if date_arg(r['observed_at']) <= since]
        start = before[-1] if before else observations[0]
        end = observations[-1]
        if date_arg(end['observed_at']) < since:
            continue
        elapsed = (datetime.fromisoformat(date_arg(end['observed_at']).replace('Z','+00:00')) -
                   datetime.fromisoformat(date_arg(start['observed_at']).replace('Z','+00:00'))).total_seconds()
        if elapsed <= 0:
            continue
        gained = end['views'] - start['views']
        result.append({**dict(v), 'views_gained': gained, 'views_per_day': round(gained / (elapsed / 86400), 2),
                       'from_views': start['views'], 'to_views': end['views'], 'from_observed_at': start['observed_at'],
                       'to_observed_at': end['observed_at'], 'partial_window': not bool(before), 'source': end['source']})
    return sorted(result, key=lambda r: r['views_gained'], reverse=True)[:n]


def report(c, channel=None, since=None, until=None, n=20):
    since, until = since or days_ago(7), until or now()
    return {'generated_at': now(), 'since': date_arg(since), 'until': end_date_arg(until),
            'coverage': coverage(c, channel), 'changes': changes(c, channel, since, until, n),
            'growth': growth(c, channel, since, until, n), 'questions': questions(c, channel, since, until, n),
            'note': 'Observed changes and view differences are not causal evidence. Missing data is not zero. Lists are limited to ' + str(n) + ' entries.'}


def markdown_report(data):
    lines = ['# Channel report', '', f"Observed window: {data['since']} to {data['until']}", '', data['note'], '', '## Collection', '']
    for c in data['coverage']:
        lines.append(f"- {c['title']}: {c['videos']} videos, {c['with_transcripts']} with transcripts, {c['comments_complete']} complete comment scans. Last success: {c['last_success_at'] or 'none recorded'}.")
    lines += ['', '## Changes', '']
    for r in data['changes']:
        lines.append(f"- [{r['title']}]({r['url']}), {r['observed_at']}: " + ('first observation' if r['initial'] else ', '.join(r['changes'])))
    if not data['changes']: lines.append('No changes observed in this window.')
    lines += ['', '## View growth', '']
    for r in data['growth']:
        lines.append(f"- [{r['title']}]({r['url']}): {r['views_gained']:+,} views between {r['from_observed_at']} and {r['to_observed_at']}.")
    if not data['growth']: lines.append('Not enough observations to compare.')
    lines += ['', '## Questions with no observed replies', '']
    for r in data['questions']:
        lines.append(f"- [{r['title']}]({r['url']}): {r['text']} (scan: {r['observed_at']})")
    if not data['questions']: lines.append('No matching questions in completed scans.')
    return '\n'.join(lines) + '\n'
