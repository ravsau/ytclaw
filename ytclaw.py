#!/usr/bin/env python3
"""ytclaw - a local SQLite store + search for your own YouTube channel data.

Pattern borrowed from birdclaw by Peter Steinberger (@steipete):
one SQLite file, FTS5 shadow tables, content-hashed snapshots, a sync_cache
with per-source cursors, and free local reads separated from paid network calls.

ytclaw does NOT call the YouTube API. It imports what fetchers already saved
(per-video YAML with transcripts + comments, daily JSON stat snapshots, a views
JSONL) so every query is $0 and 0 quota units.

Usage:
  ytclaw import  --yaml DIR [--json DIR] [--views FILE]
  ytclaw search  QUERY [--in transcripts|comments|videos|all] [-n 20]
  ytclaw video   VIDEO_ID
  ytclaw top     [--by views|likes|comments] [-n 20]
  ytclaw stats
  ytclaw sql     "SELECT ..."
Global: --db PATH (default ~/.ytclaw/ytclaw.sqlite), --json
"""
import argparse, hashlib, json, os, sqlite3, sys, time
from pathlib import Path

DEFAULT_DB = Path(os.environ.get("YTCLAW_DB", Path.home() / ".ytclaw" / "ytclaw.sqlite"))

SCHEMA = """
create table if not exists videos(
  video_id text primary key, channel text, title text, description text,
  tags_json text, published_at text, duration text, url text,
  views integer, likes integer, comments integer,
  first_seen_at text, last_seen_at text, seen_count integer default 1);
create table if not exists transcript_segments(
  video_id text, idx integer, start real, duration real, text text,
  primary key(video_id, idx));
create table if not exists comments(
  comment_id text primary key, video_id text, author text, text text,
  likes integer, published_at text, parent_id text);
create table if not exists stats_snapshots(
  video_id text, snapshot_hash text, observed_at text, last_seen_at text,
  views integer, likes integer, comments integer, source text,
  primary key(video_id, snapshot_hash));
create table if not exists channel_snapshots(
  observed_at text primary key, subscribers integer, total_views integer,
  video_count integer, source text);
create table if not exists sync_cache(
  cache_key text primary key, value_json text, updated_at text);
create virtual table if not exists videos_fts using fts5(video_id unindexed, title, description);
create virtual table if not exists transcript_fts using fts5(video_id unindexed, idx unindexed, text);
create virtual table if not exists comments_fts using fts5(comment_id unindexed, text);
"""

def S(x): return None if x is None else str(x)  # yaml may parse bare dates as datetime
def now(): return time.strftime("%Y-%m-%dT%H:%M:%S")
def h(*parts): return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]

def connect(db):
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(db); c.row_factory = sqlite3.Row
    c.executescript(SCHEMA); return c

# ---------- sync_cache: skip files that have not changed ----------
def file_cursor(c, path):
    st = Path(path).stat(); sig = f"{st.st_mtime_ns}:{st.st_size}"
    key = f"file:{path}"
    row = c.execute("select value_json from sync_cache where cache_key=?", (key,)).fetchone()
    if row and json.loads(row[0]).get("sig") == sig: return None
    return key, sig

def commit_cursor(c, key, sig):
    c.execute("insert or replace into sync_cache values(?,?,?)", (key, json.dumps({"sig": sig, "state": "committed"}), now()))

