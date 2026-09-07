#!/usr/bin/env python3
"""ytclaw - local SQLite memory for a YouTube channel. Sync once, search forever.

Pattern borrowed from birdclaw by Peter Steinberger (@steipete): one SQLite
file, FTS5 shadow tables, content-hashed snapshots, per-resource cursors in a
sync_cache, and a hard split between free local reads and metered network calls.

  ytclaw sync @handle                 videos + stats (Data API v3, ~1 quota unit per 50 videos)
  ytclaw sync @handle --comments      + comment threads (1 unit per video)
  ytclaw sync @handle --transcripts   + captions via youtube-transcript-api (0 quota)
  ytclaw search "phrase" [--in videos|transcripts|comments] [-n 20]
  ytclaw video VIDEO_ID | top [--by views] | stats | sql "select ..."
  ytclaw import --yaml DIR            optional: load an existing per-video YAML dump
  ytclaw skill [install]              print or install the bundled Claude Code skill

Auth: YOUTUBE_API_KEY env var, or "api_key" in ~/.ytclaw/config.json.
Every network call is counted; `stats` shows quota units used today.
"""
import argparse, hashlib, json, os, sqlite3, sys, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

HOME = Path(os.environ.get("YTCLAW_HOME", Path.home() / ".ytclaw"))
DEFAULT_DB = Path(os.environ.get("YTCLAW_DB", HOME / "ytclaw.sqlite"))
API = "https://www.googleapis.com/youtube/v3/"
QUOTA = {"channels": 1, "playlistItems": 1, "videos": 1, "commentThreads": 1, "comments": 1, "search": 100}

SCHEMA = """
create table if not exists channels(channel_id text primary key, handle text, title text,
  uploads_playlist text, first_seen_at text, last_seen_at text);
create table if not exists videos(
  video_id text primary key, channel_id text, title text, description text,
  tags_json text, published_at text, duration text, url text,
  views integer, likes integer, comments integer,
  first_seen_at text, last_seen_at text, seen_count integer default 1,
  comments_synced_at text, transcript_synced_at text);
create table if not exists transcript_segments(
  video_id text, idx integer, start real, duration real, text text, primary key(video_id, idx));
create table if not exists comments(
  comment_id text primary key, video_id text, author text, text text,
  likes integer, published_at text, parent_id text);
create table if not exists stats_snapshots(
  video_id text, snapshot_hash text, observed_at text, last_seen_at text,
  views integer, likes integer, comments integer, source text, primary key(video_id, snapshot_hash));
create table if not exists metadata_versions(
  version_id integer primary key, video_id text not null, source text not null,
  observed_at text not null, last_seen_at text not null, snapshot_hash text not null,
  metadata_json text not null);
create index if not exists metadata_video on metadata_versions(video_id, version_id);
create table if not exists metadata_baselines(video_id text primary key, version_id integer not null);
create table if not exists thumbnail_assets(
  sha256 text primary key, content_type text, data blob not null);
create table if not exists channel_snapshots(
  observed_at text, channel_id text, subscribers integer, total_views integer,
  video_count integer, source text, primary key(channel_id, observed_at));
create table if not exists unresolved(kind text, entity_id text, reason text,
  last_attempted_at text, ttl_until text, primary key(kind, entity_id));
create table if not exists sync_cache(cache_key text primary key, value_json text, updated_at text);
create table if not exists pending_videos(video_id text primary key, channel_id text not null);
create table if not exists collection_status(video_id text primary key, metadata_synced_at text,
  thumbnails_synced_at text, thumbnail_error text, comments_complete_at text, comments_error text);
create table if not exists comment_scan_items(video_id text, comment_id text, primary key(video_id,comment_id));
create table if not exists stats_observations(
  observation_id integer primary key, video_id text, observed_at text, views integer,
  likes integer, comments integer, source text);
create index if not exists observations_video_time on stats_observations(video_id, observed_at);
create table if not exists sync_runs(run_id integer primary key, handle text, channel_id text,
  started_at text, finished_at text, status text, summary_json text, error text);
create table if not exists watch_jobs(handle text primary key, interval_seconds integer not null,
  next_run real not null, options_json text not null, last_status text);
create table if not exists project_links(video_id text primary key, manifest_path text not null);
create table if not exists experiments(experiment_id integer primary key, video_id text not null,
  recorded_at text, started_at text, ended_at text, note text, result text, source_url text);
create table if not exists owner_reports(report_id integer primary key, channel_id text not null,
  video_id text not null, kind text not null, start_date text, end_date text, fetched_at text,
  source text, columns_json text, rows_json text,
  unique(channel_id, video_id, kind, start_date, end_date, source));
create virtual table if not exists videos_fts using fts5(video_id unindexed, title, description);
create virtual table if not exists transcript_fts using fts5(video_id unindexed, idx unindexed, text);
create virtual table if not exists comments_fts using fts5(comment_id unindexed, text);
"""

def S(x): return None if x is None else str(x)
def now(): return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
def today(): return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
def days_ago(days): return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
def h(*parts): return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]

