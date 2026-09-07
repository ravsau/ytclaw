---
name: ytclaw
description: "Search local YouTube channel memory, inspect metadata and thumbnail history, find viewer questions, report observed growth, and check collection coverage. Use the CLI for channel research and drift checks; live collection is explicit."
argument-hint: "[search phrase | report --channel @handle | changes | project drift VIDEO_ID | sync @handle]"
---

# ytclaw

Channel data lives in `~/.ytclaw/ytclaw.sqlite`, overridden by `--db` or `YTCLAW_DB`.
Install from GitHub with `uv tool install git+https://github.com/ravsau/ytclaw`, or
`uv tool install .` from a checkout. The CLI and JSON are the integration interface.

## Start with local evidence

Run `ytclaw doctor` or `ytclaw stats` to see channels, coverage, and freshness. A
successful sync does not mean all transcripts or comment scans are present. Use
`--json` before the command when parsing results. Cite source URLs and observation
dates. State missing data explicitly; never treat missing rows as zero.

```bash
ytclaw --json search "setup" --channel @handle --in transcripts
ytclaw --json questions --channel @handle
ytclaw --json changes --channel @handle --since 2026-09-01
ytclaw --json growth --channel @handle --since 2026-09-01
ytclaw --json report --channel @handle
ytclaw video VIDEO_ID
ytclaw history VIDEO_ID
ytclaw runs --channel @handle
```

`search` and `top` dates filter video publication time. `changes` dates filter
metadata observations. `questions` dates filter comment publication. Transcript
results contain neighboring segments and timestamp links. `questions` uses a question
mark filter and excludes unverified reply scans unless `--include-unverified` is used.
It detects absence of any observed reply, not absence of a channel-owner response.

`growth` uses dated observations and includes actual boundaries, partial-window flags,
and negative changes. Do not use min/max over legacy `stats_snapshots` as a substitute
for chronological growth. `report` defaults to seven days, 20 entries per list.

## Live collection

```bash
ytclaw init --channel @handle                 # hidden API key prompt
ytclaw sync @handle --comments --transcripts --thumbnails --limit 30
ytclaw sync @handle --full                    # walk the complete uploads list
```

Only sync when fresh data is requested or needed. Data API calls spend quota units.
Thumbnail downloads and caption requests add network traffic. `--limit` caps comment
and transcript videos, not metadata or pages per video's comments. Full replies are
paginated. Interrupted collection retains committed upload pages, metadata batches,
and comment pages. Run logs show partial and failed work.

Exit `75` means partial collection or a quota/rate-limit stop. Do not repeatedly retry
a blocked caption source. Exit `1` means failure. API key setup does not grant owner
analytics access. `doctor` checks configuration without making network requests.

## Metadata and working-file drift

```bash
ytclaw baseline VIDEO_ID                     # pin latest local version; replaces a prior pin
ytclaw sync @handle --thumbnails
ytclaw drift VIDEO_ID
ytclaw project link VIDEO_ID project.json
ytclaw project drift VIDEO_ID
ytclaw thumbnail SHA256 output.jpg
```

Baseline drift compares database versions. Project drift rereads mapped local files
and compares only mapped fields against the last API observation, not a YAML import.
The manifest supports `title_file`, `description_file`, `tags_file`, `thumbnail_file`,
and inline `metadata`. Paths are relative to the manifest. Thumbnail byte differences
are separate from metadata drift because YouTube may recompress uploads. Always report
the image check time. A metadata-only sync retains the last archived image.

History contains observed versions, not exact edit times, editor identities, or all
concurrent YouTube experiment variants. Do not infer causality from growth after an edit.

## Owner reports and notes

```bash
ytclaw auth status
ytclaw analytics show VIDEO_ID
ytclaw analytics compare VIDEO_ID --version 12 --days 7
ytclaw analytics import VIDEO_ID --csv video-by-date.csv
ytclaw experiment list VIDEO_ID
```

`auth login --client-secret desktop-client.json` requires the optional `owner`
dependencies and user consent in a browser. The API path supports daily watch time,
retention, and traffic reports. Studio CSV import handles documented date/metric
columns, including impressions and CTR. Missing days are not zero. `compare` excludes
the observed change date and is not an A/B test. Experiment notes are manual records.
Never print OAuth tokens or API keys. Database exports do not include credentials.

## Monitoring, backups, and UI

```bash
ytclaw watch add @handle --every-hours 24 --comments --thumbnails
ytclaw watch run --once
ytclaw watch install                         # installs a per-user OS timer
ytclaw watch list
ytclaw backup archive.sqlite
ytclaw export archive.zip
ytclaw --db restored.sqlite restore archive.zip
ytclaw serve
```

Do not claim that a scheduled collector runs while the machine is off. Creating a job
and installing its timer are separate actions. Restore and export refuse overwrites.
The loopback browser reads local data; its explicit Sync button performs collection.
`ytclaw --db demo.sqlite demo` creates labeled synthetic data in a new database.

## SQL and schema

SQL is read-only at the SQLite boundary. Tables include `channels`, `videos`,
`metadata_versions`, `metadata_baselines`, `thumbnail_assets`, `stats_observations`,
`comments`, `transcript_segments`, `collection_status`, `sync_runs`, `project_links`,
`watch_jobs`, `owner_reports`, and `experiments`. Legacy `stats_snapshots` remains
available but cannot recover observation order lost by older versions.

Prefer the supported report commands to hand-written SQL. For a hook excerpt:

```sql
select v.video_id, v.title, t.start, t.text
from videos v join transcript_segments t using(video_id)
where t.start < 15 order by v.published_at desc, t.idx;
```

Consult README.md and docs/ for precise setup, scheduler behavior, and CSV units.