# ---------- upserts ----------
def upsert_video(c, v, source):
    t = now()
    c.execute("""insert into videos(video_id,channel,title,description,tags_json,published_at,duration,url,
      views,likes,comments,first_seen_at,last_seen_at,seen_count) values(?,?,?,?,?,?,?,?,?,?,?,?,?,1)
      on conflict(video_id) do update set
      title=coalesce(excluded.title,title), description=coalesce(excluded.description,description),
      tags_json=coalesce(excluded.tags_json,tags_json), published_at=coalesce(excluded.published_at,published_at),
      duration=coalesce(excluded.duration,duration), url=coalesce(excluded.url,url),
      views=coalesce(excluded.views,views), likes=coalesce(excluded.likes,likes), comments=coalesce(excluded.comments,comments),
      last_seen_at=excluded.last_seen_at, seen_count=seen_count+1""",
      (v["video_id"], v.get("channel"), v.get("title"), v.get("description"),
       json.dumps(v.get("tags") or []), S(v.get("published_at")), v.get("duration"),
       v.get("url") or f"https://www.youtube.com/watch?v={v['video_id']}",
       v.get("views"), v.get("likes"), v.get("comments"), t, t))
    c.execute("delete from videos_fts where video_id=?", (v["video_id"],))
    c.execute("insert into videos_fts values(?,?,?)", (v["video_id"], v.get("title") or "", v.get("description") or ""))
    if v.get("views") is not None:
        snap_stats(c, v["video_id"], S(v.get("observed_at")) or t, v.get("views"), v.get("likes"), v.get("comments"), source)

def snap_stats(c, vid, observed_at, views, likes, comments, source):
    sh = h(views, likes, comments)
    c.execute("""insert into stats_snapshots values(?,?,?,?,?,?,?,?)
      on conflict(video_id,snapshot_hash) do update set last_seen_at=max(last_seen_at,excluded.last_seen_at)""",
      (vid, sh, observed_at, observed_at, views, likes, comments, source))

def replace_transcript(c, vid, segs):
    c.execute("delete from transcript_segments where video_id=?", (vid,))
    c.execute("delete from transcript_fts where video_id=?", (vid,))
    rows = [(vid, i, s.get("start"), s.get("duration"), s.get("text") or "") for i, s in enumerate(segs)]
    c.executemany("insert into transcript_segments values(?,?,?,?,?)", rows)
    c.executemany("insert into transcript_fts values(?,?,?)", [(vid, i, t) for (_, i, _, _, t) in rows])

def upsert_comments(c, vid, comments, parent=None):
    n = 0
    for cm in comments or []:
        cid = cm.get("id") or h(vid, cm.get("author"), cm.get("published_at"), (cm.get("text") or "")[:40])
        c.execute("""insert into comments values(?,?,?,?,?,?,?) on conflict(comment_id) do update set
          likes=excluded.likes, text=excluded.text""",
          (cid, vid, cm.get("author"), cm.get("text"), cm.get("likes"), S(cm.get("published_at")), parent))
        c.execute("delete from comments_fts where comment_id=?", (cid,))
        c.execute("insert into comments_fts values(?,?)", (cid, cm.get("text") or ""))
        n += 1 + upsert_comments(c, vid, cm.get("replies"), parent=cid)
    return n

# ---------- importers ----------
def import_yaml_dir(c, d):
    import yaml
    files = sorted(p for p in Path(d).glob("*.yaml") if not p.name.startswith("_"))
    done = skipped = 0
    for p in files:
        cur = file_cursor(c, p)
        if cur is None: skipped += 1; continue
        v = yaml.safe_load(p.read_text()) or {}
        if not v.get("video_id"): continue
        st = v.get("stats") or {}
        upsert_video(c, {**v, "views": st.get("views"), "likes": st.get("likes"),
                         "comments": st.get("comments"), "observed_at": v.get("pulled_at")}, source="yaml")
        if v.get("transcript"): replace_transcript(c, v["video_id"], v["transcript"])
        upsert_comments(c, v["video_id"], v.get("comments"))
        commit_cursor(c, *cur); done += 1
    c.commit(); return {"yaml_files": len(files), "yaml_imported": done, "yaml_unchanged": skipped}