def connect(db):
    if str(db) != ':memory:':
        Path(db).parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(db, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600); os.close(fd)
        except FileExistsError:
            pass
    c = sqlite3.connect(db, timeout=30); c.row_factory = sqlite3.Row
    c.execute("pragma journal_mode=WAL")
    c.executescript(SCHEMA)
    # Older releases keyed channel observations only by time, losing concurrent channels.
    pk = [r[1] for r in c.execute("pragma table_info(channel_snapshots)") if r[5]]
    if pk == ["observed_at"]:
        c.executescript("""begin;
          alter table channel_snapshots rename to channel_snapshots_old;
          create table channel_snapshots(observed_at text, channel_id text, subscribers integer,
            total_views integer, video_count integer, source text, primary key(channel_id,observed_at));
          insert into channel_snapshots select * from channel_snapshots_old;
          drop table channel_snapshots_old;
          commit;""")
    if not cache_get(c, "migration:observations-v1"):
        c.execute("""insert into stats_observations(video_id,observed_at,views,likes,comments,source)
          select video_id,observed_at,views,likes,comments,'legacy:' || source from stats_snapshots
          order by observed_at""")
        cache_set(c, "migration:observations-v1", True)
    # Preserve the last local copy before the first sync with history enabled.
    for r in c.execute("select * from videos where video_id not in (select video_id from metadata_versions)").fetchall():
        v = dict(r); v["tags"] = json.loads(v.pop("tags_json") or "[]")
        snap_metadata(c, v, "migration", now())
    c.commit()
    return c

def cache_get(c, key):
    r = c.execute("select value_json from sync_cache where cache_key=?", (key,)).fetchone()
    return json.loads(r[0]) if r else None
def cache_set(c, key, val):
    c.execute("insert or replace into sync_cache values(?,?,?)", (key, json.dumps(val), now()))

# ---------- network (the only metered part) ----------
class QuotaExhausted(Exception): pass
class ApiError(Exception):
    def __init__(self, code, resource, body): super().__init__(f"YouTube API {code} on {resource}: {body}"); self.code = code; self.body = body

class YT:
    def __init__(self, c, key):
        self.c, self.key, self.used = c, key, 0
    def get(self, resource, **params):
        params.update(key=self.key)
        url = API + resource + "?" + urllib.parse.urlencode(params, doseq=True)
        for attempt in range(3):
            # Failed requests also spend quota. Persist the ledger before each attempt.
            self.used += QUOTA.get(resource, 1)
            q = cache_get(self.c, f"quota:{today()}") or {"units": 0, "calls": 0}
            q["units"] += QUOTA.get(resource, 1); q["calls"] += 1
            cache_set(self.c, f"quota:{today()}", q); self.c.commit()
            try:
                with urllib.request.urlopen(url, timeout=30) as response:
                    return json.load(response)
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace"); e.close()
                try: reasons = [x.get("reason", "") for x in json.loads(body).get("error", {}).get("errors", [])]
                except (ValueError, AttributeError): reasons = []
                reason = ",".join(reasons) or f"HTTP {e.code}"
                if e.code == 403 and any(r in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded") for r in reasons):
                    raise QuotaExhausted(f"YouTube quota/rate limit hit on {resource}; rerun later.") from None
                if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(2 ** attempt); continue
                raise ApiError(e.code, resource, reason) from None
            except OSError:
                if attempt < 2: time.sleep(2 ** attempt); continue
                raise ApiError(0, resource, "network request failed after 3 attempts") from None

def api_key():
    k = os.environ.get("YOUTUBE_API_KEY")
    if not k and (HOME / "config.json").exists(): k = json.loads((HOME / "config.json").read_text()).get("api_key")
    if not k: raise SystemExit("no API key: set YOUTUBE_API_KEY or put {\"api_key\": ...} in ~/.ytclaw/config.json\n"
                               "get one free at https://console.cloud.google.com/apis/credentials (enable YouTube Data API v3)")
    return k

# ---------- upserts ----------
METADATA_FIELDS = ("channel_id", "title", "description", "tags", "published_at", "duration",
                   "category_id", "default_language", "default_audio_language", "thumbnails",
                   "thumbnail_asset")

def metadata_diff(before, after):
    return {k: {"before": before.get(k), "after": after.get(k)}
            for k in sorted(before.keys() | after.keys()) if before.get(k) != after.get(k)}

def snap_metadata(c, v, source, observed_at):
    previous = c.execute("select * from metadata_versions where video_id=? order by version_id desc limit 1",
                         (v["video_id"],)).fetchone()
    data = json.loads(previous["metadata_json"]) if previous else {}
    data.update({k: S(v[k]) if k == "published_at" else v[k]
                 for k in METADATA_FIELDS if k in v and (v[k] is not None or k == "thumbnail_asset")})
    if "tags" in data: data["tags"] = sorted(set(data["tags"]))
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    if previous and previous["snapshot_hash"] == digest and previous["source"] == source:
        c.execute("update metadata_versions set last_seen_at=? where version_id=?", (observed_at, previous["version_id"]))
        return
    c.execute("insert into metadata_versions(video_id,source,observed_at,last_seen_at,snapshot_hash,metadata_json) values(?,?,?,?,?,?)",
              (v["video_id"], source, observed_at, observed_at, digest, encoded))

def metadata_history(c, vid):
    result, before = [], {}
    for r in c.execute("select * from metadata_versions where video_id=? order by version_id", (vid,)):
        row = dict(r); data = json.loads(row.pop("metadata_json"))
        row.update(metadata=data, changes=metadata_diff(before, data)); result.append(row); before = data
    return result

def baseline(c, vid):
    row = c.execute("select max(version_id) from metadata_versions where video_id=?", (vid,)).fetchone()
    if row[0] is None: raise SystemExit(f"no metadata for {vid}; sync or import first")
    c.execute("insert or replace into metadata_baselines values(?,?)", (vid, row[0])); c.commit()
    return {"video_id": vid, "baseline_version": row[0]}

def drift(c, vid):
    pinned = c.execute("select version_id from metadata_baselines where video_id=?", (vid,)).fetchone()
    if not pinned: raise SystemExit(f"no baseline for {vid}; run: ytclaw baseline {vid}")
    versions = metadata_history(c, vid)
    base = next(v for v in versions if v["version_id"] == pinned[0])
    latest = versions[-1]
    changes = metadata_diff(base["metadata"], latest["metadata"])
    return {"video_id": vid, "baseline_version": pinned[0], "latest_version": latest["version_id"],
            "observed_at": latest["observed_at"], "drifted": bool(changes), "changes": changes}

def archive_thumbnail(c, thumbnails):
    if not thumbnails: return None
    item = max(thumbnails.values(), key=lambda x: x.get("width", 0) * x.get("height", 0))
    url = item["url"]
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".ytimg.com"):
        raise ValueError("unexpected thumbnail host")
    with urllib.request.urlopen(url, timeout=30) as response:
        content_type = response.headers.get("Content-Type", "").split(";")[0]
        data = response.read(10 * 1024 * 1024 + 1)
    if content_type not in ("image/png", "image/jpeg", "image/webp", "image/gif") or len(data) > 10 * 1024 * 1024:
        raise ValueError("invalid or oversized thumbnail")
    digest = hashlib.sha256(data).hexdigest()
    c.execute("insert or ignore into thumbnail_assets values(?,?,?)", (digest, content_type, data))
    return {"sha256": digest, "url": url, "content_type": content_type}

