# ytclaw

Searchable local memory for YouTube channels, with a record of what changed.

Find an explanation in your back catalog, review title and thumbnail changes, collect
viewer questions, and keep the evidence in a SQLite file you own. Read it through a
CLI, JSON, SQL, or a local browser interface. Public collection works with any public
channel; owner analytics are an optional authenticated path.

Inspired by [birdclaw](https://github.com/steipete/birdclaw) by Peter Steinberger
([@steipete](https://x.com/steipete)). MIT licensed.

## Install and try it

Python 3.10 or newer. macOS and Linux are supported for collection and scheduling.

```bash
uv tool install git+https://github.com/ravsau/ytclaw
# From a checkout:
uv tool install .
```

Explore a synthetic channel before setting up an API key. Use a new database path:

```bash
ytclaw --db demo.sqlite demo
ytclaw --db demo.sqlite serve
```

The reading desk opens at `http://127.0.0.1:8765`. If that port is occupied, use
`serve --port 0` to choose a free port. Sample data is labeled, and live sync is disabled
in the sample browser. Demo never overwrites a database.

## Connect your channel

1. Create a project in [Google Cloud](https://console.cloud.google.com/).
2. Enable [YouTube Data API v3](https://console.cloud.google.com/apis/library/youtube.googleapis.com).
3. Create an API key in [Credentials](https://console.cloud.google.com/apis/credentials).
4. Run the guided setup. The key prompt is hidden:

```bash
ytclaw init --channel @yourchannel
ytclaw sync --comments --transcripts --thumbnails --limit 30
ytclaw serve
```

For noninteractive setup, set `YOUTUBE_API_KEY` in your environment and run
`ytclaw init --channel @yourchannel --non-interactive`. Setup saves the key and default
channel in `~/.ytclaw/config.json` with owner-only permissions. An explicit handle
on `sync` overrides the default.

`ytclaw doctor` checks local configuration, dependencies, database integrity, and
collection coverage without calling YouTube. It never prints the key.

Database: `~/.ytclaw/ytclaw.sqlite`. Override with `--db` or `YTCLAW_DB`.
`YTCLAW_HOME` selects a separate configuration and credential directory.

## Read useful results without SQL

```bash
ytclaw search "camera setup" --channel @yourchannel
ytclaw search "pricing" --in transcripts            # surrounding context in --json output
ytclaw top --channel @yourchannel --since 2026-08-01 --by views
ytclaw questions --channel @yourchannel              # question marks, no observed replies
ytclaw changes --channel @yourchannel --since 2026-09-01
ytclaw growth --channel @yourchannel --since 2026-09-01
ytclaw report --channel @yourchannel > weekly-report.md
ytclaw --json report --channel @yourchannel
ytclaw video VIDEO_ID
ytclaw runs --channel @yourchannel
ytclaw stats
```

- `search` and `top` date filters use video publication time. Transcript search includes
  timestamped source links and two neighboring segments on each side in JSON.
- `changes` dates use metadata observation time. The first observation is labeled;
  it is not an inferred edit. `history VIDEO_ID` returns the complete local timeline.
- `questions` dates use comment publication time. By default, results require a
  completed reply scan. `--include-unverified` includes imported or partial data with
  a coverage label. A question mark is a simple filter, not sentiment analysis.
- `growth` compares actual dated public view observations. Counts can decrease.
  It uses the nearest observation at or before the requested start, or labels a
  partial window when none exists. The actual comparison dates are included.
- `report` defaults to the last seven days and 20 entries per list; increase with `-n`.
  Missing records are not interpreted as zeros. View growth after an edit is not
  evidence that the edit caused it.

Date-only `--until` values include the entire UTC day in local reports.
Global `--json` goes before the command. SQL is enforced read-only by SQLite and an
authorizer, including statements beginning with `WITH`:

```bash
ytclaw sql "select title, views from videos order by views desc limit 5"
```

## Track titles, metadata, and images

Every sync records observed changes to titles, descriptions, tags, publication time,
duration, category, language fields, and thumbnail URLs. Identical consecutive
metadata from the same source extends `last_seen_at`. Reverting a title creates
another version. View-count changes do not create metadata versions.

```bash
ytclaw baseline VIDEO_ID                # pin the current local version
# Edit the video in YouTube Studio, then:
ytclaw sync @yourchannel --thumbnails
ytclaw drift VIDEO_ID                   # compare latest local version to the pin
ytclaw history VIDEO_ID
```

The baseline stays pinned until replaced explicitly. YAML imports create versions
with source `yaml`. Existing databases preserve their last local copy as a `migration`
version. History begins with observations; it cannot reconstruct unobserved edits,
identify the editor, or capture every setting in YouTube Studio.

`--thumbnails` downloads the largest available image on every sync, including when
its URL stays unchanged. Bytes are deduplicated by SHA-256 in SQLite. A failed download
is recorded in collection health and makes the run partial. An omitted or failed image
check retains the last successful archive; it is not evidence of a fresh image check.

```bash
ytclaw thumbnail SHA256 saved-thumbnail.jpg
```

Export refuses to overwrite a file. The browser shows archived images side by side.
Downloads add traffic and database size, but do not spend Data API quota units.

## Compare YouTube against working files

A pinned database version and a local project are separate baselines. Link the files
you actually edit, then compare them with the last API metadata observation:

```bash
ytclaw project link VIDEO_ID path/to/project.json
ytclaw sync @yourchannel --thumbnails
ytclaw project drift VIDEO_ID
ytclaw project list
ytclaw project unlink VIDEO_ID
```

See [the example manifest](examples/project/project.json) and
[project-file drift](docs/project-drift.md). File paths are relative to the manifest.
Only mapped fields are compared. A later YAML import does not replace the API side
of this comparison. Thumbnail byte differences are reported separately because
YouTube may resize or recompress an uploaded image.

## Keep it collecting

```bash
ytclaw watch add @yourchannel --every-hours 24 --comments --thumbnails
ytclaw watch run --once                 # run due jobs now
ytclaw watch install                    # macOS LaunchAgent or Linux user timer
ytclaw watch list
ytclaw watch uninstall                  # remove this database's OS timer
ytclaw watch remove @yourchannel        # remove a job
```

The timer checks for due jobs every five minutes. Each job has its own interval.
`watch run` also works as a foreground process. Failed jobs retry later, and per-database
locks prevent overlapping collectors. The machine must be on and the scheduler
available. A user timer is not cloud hosting. See [operations](docs/operations.md)
for logs, restart behavior, and Linux login requirements.

## Owner analytics and experiment notes

The optional owner module stores daily views and watch time, retention curves, and
traffic-source reports through read-only OAuth. It also imports dated Studio CSVs
for impressions, CTR, watch time, and supported revenue columns.

```bash
uv tool install '.[owner]'              # from this checkout
ytclaw auth login --client-secret desktop-client.json
ytclaw analytics sync --channel @yourchannel --since 2026-08-01 --until 2026-09-01
ytclaw analytics show VIDEO_ID
ytclaw analytics import VIDEO_ID --csv video-by-date.csv
ytclaw analytics compare VIDEO_ID --version 12 --days 7

ytclaw experiment add VIDEO_ID --note "Shortened the title" --result "Studio test was inconclusive"
ytclaw experiment list VIDEO_ID
```

Owner reports require ownership and API availability. CSV imports do not require
OAuth. `compare` excludes the date the change was observed and lists missing days.
It is an observational comparison, not a controlled experiment. Experiment notes
are user-entered, not automatically fetched test results.

See [owner setup and supported CSV columns](docs/owner-analytics.md).
OAuth credentials stay outside database backups and exports.

## Back up, export, restore

```bash
ytclaw backup channel-backup.sqlite
ytclaw export channel-export.zip
ytclaw --db restored.sqlite restore channel-export.zip
# A SQLite backup can also be the restore source.
```

Backup uses SQLite's snapshot API and verifies database integrity. ZIP export includes
that snapshot, readable JSONL tables, image bytes, and a checksum manifest. Restore
checks integrity and archive checksums and requires a new destination. Files are never
silently overwritten. JSONL contains base64 objects for binary image data.

Archives contain channel records and local file mappings. They do not contain the
API key or OAuth token. Restored mappings may need relinking on another machine.
Exports are for restore and inspection; arbitrary JSONL merging is not implemented.

## Collection behavior and limits

- Upload IDs and pagination cursors are committed together after every page. Video
  metadata commits in batches of up to 50. Interrupted work is retried safely.
- Comment collection fetches all reply pages when embedded replies are incomplete.
  Deleted comments are pruned only after a complete scan. Imported comments do not
  count as verified complete scans.
- Each sync creates a run record: `running`, `success`, `partial`, `failed`, or
  `interrupted`. Success means the requested work completed, not that unrequested
  transcripts or limited comment batches are complete for the channel.
- HTTP network and transient server failures get up to three attempts. Quota errors
  stop collection. Failed Data API attempts are included in the local quota ledger,
  whose day boundary is Pacific time. The ledger cannot see other clients using the key.
- Caption rate limits stop the run without a long-lived negative cache. Known unavailable
  captions have a 30-day retry TTL. Captions remain dependent on the caption source;
  no automatic speech-recognition fallback is included.
- Exit `75` means partial collection or quota/rate-limit stop. Exit `1` means failure.
  `--limit` caps comment/transcript videos per run, not metadata collection or comment pages.
- New timestamps are UTC. Old timestamps without offsets are retained because their
  original timezone cannot be reconstructed. Legacy hash-deduplicated stats cannot
  recover lost observation order; new growth reports use the new observation table.

## Storage and agent integration

Core tables include `channels`, `videos`, `metadata_versions`, `metadata_baselines`,
`thumbnail_assets`, `stats_observations`, `channel_snapshots`, `comments`,
`transcript_segments`, `collection_status`, `sync_runs`, `project_links`, `watch_jobs`,
`owner_reports`, and `experiments`. The old `stats_snapshots` table remains for compatibility.
FTS5 indexes cover current video metadata, transcripts, and comments.

```bash
ytclaw skill install                        # ~/.claude/skills/ytclaw
ytclaw skill install --dir ~/.codex/skills
```

The [bundled skill](skills/ytclaw/SKILL.md) explains local reads, collection coverage,
and source-backed answers. The CLI and JSON output are the integration interface.

Existing per-video YAML dumps can still be loaded with `ytclaw import --yaml DIR`.
Unchanged files are skipped, and each successfully imported file is committed.

## Development

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
PYTHON=python3 bash tests/test_smoke.sh
uv build
```

[Verification notes](docs/verification.md) distinguish automated fixtures, browser
checks, and account-dependent paths. No YouTube writes are implemented. Future work
includes playlist modeling, stronger transcript retrieval, bulk owner-report jobs,
and a wider set of creator-tested workflows.