def import_json_dir(c, d):
    files = sorted(p for p in Path(d).glob("*.json") if "latest" not in p.name)
    done = skipped = 0
    for p in files:
        cur = file_cursor(c, p)
        if cur is None: skipped += 1; continue
        j = json.loads(p.read_text()); obs = j.get("synced_at") or p.stem[-10:]
        c.execute("insert or replace into channel_snapshots values(?,?,?,?,?)",
                  (obs, j.get("subscribers"), j.get("total_views"), j.get("video_count"), "json"))
        for v in j.get("videos", []):
            upsert_video(c, {**v, "channel": j.get("channel"), "observed_at": obs}, source="json")
        commit_cursor(c, *cur); done += 1
    c.commit(); return {"json_files": len(files), "json_imported": done, "json_unchanged": skipped}

def import_views_jsonl(c, f):
    key = f"jsonl:{f}"; row = c.execute("select value_json from sync_cache where cache_key=?", (key,)).fetchone()
    offset = json.loads(row[0]).get("offset", 0) if row else 0
    n = 0
    with open(f) as fh:
        fh.seek(offset)
        for line in fh:
            line = line.strip()
            if not line: continue
            j = json.loads(line); obs = j.get("ts") or j.get("date")
            c.execute("insert or ignore into channel_snapshots values(?,?,?,?,?)",
                      (obs, j.get("subs"), j.get("channel_views"), j.get("video_count"), "views_jsonl"))
            for vid, views in (j.get("videos") or {}).items():
                c.execute("insert or ignore into videos(video_id,first_seen_at,last_seen_at) values(?,?,?)", (vid, obs, obs))
                snap_stats(c, vid, obs, views, None, None, "views_jsonl")
            n += 1
        offset = fh.tell()
    c.execute("insert or replace into sync_cache values(?,?,?)", (key, json.dumps({"offset": offset, "state": "committed"}), now()))
    c.commit(); return {"views_lines": n, "offset": offset}

# ---------- queries ----------
def q(term):  # make plain words safe for FTS5 MATCH
    return " ".join(f'"{w}"' for w in term.replace('"', " ").split()) or '""'

def search(c, term, scope, n):
    out = []
    if scope in ("videos", "all"):
        for r in c.execute("""select v.video_id, v.title, v.published_at, v.views,
          snippet(videos_fts,2,'[',']','...',12) snip from videos_fts join videos v using(video_id)
          where videos_fts match ? order by rank limit ?""", (q(term), n)):
            out.append({"kind": "video", **dict(r)})
    if scope in ("transcripts", "all"):
        for r in c.execute("""select t.video_id, v.title, t.idx, s.start,
          snippet(transcript_fts,2,'[',']','...',14) snip from transcript_fts t
          join transcript_segments s on s.video_id=t.video_id and s.idx=t.idx
          join videos v on v.video_id=t.video_id where transcript_fts match ? order by rank limit ?""", (q(term), n)):
            d = dict(r); d["url"] = f"https://youtu.be/{d['video_id']}?t={int(d['start'] or 0)}"
            out.append({"kind": "transcript", **d})
    if scope in ("comments", "all"):
        for r in c.execute("""select cm.video_id, v.title, cm.author, cm.likes, cm.published_at,
          snippet(comments_fts,1,'[',']','...',14) snip from comments_fts f
          join comments cm on cm.comment_id=f.comment_id join videos v on v.video_id=cm.video_id
          where comments_fts match ? order by rank limit ?""", (q(term), n)):
            out.append({"kind": "comment", **dict(r)})
    return out

def video(c, vid):
    v = c.execute("select * from videos where video_id=?", (vid,)).fetchone()
    if not v: return None
    d = dict(v); d["tags"] = json.loads(d.pop("tags_json") or "[]")
    d["stats_history"] = [dict(r) for r in c.execute(
        "select observed_at, last_seen_at, views, likes, comments, source from stats_snapshots where video_id=? order by observed_at", (vid,))]
    d["transcript_segments"] = c.execute("select count(*) from transcript_segments where video_id=?", (vid,)).fetchone()[0]
    d["comment_count_local"] = c.execute("select count(*) from comments where video_id=?", (vid,)).fetchone()[0]
    return d

