# ytclaw

A local SQLite store and full-text search for your own YouTube channel data. One file, one dependency, zero API calls.

Inspired by [birdclaw](https://github.com/steipete/birdclaw) by Peter Steinberger ([@steipete](https://x.com/steipete)). birdclaw keeps your X data in a local SQLite file so every read is free. ytclaw does the same for YouTube.

## What it does

- Imports what your fetchers already saved: per-video YAML (metadata, transcript, comments), daily JSON stat snapshots, and a views JSONL time series.
- Stores it in `~/.ytclaw/ytclaw.sqlite` with FTS5 indexes over titles, descriptions, transcripts, and comments.
- Skips unchanged files on re-import. A `sync_cache` table holds one cursor per source file.
- Keeps a content-hashed stats history per video. Same numbers again means one `last_seen_at` update, not a new row.
- Makes no network calls. Quota cost of every command is 0.

## Install

```bash
pip install pyyaml
curl -O https://raw.githubusercontent.com/ravsau/ytclaw/main/ytclaw.py
chmod +x ytclaw.py
```

## Use

```bash
# import
./ytclaw.py import --yaml path/to/corpus --json-dir path/to/snapshots --views path/to/snapshots.jsonl

# search everything, or one scope
./ytclaw.py search "bedrock pricing"
./ytclaw.py search "thank you" --in comments
./ytclaw.py search "guardrails" --in transcripts   # returns youtu.be links with ?t= offsets

# one video with its stats history
./ytclaw.py video HDlMRaJq8FE

# rankings and health
./ytclaw.py top --by views -n 10
./ytclaw.py stats

# read-only SQL
./ytclaw.py sql "select title, views from videos order by views desc limit 5"

# every command accepts --json
./ytclaw.py --json search "lambda" -n 3
```

## Input formats

**YAML, one file per video** (what `channel_corpus.py` in my content pipeline writes):

```yaml
video_id: abc123
title: ...
description: ...
tags: [..]
published_at: 2026-01-01T00:00:00Z
duration: PT9M44S
stats: {views: 1, likes: 1, comments: 1}
pulled_at: 2026-07-29T01:06:27
transcript: [{text: ..., start: 0.0, duration: 9.5}]
comments: [{author: "@x", text: ..., likes: 0, published_at: ..., replies: []}]
```

**JSON snapshot** with `synced_at`, `subscribers`, `total_views`, `video_count`, and a `videos` list of `{video_id, title, views, likes, comments, tags, description, published_at, duration}`.

**Views JSONL**, one line per day: `{"ts": ..., "subs": ..., "channel_views": ..., "videos": {"id": views}}`.

Point the importers at your own fetcher output. If your fields differ, edit the three `import_*` functions. They are short.

## Design, borrowed from birdclaw

| birdclaw | ytclaw |
|---|---|
| one SQLite file under `~/.birdclaw` | one SQLite file under `~/.ytclaw` |
| `tweets_fts` FTS5 shadow table | `videos_fts`, `transcript_fts`, `comments_fts` |
| `sync_cache` with per-resource cursors | `sync_cache` with per-file cursors |
| `first_seen_at` / `last_seen_at` / `seen_count` | same columns on `videos` |
| content-hashed `profile_snapshots` | content-hashed `stats_snapshots` |
| free local reads vs paid live reads | local reads only; fetching stays in your existing scripts |

## Numbers from my channel

195 videos, 51,444 transcript segments, 2,158 comments, 3,135 stat snapshots. First import takes about 6 seconds. A re-import with no changes takes 0.05 seconds.

## License

MIT