def upsert_video(c, v, source, observed_at=None):
    t = now()
    snap_metadata(c, v, source, observed_at or t)
    c.execute("""insert into videos(video_id,channel_id,title,description,tags_json,published_at,duration,url,
      views,likes,comments,first_seen_at,last_seen_at,seen_count) values(?,?,?,?,?,?,?,?,?,?,?,?,?,1)
      on conflict(video_id) do update set
      channel_id=coalesce(excluded.channel_id,channel_id), title=coalesce(excluded.title,title),
      description=coalesce(excluded.description,description), tags_json=coalesce(excluded.tags_json,tags_json),
      published_at=coalesce(excluded.published_at,published_at), duration=coalesce(excluded.duration,duration),
      views=coalesce(excluded.views,views), likes=coalesce(excluded.likes,likes), comments=coalesce(excluded.comments,comments),
      last_seen_at=excluded.last_seen_at, seen_count=seen_count+1""",
      (v["video_id"], v.get("channel_id"), v.get("title"), v.get("description"), json.dumps(v["tags"] or []) if "tags" in v else None,
       S(v.get("published_at")), v.get("duration"), f"https://www.youtube.com/watch?v={v['video_id']}",
       v.get("views"), v.get("likes"), v.get("comments"), t, t))
    c.execute("delete from videos_fts where video_id=?", (v["video_id"],))
    c.execute("insert into videos_fts select video_id,coalesce(title,''),coalesce(description,'') from videos where video_id=?", (v["video_id"],))
    if v.get("views") is not None:
        snap_stats(c, v["video_id"], observed_at or t, v.get("views"), v.get("likes"), v.get("comments"), source)

def snap_stats(c, vid, observed_at, views, likes, comments, source):
    c.execute("insert into stats_observations(video_id,observed_at,views,likes,comments,source) values(?,?,?,?,?,?)",
              (vid, observed_at, views, likes, comments, source))
    c.execute("""insert into stats_snapshots values(?,?,?,?,?,?,?,?)
      on conflict(video_id,snapshot_hash) do update set last_seen_at=max(last_seen_at,excluded.last_seen_at)""",
      (vid, h(views, likes, comments), observed_at, observed_at, views, likes, comments, source))

def replace_transcript(c, vid, segs):
    c.execute("delete from transcript_segments where video_id=?", (vid,))
    c.execute("delete from transcript_fts where video_id=?", (vid,))
    rows = [(vid, i, s.get("start"), s.get("duration"), s.get("text") or "") for i, s in enumerate(segs)]
    c.executemany("insert into transcript_segments values(?,?,?,?,?)", rows)
    c.executemany("insert into transcript_fts values(?,?,?)", [(vid, i, t) for (_, i, _, _, t) in rows])
    c.execute("update videos set transcript_synced_at=? where video_id=?", (now(), vid))

def upsert_comment(c, vid, cm, parent=None):
    cid = cm.get("id") or h(vid, cm.get("author"), cm.get("published_at"), (cm.get("text") or "")[:40])
    c.execute("""insert into comments values(?,?,?,?,?,?,?) on conflict(comment_id) do update set likes=excluded.likes, text=excluded.text""",
              (cid, vid, cm.get("author"), cm.get("text"), cm.get("likes"), S(cm.get("published_at")), parent))
    c.execute("delete from comments_fts where comment_id=?", (cid,))
    c.execute("insert into comments_fts values(?,?)", (cid, cm.get("text") or ""))
    return cid

def mark_unresolved(c, kind, eid, reason, ttl_days=30):
    ttl = days_ago(-ttl_days)
    c.execute("insert or replace into unresolved values(?,?,?,?,?)", (kind, eid, reason[:200], now(), ttl))

