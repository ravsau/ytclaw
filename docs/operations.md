# Collection and monitoring

## A first full collection

```bash
ytclaw init --channel @yourchannel
ytclaw sync --full --comments --transcripts --thumbnails --limit 30
ytclaw doctor
ytclaw runs
```

Look at coverage, not only the exit code. Thirty successfully fetched transcripts may
be a useful partial catalog, but it is not the whole channel. `videos_unavailable`
means the public videos endpoint omitted an ID; it does not distinguish deletion
from privacy. Public channel totals and accessible uploaded videos can differ.

A process killed during uploads leaves discovered IDs in `pending_videos`. The next
run resumes the saved playlist token. A process killed during metadata refresh keeps
completed batches; remaining IDs are retained, and known local videos are refreshed
again. An expired page token restarts that walk without losing the pending IDs.

Comment scans checkpoint completed thread pages. If interrupted partway through a
thread's replies, that thread page is refetched on resume. No stale comments are
pruned until all pages have been read. Pagination is not a server-side snapshot;
comments arriving during a long scan may be captured by the next refresh.

## Daily collection

```bash
ytclaw watch add @yourchannel --every-hours 24 --comments --thumbnails
ytclaw watch run --once
ytclaw watch install
```

The job stores options in the selected database. Repeating `watch add` updates its
options and interval while retaining the next due time. It does not install an OS
timer by itself. After a failure, the next attempt is delayed by one to six hours,
depending on the normal interval. Foreground `watch run` checks due jobs every 30 seconds.

The installed timer starts the exact Python executable and package path used during
installation. Run `watch install` again after moving the checkout, changing virtual
environments, or replacing an installation. API keys must be saved by `init`; the
OS timer does not inherit your interactive shell's `YOUTUBE_API_KEY`.

macOS writes `~/Library/LaunchAgents/local.ytclaw.<database-hash>.plist` and logs under
`YTCLAW_HOME`. It runs in your login session. Linux writes a `.service` and `.timer`
under `~/.config/systemd/user`; inspect with `systemctl --user status` and `journalctl --user`.
User services ordinarily depend on a login session. ytclaw does not enable lingering,
keep a sleeping laptop awake, or provision a cloud host.

```bash
ytclaw watch list
ytclaw runs --channel @yourchannel
ytclaw report --channel @yourchannel --format markdown > weekly.md
ytclaw watch uninstall
ytclaw watch remove @yourchannel
```

`uninstall` removes this database's OS timer. Job definitions remain until removed.
One database can watch multiple channels. Sync and watcher locks are released by the
OS if a process exits. Run records left `running` are marked `interrupted` when the
next collector obtains the lock.

## The reading desk

```bash
ytclaw serve --port 8765
```

It binds only to `127.0.0.1`. The browser loads local records and archived images.
“Refresh local data” does not call YouTube. “Sync channel” refreshes the selected
channel's metadata and thumbnails and scans up to 30 eligible comment videos. It
does not collect transcripts or owner analytics. The button is disabled for sample data.

The server has no arbitrary SQL or arbitrary file-serving endpoint. Writes require
a per-process session token, and Host/Origin checks restrict browser actions to the
loopback origin. Baseline pinning and experiment notes change only the local database.

## Backups

Use `backup` or `export` instead of copying an open SQLite file without its WAL.
Restore always targets a new database. It preserves FTS, image bytes, baselines,
reports, and run records. Configuration credentials live separately. Treat an export
as your channel archive: it can also contain local project paths and private owner reports.