def top(c, by, n):
    assert by in ("views", "likes", "comments")
    return [dict(r) for r in c.execute(f"select video_id, title, published_at, views, likes, comments from videos where title is not null order by {by} desc limit ?", (n,))]

def stats(c, db):
    t = lambda s: c.execute(s).fetchone()[0]
    return {"db": str(db), "size_mb": round(Path(db).stat().st_size / 1e6, 2),
            "videos": t("select count(*) from videos where title is not null"),
            "videos_with_transcript": t("select count(distinct video_id) from transcript_segments"),
            "transcript_segments": t("select count(*) from transcript_segments"),
            "comments": t("select count(*) from comments"),
            "stats_snapshots": t("select count(*) from stats_snapshots"),
            "channel_snapshots": t("select count(*) from channel_snapshots"),
            "latest_channel": dict(c.execute("select * from channel_snapshots order by observed_at desc limit 1").fetchone() or {}),
            "sync_cache_entries": t("select count(*) from sync_cache"),
            "api_calls_made_by_ytclaw": 0}

# ---------- cli ----------
def main(argv=None):
    ap = argparse.ArgumentParser(prog="ytclaw", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(DEFAULT_DB)); ap.add_argument("--json", action="store_true")
    sp = ap.add_subparsers(dest="cmd", required=True)
    i = sp.add_parser("import"); i.add_argument("--yaml"); i.add_argument("--json-dir", dest="json_dir"); i.add_argument("--views")
    s = sp.add_parser("search"); s.add_argument("query"); s.add_argument("--in", dest="scope", default="all",
        choices=["all", "videos", "transcripts", "comments"]); s.add_argument("-n", type=int, default=20)
    v = sp.add_parser("video"); v.add_argument("video_id")
    tp = sp.add_parser("top"); tp.add_argument("--by", default="views"); tp.add_argument("-n", type=int, default=20)
    sp.add_parser("stats")
    sq = sp.add_parser("sql"); sq.add_argument("query")
    a = ap.parse_args(argv)
    c = connect(a.db)

    if a.cmd == "import":
        if not (a.yaml or a.json_dir or a.views): ap.error("give at least one of --yaml, --json-dir, --views")
        res = {}
        if a.yaml: res.update(import_yaml_dir(c, a.yaml))
        if a.json_dir: res.update(import_json_dir(c, a.json_dir))
        if a.views: res.update(import_views_jsonl(c, a.views))
        out = res
    elif a.cmd == "search": out = search(c, a.query, a.scope, a.n)
    elif a.cmd == "video": out = video(c, a.video_id)
    elif a.cmd == "top": out = top(c, a.by, a.n)
    elif a.cmd == "stats": out = stats(c, a.db)
    elif a.cmd == "sql":
        if not a.query.lstrip().lower().startswith(("select", "with", "explain")): ap.error("sql: read-only, start with SELECT")
        out = [dict(r) for r in c.execute(a.query)]

    if a.json or a.cmd in ("import", "stats", "video"): print(json.dumps(out, indent=2, default=str)); return
    if a.cmd == "search":
        for r in out:
            if r["kind"] == "transcript": print(f"[T] {r['url']}  {r['title'][:50]}\n    {r['snip']}")
            elif r["kind"] == "comment": print(f"[C] {r['video_id']}  {r['author']} ({r['likes']} likes) on {r['title'][:40]}\n    {r['snip']}")
            else: print(f"[V] {r['video_id']}  {r['title']}  views={r['views']}\n    {r['snip']}")
    elif a.cmd == "top":
        for r in out: print(f"{r['video_id']}  {str(r[a.by]):>8}  {r['published_at'][:10] if r['published_at'] else '':10}  {r['title']}")
    else: print(json.dumps(out, indent=2, default=str))

if __name__ == "__main__": main()