# ---------- sync from YouTube ----------
def sync_channel(c, yt, handle):
    p = {"forHandle": handle} if handle.startswith("@") else {"id": handle}
    d = yt.get("channels", part="snippet,contentDetails,statistics", **p)
    if not d.get("items"): raise ApiError(404, "channels", "channel not found")
    it = d["items"][0]
    ch = {"channel_id": it["id"], "title": it["snippet"]["title"], "uploads": it["contentDetails"]["relatedPlaylists"]["uploads"]}
    c.execute("""insert into channels values(?,?,?,?,?,?) on conflict(channel_id) do update set
      handle=excluded.handle,title=excluded.title,uploads_playlist=excluded.uploads_playlist,last_seen_at=excluded.last_seen_at""",
      (ch["channel_id"], handle, ch["title"], ch["uploads"], now(), now()))
    st = it.get("statistics", {})
    number = lambda name: int(st[name]) if name in st else None
    c.execute("insert or replace into channel_snapshots values(?,?,?,?,?,?)",
              (now(), ch["channel_id"], number("subscriberCount"), number("viewCount"), number("videoCount"), "api"))
    c.commit()
    return ch

def sync_videos(c, yt, ch, full, thumbnails=False):
    local = {r[0] for r in c.execute("select video_id from videos where channel_id=?", (ch["channel_id"],))}
    cur_key = f"uploads:{ch['channel_id']}:cursor"; cur = cache_get(c, cur_key) or {}
    token = cur.get("token") if cur.get("state") == "pending" else None
    if token: full = full or cur.get("full", False)
    pages = 0
    while True:
        p = {"part": "contentDetails", "playlistId": ch["uploads"], "maxResults": 50}
        if token: p["pageToken"] = token
        try:
            d = yt.get("playlistItems", **p)
        except ApiError as e:
            if token and e.code == 400 and "invalidPageToken" in e.body:
                token = None; continue
            raise
        pages += 1
        ids = [i["contentDetails"]["videoId"] for i in d.get("items", [])]
        fresh = [i for i in ids if i not in local]
        c.executemany("insert or ignore into pending_videos values(?,?)", [(i, ch["channel_id"]) for i in fresh])
        token = d.get("nextPageToken")
        done = not token or (not full and not fresh and local)
        cache_set(c, cur_key, {"state": "committed" if done else "pending", "token": None if done else token, "full": full})
        # Discovered IDs and their continuation token are durable together.
        c.commit()
        if done: break
    pending = {r[0] for r in c.execute("select video_id from pending_videos where channel_id=?", (ch["channel_id"],))}
    targets = sorted(local | pending)
    refreshed = images = image_errors = new_count = 0
    for i in range(0, len(targets), 50):
        batch = targets[i:i+50]
        d = yt.get("videos", part="snippet,contentDetails,statistics", id=",".join(batch), maxResults=50)
        seen = set()
        for it in d.get("items", []):
            sn, st = it["snippet"], it.get("statistics", {})
            vid = it["id"]; seen.add(vid); obs = now()
            extra = {"category_id": sn.get("categoryId", ""), "default_language": sn.get("defaultLanguage", ""),
                     "default_audio_language": sn.get("defaultAudioLanguage", ""), "thumbnails": sn.get("thumbnails", {})}
            c.execute("insert or ignore into collection_status(video_id) values(?)", (vid,))
            if thumbnails:
                try:
                    extra["thumbnail_asset"] = archive_thumbnail(c, extra["thumbnails"])
                    c.execute("update collection_status set thumbnails_synced_at=?,thumbnail_error=null where video_id=?", (obs, vid))
                    images += 1
                except (OSError, ValueError) as e:
                    c.execute("update collection_status set thumbnail_error=? where video_id=?", (type(e).__name__, vid))
                    image_errors += 1
            upsert_video(c, {**extra, "video_id": vid, "channel_id": ch["channel_id"], "title": sn.get("title"),
                             "description": sn.get("description", ""), "tags": sn.get("tags", []), "published_at": sn.get("publishedAt"),
                             "duration": it.get("contentDetails", {}).get("duration"),
                             "views": int(st["viewCount"]) if "viewCount" in st else None,
                             "likes": int(st["likeCount"]) if "likeCount" in st else None,
                             "comments": int(st["commentCount"]) if "commentCount" in st else None}, "api", obs)
            c.execute("update collection_status set metadata_synced_at=? where video_id=?", (obs, vid))
            c.execute("delete from unresolved where kind='video' and entity_id=?", (vid,))
            refreshed += 1
            if vid not in local: new_count += 1
        for vid in set(batch) - seen: mark_unresolved(c, "video", vid, "not returned by videos.list (deleted/private)")
        c.executemany("delete from pending_videos where video_id=?", [(vid,) for vid in batch])
        c.commit()
    return {"pages": pages, "new_videos": new_count, "discovered_videos": len(pending - local), "stats_refreshed": refreshed,
            "videos_unavailable": len(targets) - refreshed, "thumbnails_checked": images, "thumbnail_errors": image_errors}

