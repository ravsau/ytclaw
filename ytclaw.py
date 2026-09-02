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
from pathlib import Path

HOME = Path(os.environ.get("YTCLAW_HOME", Path.home() / ".ytclaw"))
DEFAULT_DB = Path(os.environ.get("YTCLAW_DB", HOME / "ytclaw.sqlite"))
API = "https://www.googleapis.com/youtube/v3/"
QUOTA = {"channels": 1, "playlistItems": 1, "videos": 1, "commentThreads": 1, "search": 100}

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
create table if not exists channel_snapshots(
  observed_at text primary key, channel_id text, subscribers integer, total_views integer,
  video_count integer, source text);
create table if not exists unresolved(kind text, entity_id text, reason text,
  last_attempted_at text, ttl_until text, primary key(kind, entity_id));
create table if not exists sync_cache(cache_key text primary key, value_json text, updated_at text);
create virtual table if not exists videos_fts using fts5(video_id unindexed, title, description);
create virtual table if not exists transcript_fts using fts5(video_id unindexed, idx unindexed, text);
create virtual table if not exists comments_fts using fts5(comment_id unindexed, text);
"""

def S(x): return None if x is None else str(x)
def now(): return time.strftime("%Y-%m-%dT%H:%M:%S")
def today(): return time.strftime("%Y-%m-%d")
def h(*parts): return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]

def connect(db):
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(db); c.row_factory = sqlite3.Row
    c.executescript(SCHEMA); return c

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
        try:
            with urllib.request.urlopen(url, timeout=30) as r: data = json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            if e.code == 403 and ("quotaExceeded" in body or "rateLimitExceeded" in body):
                raise QuotaExhausted(f"YouTube quota/rate limit hit on {resource}. Progress is saved; rerun later to continue.")
            raise ApiError(e.code, resource, body)
        self.used += QUOTA.get(resource, 1)
        q = cache_get(self.c, f"quota:{today()}") or {"units": 0, "calls": 0}
        q["units"] += QUOTA.get(resource, 1); q["calls"] += 1
        cache_set(self.c, f"quota:{today()}", q)
        return data

def api_key():
    k = os.environ.get("YOUTUBE_API_KEY")
    if not k and (HOME / "config.json").exists(): k = json.loads((HOME / "config.json").read_text()).get("api_key")
    if not k: raise SystemExit("no API key: set YOUTUBE_API_KEY or put {\"api_key\": ...} in ~/.ytclaw/config.json\n"
                               "get one free at https://console.cloud.google.com/apis/credentials (enable YouTube Data API v3)")
    return k

# ---------- upserts ----------
def upsert_video(c, v, source, observed_at=None):
    t = now()
    c.execute("""insert into videos(video_id,channel_id,title,description,tags_json,published_at,duration,url,
      views,likes,comments,first_seen_at,last_seen_at,seen_count) values(?,?,?,?,?,?,?,?,?,?,?,?,?,1)
      on conflict(video_id) do update set
      channel_id=coalesce(excluded.channel_id,channel_id), title=coalesce(excluded.title,title),
      description=coalesce(excluded.description,description), tags_json=coalesce(excluded.tags_json,tags_json),
      published_at=coalesce(excluded.published_at,published_at), duration=coalesce(excluded.duration,duration),
      views=coalesce(excluded.views,views), likes=coalesce(excluded.likes,likes), comments=coalesce(excluded.comments,comments),
      last_seen_at=excluded.last_seen_at, seen_count=seen_count+1""",
      (v["video_id"], v.get("channel_id"), v.get("title"), v.get("description"), json.dumps(v.get("tags") or []),
       S(v.get("published_at")), v.get("duration"), f"https://www.youtube.com/watch?v={v['video_id']}",
       v.get("views"), v.get("likes"), v.get("comments"), t, t))
    if v.get("title") is not None:
        c.execute("delete from videos_fts where video_id=?", (v["video_id"],))
        c.execute("insert into videos_fts values(?,?,?)", (v["video_id"], v.get("title") or "", v.get("description") or ""))
    if v.get("views") is not None:
        snap_stats(c, v["video_id"], observed_at or t, v.get("views"), v.get("likes"), v.get("comments"), source)

def snap_stats(c, vid, observed_at, views, likes, comments, source):
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
    ttl = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() + ttl_days * 86400))
    c.execute("insert or replace into unresolved values(?,?,?,?,?)", (kind, eid, reason[:200], now(), ttl))

# ---------- sync from YouTube ----------
def sync_channel(c, yt, handle):
    key = "channel:" + handle.lower()
    ch = cache_get(c, key)
    if not ch:
        p = {"forHandle": handle} if handle.startswith("@") else {"id": handle}
        d = yt.get("channels", part="snippet,contentDetails,statistics", **p)
        if not d.get("items"): raise SystemExit(f"channel not found: {handle}")
        it = d["items"][0]
        ch = {"channel_id": it["id"], "title": it["snippet"]["title"], "uploads": it["contentDetails"]["relatedPlaylists"]["uploads"]}
        cache_set(c, key, ch)
        c.execute("insert or replace into channels values(?,?,?,?,?,?)", (ch["channel_id"], handle, ch["title"], ch["uploads"], now(), now()))
        st = it.get("statistics", {})
        c.execute("insert or replace into channel_snapshots values(?,?,?,?,?,?)",
                  (now(), ch["channel_id"], int(st.get("subscriberCount", 0)), int(st.get("viewCount", 0)), int(st.get("videoCount", 0)), "api"))
    else:
        d = yt.get("channels", part="statistics", id=ch["channel_id"])
        st = d["items"][0].get("statistics", {})
        c.execute("insert or replace into channel_snapshots values(?,?,?,?,?,?)",
                  (now(), ch["channel_id"], int(st.get("subscriberCount", 0)), int(st.get("viewCount", 0)), int(st.get("videoCount", 0)), "api"))
        c.execute("update channels set last_seen_at=? where channel_id=?", (now(), ch["channel_id"]))
    return ch

def sync_videos(c, yt, ch, full):
    """Walk the uploads playlist newest-first. Early-stop when a whole page is already local
    (unless --full). Then refresh stats for every local video, 50 per unit."""
    local = {r[0] for r in c.execute("select video_id from videos where channel_id=?", (ch["channel_id"],))}
    cur_key = f"uploads:{ch['channel_id']}:cursor"; cur = cache_get(c, cur_key) or {}
    token = cur.get("token") if cur.get("state") == "pending" else None
    new_ids, pages = [], 0
    while True:
        p = {"part": "contentDetails", "playlistId": ch["uploads"], "maxResults": 50}
        if token: p["pageToken"] = token
        d = yt.get("playlistItems", **p); pages += 1
        ids = [i["contentDetails"]["videoId"] for i in d.get("items", [])]
        fresh = [i for i in ids if i not in local]
        new_ids += fresh
        token = d.get("nextPageToken")
        if not token or (not full and not fresh and local): break
        cache_set(c, cur_key, {"state": "pending", "token": token})
    cache_set(c, cur_key, {"state": "committed", "last_run": now(), "pages": pages})
    targets = sorted(local | set(new_ids))
    obs = now()
    for i in range(0, len(targets), 50):
        d = yt.get("videos", part="snippet,contentDetails,statistics", id=",".join(targets[i:i+50]), maxResults=50)
        seen = set()
        for it in d.get("items", []):
            sn, st = it["snippet"], it.get("statistics", {})
            seen.add(it["id"])
            upsert_video(c, {"video_id": it["id"], "channel_id": ch["channel_id"], "title": sn.get("title"),
                             "description": sn.get("description"), "tags": sn.get("tags"), "published_at": sn.get("publishedAt"),
                             "duration": it.get("contentDetails", {}).get("duration"),
                             "views": int(st["viewCount"]) if "viewCount" in st else None,
                             "likes": int(st["likeCount"]) if "likeCount" in st else None,
                             "comments": int(st["commentCount"]) if "commentCount" in st else None}, "api", obs)
        for vid in set(targets[i:i+50]) - seen: mark_unresolved(c, "video", vid, "not returned by videos.list (deleted/private)")
    c.commit()
    return {"pages": pages, "new_videos": len(new_ids), "stats_refreshed": len(targets)}

def sync_comments(c, yt, ch, max_age_days, limit):
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - max_age_days * 86400))
    rows = c.execute("""select video_id from videos where channel_id=? and coalesce(comments,0)>0
      and (comments_synced_at is null or comments_synced_at < ?)
      and video_id not in (select entity_id from unresolved where kind='comments' and ttl_until > ?)
      order by published_at desc limit ?""", (ch["channel_id"], cutoff, now(), limit)).fetchall()
    n = 0
    for (vid,) in rows:
        token = None
        try:
            while True:
                p = {"part": "snippet,replies", "videoId": vid, "maxResults": 100, "order": "time", "textFormat": "plainText"}
                if token: p["pageToken"] = token
                d = yt.get("commentThreads", **p)
                for th in d.get("items", []):
                    top = th["snippet"]["topLevelComment"]; s = top["snippet"]
                    cid = upsert_comment(c, vid, {"id": top["id"], "author": s.get("authorDisplayName"), "text": s.get("textDisplay"),
                                                  "likes": s.get("likeCount"), "published_at": s.get("publishedAt")}); n += 1
                    for rp in th.get("replies", {}).get("comments", []):
                        rs = rp["snippet"]
                        upsert_comment(c, vid, {"id": rp["id"], "author": rs.get("authorDisplayName"), "text": rs.get("textDisplay"),
                                                "likes": rs.get("likeCount"), "published_at": rs.get("publishedAt")}, parent=cid); n += 1
                token = d.get("nextPageToken")
                if not token: break
            c.execute("update videos set comments_synced_at=? where video_id=?", (now(), vid))
        except ApiError as e:
            if e.code in (403, 404): mark_unresolved(c, "comments", vid, e.body); c.commit(); continue
            raise
        c.commit()
    return {"videos_scanned": len(rows), "comments_upserted": n}

def sync_transcripts(c, ch, limit, langs):
    try: from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError: raise SystemExit("pip install youtube-transcript-api")
    rows = c.execute("""select video_id from videos where channel_id=? and transcript_synced_at is null
      and video_id not in (select entity_id from unresolved where kind='transcript' and ttl_until > ?)
      order by published_at desc limit ?""", (ch["channel_id"], now(), limit)).fetchall()
    ytt = YouTubeTranscriptApi(); ok = 0
    for (vid,) in rows:
        try:
            tr = ytt.fetch(vid, languages=langs)
            segs = [{"text": s.text, "start": s.start, "duration": s.duration} for s in tr]
            replace_transcript(c, vid, segs); ok += 1
        except Exception as e:
            name = type(e).__name__
            if any(k in name for k in ("Block", "TooMany", "RateLimit", "Timeout")):
                c.commit(); return {"videos_tried": len(rows), "transcripts_saved": ok, "stopped": f"{name}: transcript source is rate limiting; progress saved, rerun later"}
            mark_unresolved(c, "transcript", vid, name, ttl_days=90)
        c.commit()
    return {"videos_tried": len(rows), "transcripts_saved": ok}

# ---------- optional: bring your own YAML dump ----------
def import_yaml_dir(c, d):
    import yaml
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
        cache_set(c, key, {"sig": sig}); done += 1
    c.commit(); return {"yaml_files": len(files), "yaml_imported": done, "yaml_unchanged": skipped}

# ---------- free local reads ----------
def q(term): return " ".join(f'"{w}"' for w in term.replace('"', " ").split()) or '""'

def search(c, term, scope, n):
    out = []
    if scope in ("videos", "all"):
        for r in c.execute("""select v.video_id, v.title, v.published_at, v.views, snippet(videos_fts,2,'[',']','...',12) snip
          from videos_fts join videos v using(video_id) where videos_fts match ? order by rank limit ?""", (q(term), n)):
            out.append({"kind": "video", **dict(r)})
    if scope in ("transcripts", "all"):
        for r in c.execute("""select t.video_id, v.title, t.idx, s.start, snippet(transcript_fts,2,'[',']','...',14) snip
          from transcript_fts t join transcript_segments s on s.video_id=t.video_id and s.idx=t.idx
          join videos v on v.video_id=t.video_id where transcript_fts match ? order by rank limit ?""", (q(term), n)):
            d = dict(r); d["url"] = f"https://www.youtube.com/watch?v={d['video_id']}&t={int(d['start'] or 0)}s"; out.append({"kind": "transcript", **d})
    if scope in ("comments", "all"):
        for r in c.execute("""select cm.video_id, v.title, cm.author, cm.likes, cm.published_at, snippet(comments_fts,1,'[',']','...',14) snip
          from comments_fts f join comments cm on cm.comment_id=f.comment_id join videos v on v.video_id=cm.video_id
          where comments_fts match ? order by rank limit ?""", (q(term), n)):
            out.append({"kind": "comment", **dict(r)})
    return out

def video(c, vid):
    v = c.execute("select * from videos where video_id=?", (vid,)).fetchone()
    if not v: return None
    d = dict(v); d["tags"] = json.loads(d.pop("tags_json") or "[]")
    d["stats_history"] = [dict(r) for r in c.execute("select observed_at,last_seen_at,views,likes,comments,source from stats_snapshots where video_id=? order by observed_at", (vid,))]
    d["transcript_segments"] = c.execute("select count(*) from transcript_segments where video_id=?", (vid,)).fetchone()[0]
    d["comment_count_local"] = c.execute("select count(*) from comments where video_id=?", (vid,)).fetchone()[0]
    return d

def top(c, by, n):
    assert by in ("views", "likes", "comments")
    return [dict(r) for r in c.execute(f"select video_id,title,published_at,views,likes,comments from videos where title is not null order by {by} desc limit ?", (n,))]

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

# ---------- cli ----------
def main(argv=None):
    ap = argparse.ArgumentParser(prog="ytclaw", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(DEFAULT_DB)); ap.add_argument("--json", action="store_true")
    sp = ap.add_subparsers(dest="cmd", required=True)
    sy = sp.add_parser("sync", help="fetch from YouTube (metered)"); sy.add_argument("handle", help="@handle or channel id")
    sy.add_argument("--comments", action="store_true"); sy.add_argument("--transcripts", action="store_true")
    sy.add_argument("--full", action="store_true", help="walk the whole uploads playlist, no early stop")
    sy.add_argument("--limit", type=int, default=200, help="max videos per run for comments/transcripts")
    sy.add_argument("--comment-max-age", type=int, default=7, help="days before a video's comments are refetched")
    sy.add_argument("--langs", default="en,en-US,en-GB")
    i = sp.add_parser("import"); i.add_argument("--yaml", required=True)
    s = sp.add_parser("search"); s.add_argument("query"); s.add_argument("--in", dest="scope", default="all", choices=["all", "videos", "transcripts", "comments"]); s.add_argument("-n", type=int, default=20)
    v = sp.add_parser("video"); v.add_argument("video_id")
    tp = sp.add_parser("top"); tp.add_argument("--by", default="views"); tp.add_argument("-n", type=int, default=20)
    sp.add_parser("stats"); sq = sp.add_parser("sql"); sq.add_argument("query")
    sk = sp.add_parser("skill", help="print or install the bundled agent skill"); sk.add_argument("action", nargs="?", choices=["install"])
    sk.add_argument("--dir", default=str(Path.home() / ".claude" / "skills"), help="skills directory (default ~/.claude/skills)")
    a = ap.parse_args(argv)
    if a.cmd == "skill":
        src = Path(__file__).resolve().parent / "skills" / "ytclaw" / "SKILL.md"
        if not src.exists(): sys.exit(f"bundled skill not found at {src}")
        if a.action != "install": print(src.read_text()); return
        dst = Path(a.dir) / "ytclaw"; dst.mkdir(parents=True, exist_ok=True)
        (dst / "SKILL.md").write_text(src.read_text()); print(f"installed {dst / 'SKILL.md'}"); return
    c = connect(a.db)

    if a.cmd == "sync":
        yt = YT(c, api_key()); out = {}
        try:
            ch = sync_channel(c, yt, a.handle)
            out = {"channel": ch["title"], **sync_videos(c, yt, ch, a.full)}
            if a.comments: out.update(sync_comments(c, yt, ch, a.comment_max_age, a.limit))
            if a.transcripts: out.update(sync_transcripts(c, ch, a.limit, a.langs.split(",")))
        except QuotaExhausted as e:
            c.commit(); out["stopped"] = str(e); out["quota_units_this_run"] = yt.used
            print(json.dumps(out, indent=2)); sys.exit(75)
        except ApiError as e:
            c.commit(); sys.exit(str(e))
        out["quota_units_this_run"] = yt.used; out["quota_units_today"] = (cache_get(c, f"quota:{today()}") or {}).get("units", 0)
        c.commit()
    elif a.cmd == "import": out = import_yaml_dir(c, a.yaml)
    elif a.cmd == "search": out = search(c, a.query, a.scope, a.n)
    elif a.cmd == "video": out = video(c, a.video_id)
    elif a.cmd == "top": out = top(c, a.by, a.n)
    elif a.cmd == "stats": out = stats(c, a.db)
    elif a.cmd == "sql":
        if not a.query.lstrip().lower().startswith(("select", "with", "explain")): ap.error("sql: read-only, start with SELECT")
        out = [dict(r) for r in c.execute(a.query)]

    if a.json or a.cmd in ("sync", "import", "stats", "video"): print(json.dumps(out, indent=2, default=str)); return
    if a.cmd == "search":
        for r in out:
            if r["kind"] == "transcript": print(f"[T] {r['url']}  {r['title'][:50]}\n    {r['snip']}")
            elif r["kind"] == "comment": print(f"[C] {r['video_id']}  {r['author']} ({r['likes']} likes) on {r['title'][:40]}\n    {r['snip']}")
            else: print(f"[V] {r['video_id']}  {r['title']}  views={r['views']}\n    {r['snip']}")
    elif a.cmd == "top":
        for r in out: print(f"{r['video_id']}  {str(r[a.by]):>8}  {(r['published_at'] or '')[:10]:10}  {r['title']}")
    else: print(json.dumps(out, indent=2, default=str))

if __name__ == "__main__": main()
