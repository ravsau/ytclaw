---
name: ytclaw
description: "Local SQLite memory for YouTube channels. Use for ANY question about a channel's videos, transcripts, comments, or view history: 'what did I say about X', 'which video mentions Y', 'what do viewers ask in comments', 'top videos', 'how did video Z grow', competitor research, content gaps. Reads are free and instant. Only `sync` touches the network and it spends Data API quota units, never dollars."
argument-hint: "[sync @handle | search 'phrase' | video ID | top | stats]"
---

# ytclaw

One SQLite file per machine (`~/.ytclaw/ytclaw.sqlite`) holding every synced channel: videos, descriptions, tags, a stats history, comments with replies, transcript segments, FTS5 search. Command: `ytclaw`. If it is missing: `uv tool install git+https://github.com/ravsau/ytclaw`.

## Rule: local first

Answer from the database before any network call. `ytclaw stats` tells you what is local and how much quota today's syncs used. Run `sync` only when the user asks for fresh data or `stats` shows the channel is absent or stale.

## Read commands (free)

```bash
ytclaw stats                                        # channels, counts, quota used today
ytclaw search "phrase"                              # videos + transcripts + comments, ranked
ytclaw search "phrase" --in transcripts -n 10       # returns watch URLs with &t= offsets
ytclaw search "phrase" --in comments
ytclaw video VIDEO_ID                               # metadata, tags, full stats history
ytclaw top --by views|likes|comments -n 20
ytclaw sql "select ..."                             # read-only; add --json for parsing
```

Always pass `--json` when you will parse the output.

## Sync commands (metered, quota units)

```bash
ytclaw sync @handle                       # videos + stats, ~1 unit per 50 videos, early-stops
ytclaw sync @handle --comments            # +1 unit per video with comments
ytclaw sync @handle --transcripts         # 0 units; caption endpoint rate-limits after ~40
ytclaw sync @handle --limit 40            # cap per run; default 200
```

Exit 75 means a quota or rate-limit checkpoint. Progress is saved. Tell the user and continue with local data. Never loop on `sync` to push through a block.

Needs `YOUTUBE_API_KEY` or `~/.ytclaw/config.json`. If missing, point the user at the README section "Get a YouTube API key" and stop.

## Recipes

**What did the channel say about X, with timestamps**
`ytclaw search "X" --in transcripts -n 15` then group hits by video.

**Unanswered viewer questions**
```sql
select v.title, c.author, c.text from comments c join videos v using(video_id)
where c.parent_id is null and c.text like '%?%'
and c.comment_id not in (select parent_id from comments where parent_id is not null)
order by c.published_at desc limit 40
```

**Hook audit: first 15 seconds of the last N videos**
```sql
select v.video_id, v.title, group_concat(t.text, ' ') hook from videos v
join transcript_segments t on t.video_id=v.video_id and t.start < 15
where v.published_at > date('now','-60 days') group by v.video_id order by v.published_at desc
```

**Velocity: views gained between two syncs**
```sql
select video_id, min(views) first_seen, max(views) latest, max(views)-min(views) gained
from stats_snapshots group by video_id order by gained desc limit 20
```

**Competitor gap**
Sync a competitor with `--comments`, then search their comments for the topic. Their audience's open questions are your video list.

**Cross-channel compare**
```sql
select c.handle, count(*) videos, avg(v.views) avg_views from videos v join channels c using(channel_id) group by 1
```

## Schema quick reference

`channels`, `channel_snapshots`, `videos` (latest stats + `first_seen_at`/`last_seen_at`/`seen_count`), `stats_snapshots` (content-hashed history), `comments` (`parent_id` for replies), `transcript_segments` (`idx`, `start`, `duration`, `text`), `unresolved` (negative cache with TTL), `sync_cache` (cursors, quota ledger), `videos_fts`, `transcript_fts`, `comments_fts`.

## Output

Cite the video ID or URL for every claim. Report `NO DATA` when a table is empty for that channel instead of guessing. Never state a number you did not read from a query.