def sync_comments(c, yt, ch, max_age_days, limit):
    rows = c.execute("""select v.video_id from videos v left join collection_status cs using(video_id)
      where channel_id=? and (coalesce(v.comments,0)>0 or cs.comments_complete_at is not null)
      and (cs.comments_complete_at is null or julianday(cs.comments_complete_at) <= julianday(?))
      and v.video_id not in (select entity_id from unresolved where kind='comments' and julianday(ttl_until)>julianday(?))
      order by cs.comments_complete_at is not null, v.published_at desc limit ?""",
      (ch["channel_id"], days_ago(max_age_days), now(), limit)).fetchall()
    n = complete = errors = 0
    for (vid,) in rows:
        key = f"comments:{vid}:cursor"; cur = cache_get(c, key) or {}
        token = cur.get("token") if cur.get("state") == "pending" else None
        if not token: c.execute("delete from comment_scan_items where video_id=?", (vid,))
        c.execute("insert or ignore into collection_status(video_id) values(?)", (vid,))
        # A partial refresh must never imply that an absent reply is verified absent.
        c.execute("update collection_status set comments_error='scan in progress' where video_id=?", (vid,)); c.commit()
        def save(cm, parent=None):
            nonlocal n
            sn = cm["snippet"]
            cid = upsert_comment(c, vid, {"id": cm["id"], "author": sn.get("authorDisplayName"), "text": sn.get("textDisplay"),
                                         "likes": sn.get("likeCount"), "published_at": sn.get("publishedAt")}, parent)
            c.execute("insert or ignore into comment_scan_items values(?,?)", (vid, cid)); n += 1
        try:
            while True:
                p = {"part": "snippet,replies", "videoId": vid, "maxResults": 100, "order": "time", "textFormat": "plainText"}
                if token: p["pageToken"] = token
                try: d = yt.get("commentThreads", **p)
                except ApiError as e:
                    if token and e.code == 400 and "invalidPageToken" in e.body:
                        token = None; c.execute("delete from comment_scan_items where video_id=?", (vid,)); continue
                    raise
                for th in d.get("items", []):
                    top = th["snippet"]["topLevelComment"]; save(top)
                    replies = th.get("replies", {}).get("comments", [])
                    if th["snippet"].get("totalReplyCount", 0) > len(replies):
                        reply_token = None
                        while True:
                            rp = {"part": "snippet", "parentId": top["id"], "maxResults": 100, "textFormat": "plainText"}
                            if reply_token: rp["pageToken"] = reply_token
                            result = yt.get("comments", **rp)
                            for cm in result.get("items", []): save(cm, top["id"])
                            c.commit()
                            reply_token = result.get("nextPageToken")
                            if not reply_token: break
                    else:
                        for cm in replies: save(cm, top["id"])
                token = d.get("nextPageToken")
                if not token: break
                cache_set(c, key, {"state": "pending", "token": token}); c.commit()
            # Prune removed comments only after a complete scan of all pages and replies.
            c.execute("""delete from comments_fts where comment_id in (select comment_id from comments
              where video_id=? and comment_id not in (select comment_id from comment_scan_items where video_id=?))""", (vid, vid))
            c.execute("delete from comments where video_id=? and comment_id not in (select comment_id from comment_scan_items where video_id=?)", (vid, vid))
            c.execute("delete from comment_scan_items where video_id=?", (vid,))
            cache_set(c, key, {"state": "committed"})
            c.execute("update videos set comments_synced_at=? where video_id=?", (now(), vid))
            c.execute("update collection_status set comments_complete_at=?,comments_error=null where video_id=?", (now(), vid))
            c.execute("delete from unresolved where kind='comments' and entity_id=?", (vid,)); complete += 1
        except ApiError as e:
            c.execute("update collection_status set comments_error=? where video_id=?", (e.body, vid))
            if e.code in (403, 404): mark_unresolved(c, "comments", vid, e.body); errors += 1
            else: c.commit(); raise
        c.commit()
    return {"videos_scanned": len(rows), "comments_completed": complete, "comments_upserted": n, "comment_errors": errors}

def sync_transcripts(c, ch, limit, langs):
    try: from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError: raise ValueError("install youtube-transcript-api to collect captions") from None
    rows = c.execute("""select video_id from videos where channel_id=? and transcript_synced_at is null
      and video_id not in (select entity_id from unresolved where kind='transcript' and julianday(ttl_until)>julianday(?))
      order by published_at desc limit ?""", (ch["channel_id"], now(), limit)).fetchall()
    ytt = YouTubeTranscriptApi(); ok = tried = unavailable = 0
    permanent = {"TranscriptsDisabled", "NoTranscriptFound", "VideoUnavailable", "VideoUnplayable", "AgeRestricted"}
    for (vid,) in rows:
        tried += 1
        try: tr = ytt.fetch(vid, languages=langs)
        except Exception as e:
            name = type(e).__name__
            if name in permanent:
                mark_unresolved(c, "transcript", vid, name, ttl_days=30); unavailable += 1; c.commit(); continue
            c.commit()
            return {"videos_tried": tried, "transcripts_saved": ok, "transcripts_unavailable": unavailable,
                    "stopped": f"{name}: caption collection stopped; progress saved. Retry later."}
        segs = [{"text": item.text, "start": item.start, "duration": item.duration} for item in tr]
        replace_transcript(c, vid, segs); ok += 1
        c.execute("delete from unresolved where kind='transcript' and entity_id=?", (vid,)); c.commit()
    return {"videos_tried": tried, "transcripts_saved": ok, "transcripts_unavailable": unavailable}

