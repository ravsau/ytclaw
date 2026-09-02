# ytclaw

Local SQLite memory for a YouTube channel. Sync once, then search videos, transcripts, comments, and stats history for free, forever.

Inspired by [birdclaw](https://github.com/steipete/birdclaw) by Peter Steinberger ([@steipete](https://x.com/steipete)), which does this for X. Same idea, different platform: your channel data belongs in a file you own, and every read should cost nothing.

```
$ ytclaw sync @CloudYeti --comments --transcripts
{ "channel": "CloudYeti", "new_videos": 175, "comments_upserted": 444, "transcripts_saved": 30, "quota_units_this_run": 36 }

$ ytclaw search "mlx" --in transcripts
[T] https://www.youtube.com/watch?v=rTX2hK8m6zc&t=5071s  Qwen3.8-27B Launch Day
    ...runs through [MLX] on Apple silicon...
```

## Any public channel, not only yours

An API key reads public data for every channel. Point `sync` at anyone:

```bash
ytclaw sync @Fireship                 # 837 videos with stats and tags, 35 quota units
ytclaw sync @Fireship --comments      # their audience, searchable
ytclaw sql "select handle, count(*) from videos join channels using(channel_id) group by 1"
```

All channels share one database. Every row carries a `channel_id`, so you can search across competitors, compare stats histories, or mine comments for what their viewers ask that nobody answers.

## Install

```bash
uv tool install git+https://github.com/ravsau/ytclaw     # or: pipx install git+https://github.com/ravsau/ytclaw
ytclaw --help
```

Python 3.10 or newer. Two dependencies: `youtube-transcript-api` for captions and `pyyaml` for the optional import.

## Get a YouTube API key (5 minutes, free)

ytclaw reads public channel data through the YouTube Data API v3. That needs an API key and nothing else: no OAuth, no app review, no billing account, no channel ownership. Transcripts come from YouTube's caption endpoint and need no key at all.

1. Open https://console.cloud.google.com/ and sign in with any Google account.
2. Create a project. Top bar, project picker, **New project**, any name.
3. Enable the API: https://console.cloud.google.com/apis/library/youtube.googleapis.com and click **Enable**.
4. Create the key: https://console.cloud.google.com/apis/credentials, **Create credentials**, **API key**. Copy it.
5. Optional but wise: click the key, under **API restrictions** pick **Restrict key** and select only *YouTube Data API v3*.
6. Give it to ytclaw, either way:

```bash
export YOUTUBE_API_KEY=AIza...            # shell
# or
mkdir -p ~/.ytclaw && echo '{"api_key": "AIza..."}' > ~/.ytclaw/config.json
```

**Quota.** Every project gets 10,000 units per day, free. ytclaw spends about 1 unit per 50 videos for metadata and stats, and 1 unit per video for comments. A full first sync of a 200-video channel with comments is about 220 units. `ytclaw stats` shows the units used today. Transcripts use no quota at all.

## Use

```bash
ytclaw sync @handle                       # videos + stats. Early-stops when a page is already local.
ytclaw sync @handle --comments            # + comment threads with replies
ytclaw sync @handle --transcripts         # + captions, newest first
ytclaw sync @handle --full                # walk the whole uploads list, ignore early-stop
ytclaw sync @handle --limit 50            # cap comments/transcripts per run (default 200)

ytclaw search "bedrock pricing"           # videos + transcripts + comments, ranked
ytclaw search "thank you" --in comments
ytclaw search "guardrails" --in transcripts   # links with &t= offsets
ytclaw video VIDEO_ID                     # metadata + full stats history
ytclaw top --by views -n 10
ytclaw stats                              # counts, latest channel numbers, quota used today
ytclaw sql "select title, views from videos order by views desc limit 5"   # read-only
ytclaw --json search "lambda"             # every command speaks JSON
```

Database: `~/.ytclaw/ytclaw.sqlite`. Override with `YTCLAW_DB` or `--db`.

## What gets stored

| table | holds |
|---|---|
| `channels` | id, handle, uploads playlist |
| `channel_snapshots` | subscribers, total views, video count, one row per sync |
| `videos` | title, description, tags, duration, latest stats, `first_seen_at`, `last_seen_at`, `seen_count` |
| `stats_snapshots` | content-hashed per-video stats history. Same numbers again touches `last_seen_at`, no new row |
| `comments` | top-level comments and replies, with `parent_id` |
| `transcript_segments` | caption lines with start and duration |
| `unresolved` | negative cache: deleted videos, disabled comments, missing captions, with a TTL |
| `sync_cache` | cursors, page tokens, quota ledger |
| `*_fts` | FTS5 indexes over titles, descriptions, transcripts, comments |

## Checkpoint and resume

Sync is designed to be interrupted.

- The uploads walk saves its page token after every page. A killed run resumes on the next page.
- Comments and transcripts commit per video and stamp `comments_synced_at` / `transcript_synced_at`. Rerun and it continues with the next video.
- A quota or rate-limit 403 from the Data API stops the run, prints what was done, and exits with code 75. Nothing is lost. Run again after the quota resets (midnight Pacific).
- The caption source rate limits by IP after a few dozen fetches in a row. ytclaw stops, reports `IpBlocked`, and does not mark those videos as failed. Wait a while or switch networks and rerun. `--limit 30` per run keeps you under it.
- Videos with no captions, disabled comments, or that were deleted go in `unresolved` with a TTL so they are not retried every run.

## Bring your own dump

If you already have per-video YAML files with `transcript` and `comments` keys (for example from Whisper runs), load them:

```bash
ytclaw import --yaml path/to/dir
```

Unchanged files are skipped on re-import.

## Known limits

- Public data only. Watch time, retention, and revenue need the Analytics API with OAuth, which ytclaw does not do.
- Transcripts depend on YouTube captions. Videos with none stay empty unless you import your own.

## Design, borrowed from birdclaw

| birdclaw | ytclaw |
|---|---|
| one SQLite file under `~/.birdclaw` | one SQLite file under `~/.ytclaw` |
| `tweets_fts` FTS5 shadow table | `videos_fts`, `transcript_fts`, `comments_fts` |
| `sync_cache` with per-resource cursors and `pending` / `committed` states | same, keyed per playlist and per file |
| `first_seen_at` / `last_seen_at` / `seen_count` | same columns on `videos` |
| content-hashed `profile_snapshots` | content-hashed `stats_snapshots` |
| `geocoded_locations_unresolved` negative cache with TTL | `unresolved` table with TTL |
| `--early-stop` on a fully local page | default behaviour of `sync`, `--full` disables |
| free local reads vs paid live reads | `search`/`video`/`top`/`stats`/`sql` are local; only `sync` touches the network, and it prints its quota cost |

## License

MIT