# ---------- optional: bring your own YAML dump ----------
def import_yaml_dir(c, d):
    import yaml
    if not Path(d).is_dir(): raise ValueError("YAML import directory not found")
    files = sorted(p for p in Path(d).glob("*.yaml") if not p.name.startswith("_"))
    done = skipped = 0
    for p in files:
        st = p.stat(); sig = f"{st.st_mtime_ns}:{st.st_size}"; key = f"file:{p}"
        if (cache_get(c, key) or {}).get("sig") == sig: skipped += 1; continue
        v = yaml.safe_load(p.read_text()) or {}
        if not v.get("video_id"): continue
        s = v.get("stats") or {}
        upsert_video(c, {**v, "views": s.get("views"), "likes": s.get("likes"), "comments": s.get("comments")}, "yaml", S(v.get("pulled_at")))
        if v.get("transcript"): replace_transcript(c, v["video_id"], v["transcript"])
        for cm in v.get("comments") or []:
            cid = upsert_comment(c, v["video_id"], cm)
            for rp in cm.get("replies") or []: upsert_comment(c, v["video_id"], rp, parent=cid)
        cache_set(c, key, {"sig": sig}); c.commit(); done += 1
    c.commit(); return {"yaml_files": len(files), "yaml_imported": done, "yaml_unchanged": skipped}

# ---------- free local reads ----------
def q(term): return " ".join(f'"{w}"' for w in term.replace('"', " ").split()) or '""'

def search(c, term, scope='all', n=20, channel=None, since=None, until=None):
    from ytclaw_reports import search as read_search
    return read_search(c, term, scope, n, channel, since, until)

def video(c, vid):
    v = c.execute("select * from videos where video_id=?", (vid,)).fetchone()
    if not v: return None
    d = dict(v); d["tags"] = json.loads(d.pop("tags_json") or "[]")
    d["metadata_history"] = metadata_history(c, vid)
    d["stats_history"] = [dict(r) for r in c.execute("select * from stats_observations where video_id=? order by julianday(observed_at),observation_id", (vid,))]
    d["collection"] = dict(c.execute("select * from collection_status where video_id=?", (vid,)).fetchone() or {})
    d["transcript_segments"] = c.execute("select count(*) from transcript_segments where video_id=?", (vid,)).fetchone()[0]
    d["comment_count_local"] = c.execute("select count(*) from comments where video_id=?", (vid,)).fetchone()[0]
    return d

def top(c, by='views', n=20, channel=None, since=None, until=None):
    from ytclaw_reports import top as read_top
    return read_top(c, by, n, channel, since, until)

def stats(c, db):
    t = lambda s: c.execute(s).fetchone()[0]
    return {"db": str(db), "size_mb": round(Path(db).stat().st_size / 1e6, 2),
            "channels": [dict(r) for r in c.execute("select handle,title,channel_id from channels")],
            "videos": t("select count(*) from videos where title is not null"),
            "videos_with_transcript": t("select count(distinct video_id) from transcript_segments"),
            "transcript_segments": t("select count(*) from transcript_segments"),
            "comments": t("select count(*) from comments"),
            "stats_snapshots": t("select count(*) from stats_snapshots"),
            "unresolved": t("select count(*) from unresolved"),
            "latest_channel": dict(c.execute("select * from channel_snapshots order by observed_at desc limit 1").fetchone() or {}),
            "quota_today": cache_get(c, f"quota:{today()}") or {"units": 0, "calls": 0}, "quota_daily_limit": 10000}

def readonly_sql(c, query):
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
    def authorize(action, first, second, database, trigger):
        if action == sqlite3.SQLITE_PRAGMA and first == 'data_version' and second is None:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION and (second or '').lower() == 'load_extension':
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY
    c.execute('pragma query_only=on')
    c.set_authorizer(authorize)
    try: return [dict(r) for r in c.execute(query)]
    finally:
        # Python 3.10 cannot reliably disable the authorizer with None.
        c.set_authorizer(lambda *args: sqlite3.SQLITE_OK)
        c.execute('pragma query_only=off')

# ---------- cli ----------
def positive(value):
    result = int(value)
    if result <= 0: raise argparse.ArgumentTypeError('must be positive')
    return result


def main(argv=None):
    try: return cli(argv)
    except (ValueError, OSError, sqlite3.Error) as e:
        print(f'ytclaw: {e}', file=sys.stderr); return 1
    except KeyboardInterrupt:
        print('ytclaw: interrupted; completed checkpoints are saved', file=sys.stderr); return 130


def cli(argv=None):
    import ytclaw_ops as ops
    import ytclaw_reports as reports
    ap = argparse.ArgumentParser(prog='ytclaw', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', default=str(DEFAULT_DB)); ap.add_argument('--json', action='store_true')
    sp = ap.add_subparsers(dest='cmd', required=True)
    def sync_options(parser):
        for flag in ('comments', 'transcripts', 'thumbnails', 'full'): parser.add_argument('--' + flag, action='store_true')
        parser.add_argument('--limit', type=positive, default=200)
        parser.add_argument('--comment-max-age', type=int, default=7)
        parser.add_argument('--langs', default='en,en-US,en-GB')
    def selection(parser, dates=True):
        parser.add_argument('--channel', help='local @handle or channel ID')
        if dates:
            parser.add_argument('--since', type=reports.date_arg); parser.add_argument('--until', type=reports.end_date_arg)
        parser.add_argument('-n', type=positive, default=20)
    sy = sp.add_parser('sync', help='refresh YouTube data with durable checkpoints')
    sy.add_argument('handle', nargs='?'); sync_options(sy)
    i = sp.add_parser('init', help='configure a default channel and API key'); i.add_argument('--channel'); i.add_argument('--non-interactive', action='store_true')
    sp.add_parser('doctor', help='check local setup and collection coverage')
    sp.add_parser('demo', help='create a synthetic sample in a NEW --db path')
    i = sp.add_parser('import'); i.add_argument('--yaml', required=True)
    s = sp.add_parser('search'); s.add_argument('query'); selection(s)
    s.add_argument('--in', dest='scope', default='all', choices=['all', 'videos', 'transcripts', 'comments'])
    v = sp.add_parser('video'); v.add_argument('video_id')
    for name in ('history', 'baseline', 'drift'):
        parser = sp.add_parser(name); parser.add_argument('video_id')
    ex = sp.add_parser('thumbnail', help='export archived thumbnail by SHA-256'); ex.add_argument('sha256'); ex.add_argument('output')
    tp = sp.add_parser('top'); tp.add_argument('--by', default='views', choices=['views','likes','comments']); selection(tp)
    for name in ('changes', 'questions', 'growth', 'report'):
        parser = sp.add_parser(name); selection(parser)
        if name == 'questions': parser.add_argument('--include-unverified', action='store_true')
        if name == 'report': parser.add_argument('--format', choices=['json','markdown'], default='markdown')
    selection(sp.add_parser('runs'), dates=False)
    sp.add_parser('stats'); sq = sp.add_parser('sql', help='SQLite-enforced read-only query'); sq.add_argument('query')
    for name in ('backup', 'export'):
        parser = sp.add_parser(name); parser.add_argument('output')
    rs = sp.add_parser('restore', help='restore to a NEW --db path'); rs.add_argument('source')
    sv = sp.add_parser('serve', help='open the local reading desk'); sv.add_argument('--port', type=int, default=8765); sv.add_argument('--no-browser', action='store_true')
    project = sp.add_parser('project'); pp = project.add_subparsers(dest='action', required=True)
    pl = pp.add_parser('link'); pl.add_argument('video_id'); pl.add_argument('manifest')
    pd = pp.add_parser('drift'); pd.add_argument('video_id')
    pp.add_parser('list')
    pu = pp.add_parser('unlink'); pu.add_argument('video_id')
    exp = sp.add_parser('experiment'); ep = exp.add_subparsers(dest='action', required=True)
    el = ep.add_parser('list'); el.add_argument('video_id')
    en = ep.add_parser('add'); en.add_argument('video_id'); en.add_argument('--note', required=True); en.add_argument('--result', default='')
    en.add_argument('--started-at'); en.add_argument('--ended-at'); en.add_argument('--source-url')
    watch = sp.add_parser('watch'); wp = watch.add_subparsers(dest='action', required=True)
    wa = wp.add_parser('add'); wa.add_argument('handle'); wa.add_argument('--every-hours', type=float, default=24); sync_options(wa)
    wp.add_parser('list'); wr = wp.add_parser('remove'); wr.add_argument('handle')
    wr = wp.add_parser('run'); wr.add_argument('--once', action='store_true')
    wp.add_parser('install'); wp.add_parser('uninstall')
    auth = sp.add_parser('auth'); au = auth.add_subparsers(dest='action', required=True)
    al = au.add_parser('login'); al.add_argument('--client-secret', required=True)
    au.add_parser('status'); au.add_parser('logout')
    analytics = sp.add_parser('analytics'); an = analytics.add_subparsers(dest='action', required=True)
    ay = an.add_parser('sync'); ay.add_argument('--channel', required=True); ay.add_argument('--since', required=True, type=reports.date_arg); ay.add_argument('--until', required=True, type=reports.date_arg)
    ay.add_argument('--video'); ay.add_argument('--kind', choices=['all','daily','retention','traffic'], default='all'); ay.add_argument('--limit', type=positive, default=50); ay.add_argument('--refresh', action='store_true')
    ai = an.add_parser('import'); ai.add_argument('video_id'); ai.add_argument('--csv', required=True)
    ash = an.add_parser('show'); ash.add_argument('video_id')
    ac = an.add_parser('compare'); ac.add_argument('video_id'); ac.add_argument('--version', required=True, type=int); ac.add_argument('--days', type=positive, default=7)
    sk = sp.add_parser('skill', help='print or install the bundled agent skill'); sk.add_argument('action', nargs='?', choices=['install'])
    sk.add_argument('--dir', default=str(Path.home() / '.claude' / 'skills'))
    a = ap.parse_args(argv)
    def emit(out): print(json.dumps(out, indent=2, default=str))
    if a.cmd == 'skill':
        src = Path(__file__).resolve().parent / 'skills' / 'ytclaw' / 'SKILL.md'
        if not src.exists(): raise ValueError(f'bundled skill not found at {src}')
        if a.action != 'install': print(src.read_text()); return 0
        dst = Path(a.dir) / 'ytclaw'; dst.mkdir(parents=True, exist_ok=True)
        (dst / 'SKILL.md').write_text(src.read_text()); print(f"installed {dst / 'SKILL.md'}"); return 0
    if a.cmd == 'init': emit(ops.initialize(a.channel, not a.non_interactive)); return 0
    if a.cmd == 'restore': emit(ops.restore(a.source, a.db)); return 0
    if a.cmd == 'demo':
        from ytclaw_demo import create
        emit(create(a.db)); return 0
    if a.cmd == 'auth':
        import ytclaw_analytics as owner
        if a.action == 'login': emit(owner.auth_login(a.client_secret))
        elif a.action == 'status': emit(owner.auth_status())
        else:
            (HOME / 'owner-token.json').unlink(missing_ok=True); emit({'local_token_removed': True, 'note': 'Remove Google account app access separately to revoke consent.'})
        return 0
    c = connect(a.db)
    try:
        if a.cmd == 'sync':
            if a.comment_max_age < 0: raise ValueError('--comment-max-age cannot be negative')
            out, code = ops.run_sync(c, a.db, a.handle or ops.default_channel(), **{k: getattr(a,k) for k in ('comments','transcripts','thumbnails','full','limit','comment_max_age','langs')})
            emit(out); return code
        elif a.cmd == 'import': out = import_yaml_dir(c, a.yaml)
        elif a.cmd == 'doctor': out = ops.doctor(c, a.db)
        elif a.cmd == 'search': out = reports.search(c, a.query, a.scope, a.n, a.channel, a.since, a.until)
        elif a.cmd == 'video':
            out = video(c, a.video_id)
            if out is None: raise ValueError('video not local')
        elif a.cmd == 'history': out = metadata_history(c, a.video_id)
        elif a.cmd == 'baseline': out = baseline(c, a.video_id)
        elif a.cmd == 'drift': out = drift(c, a.video_id)
        elif a.cmd == 'thumbnail':
            row = c.execute('select data from thumbnail_assets where sha256=?', (a.sha256,)).fetchone()
            if not row: raise ValueError('thumbnail not found')
            with open(a.output, 'xb') as f: f.write(row[0])
            out = {'output': a.output, 'sha256': a.sha256}
        elif a.cmd == 'top': out = reports.top(c, a.by, a.n, a.channel, a.since, a.until)
        elif a.cmd in ('changes','questions','growth','report'):
            options = {'include_unverified': a.include_unverified} if a.cmd == 'questions' else {}
            out = getattr(reports, a.cmd)(c, a.channel, a.since, a.until, a.n, **options)
            if a.cmd == 'report' and a.format == 'markdown' and not a.json:
                print(reports.markdown_report(out), end=''); return 0
        elif a.cmd == 'runs': out = ops.runs(c, a.channel, a.n)
        elif a.cmd == 'stats':
            out = stats(c, a.db); out['coverage'] = reports.coverage(c)
        elif a.cmd == 'sql': out = readonly_sql(c, a.query)
        elif a.cmd == 'backup': out = ops.backup(c, a.output)
        elif a.cmd == 'export': out = ops.export_archive(c, a.output)
        elif a.cmd == 'serve':
            from ytclaw_web import serve
            c.close(); serve(a.db, a.port, not a.no_browser); return 0
        elif a.cmd == 'project':
            if a.action == 'link': out = ops.link_project(c, a.video_id, a.manifest)
            elif a.action == 'drift': out = ops.project_drift(c, a.video_id)
            elif a.action == 'list': out = [dict(r) for r in c.execute('select * from project_links')]
            else:
                c.execute('delete from project_links where video_id=?', (a.video_id,)); c.commit(); out = {'unlinked': a.video_id}
        elif a.cmd == 'experiment':
            if a.action == 'list': out = [dict(r) for r in c.execute('select * from experiments where video_id=? order by experiment_id', (a.video_id,))]
            else: out = ops.record_experiment(c, a.video_id, a.note, a.result, a.started_at, a.ended_at, a.source_url)
        elif a.cmd == 'watch':
            if a.action == 'add': out = ops.watch_add(c, a.handle, a.every_hours, **{k:getattr(a,k) for k in ('comments','transcripts','thumbnails','full','limit','comment_max_age','langs')})
            elif a.action == 'list': out = ops.watch_jobs(c)
            elif a.action == 'remove':
                c.execute('delete from watch_jobs where handle=?', (a.handle,)); c.commit(); out = {'removed': a.handle}
            elif a.action == 'run': ops.watch_run(c, a.db, a.once); return 0
            else: out = ops.schedule(a.db, 'remove' if a.action == 'uninstall' else 'install')
        elif a.cmd == 'analytics':
            import ytclaw_analytics as owner
            if a.action == 'sync':
                with ops.sync_lock(a.db): out = owner.sync(c, a.channel, a.since, a.until, a.video, a.kind, a.limit, a.refresh)
            elif a.action == 'import': out = owner.import_csv(c, a.video_id, a.csv)
            elif a.action == 'show': out = owner.show(c, a.video_id)
            else: out = owner.compare(c, a.video_id, a.version, a.days)
        if a.cmd == 'search' and not a.json:
            for r in out:
                print(f"[{r['kind'][0].upper()}] {r['url']}  {r['title'] or ''}\n    {r['snip']}")
                if r['kind'] == 'comment': print(f"    {r['author']} ({r['likes']} likes)")
        elif a.cmd == 'top' and not a.json:
            for r in out: print(f"{r['video_id']}  {str(r[a.by]):>8}  {(r['published_at'] or '')[:10]:10}  {r['title']}")
        else: emit(out)
        return 0
    finally: c.close()

if __name__ == '__main__': sys.exit(main())
