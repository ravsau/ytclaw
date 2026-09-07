# Owner reports

Public collection uses an API key. Owner reports use a separate OAuth credential with
only `yt-analytics.readonly`. They never grant ytclaw permission to edit your videos.

## Connect

1. Enable YouTube Analytics API in your Google Cloud project.
2. Configure the OAuth consent screen and add your account as a test user if needed.
3. Create an OAuth client with application type **Desktop app** and download its JSON.
4. Install the optional dependency and sign in through your browser:

```bash
uv tool install '.[owner]'  # from the checkout
# From GitHub:
# uv tool install 'ytclaw[owner] @ git+https://github.com/ravsau/ytclaw'
ytclaw auth login --client-secret desktop-client.json
ytclaw auth status
```

The login uses Google's installed-app library, PKCE, and a loopback callback on a
random port. The token is saved with owner-only permissions in
`~/.ytclaw/owner-token.json` and refreshed when necessary. Account consent must be
completed by the user. OAuth apps in testing may require periodic reauthorization.

`ytclaw auth logout` deletes the local token. Remove the app from your Google account's
connections separately if you also want to revoke consent.

## Fetch and inspect

Sync public metadata first so the channel and video IDs are local:

```bash
ytclaw sync @yourchannel
ytclaw analytics sync --channel @yourchannel --since 2026-08-01 --until 2026-09-01 --limit 50
ytclaw analytics show VIDEO_ID
```

Available `--kind` values:

| Kind | Dimension | Stored metrics |
|---|---|---|
| `daily` | day | views, estimatedMinutesWatched, averageViewDuration, averageViewPercentage |
| `retention` | elapsedVideoTimeRatio | audienceWatchRatio, relativeRetentionPerformance |
| `traffic` | insightTrafficSourceType | views, estimatedMinutesWatched |
| `all` | separate requests | all three report types |

Reports are requested per video and paginated. Each completed report commits. Rerun
with the same window to skip saved reports and continue to the next batch. `--video`
selects one local video. `--refresh` replaces saved reports, including empty reports,
when YouTube revises or completes its data. A requested end date is not a guarantee
that YouTube has data through that date. Empty rows remain empty, not fabricated zeros.

Analytics calls are counted separately from the public Data API quota ledger. HTTP
429 and transient server/network failures receive bounded retries. This release does
not automatically schedule owner-report refreshes or create Reporting API bulk jobs.

## Import reach data from Studio

Export a **single video's report by date**, then:

```bash
ytclaw analytics import VIDEO_ID --csv video-by-date.csv
```

The CSV must have `Date` or `day` with ISO dates and at least one supported metric:

| Studio header | Stored field | Unit |
|---|---|---|
| Views | views | count |
| Impressions | impressions | count |
| Impressions click-through rate (%) | impressions_ctr_percent | 0–100 percent |
| Watch time (hours) | watch_time_hours | hours |
| Estimated revenue (USD) | estimated_revenue_usd | USD |

The stored field names are also accepted as input headers. Commas in quoted numeric
cells and percent suffixes are accepted. Empty metric cells remain null. Duplicate
dates, invalid dates, non-finite values, and out-of-range CTR are rejected before saving.
Total rows are skipped. Other locales, currencies, and duration-string columns need
normalizing first. ytclaw does not infer which video a generic CSV belongs to; the
provided VIDEO_ID is your explicit mapping.

Impressions and thumbnail CTR are imported through this CSV path, not requested
through an assumed Analytics API metric combination. Revenue is likewise an import
path here. [YouTube's Reporting API](https://developers.google.com/youtube/reporting)
also documents bulk reach reports; automated bulk job ingestion is future work.

## Compare a change with daily outcomes

```bash
ytclaw history VIDEO_ID
ytclaw analytics compare VIDEO_ID --version 12 --days 7
```

The result compares seven calendar dates before and after the observed version date,
excluding that date itself. It lists missing dates and never substitutes zero for them.
It uses the latest saved API daily row for an overlapping date. The UTC observation
date may not coincide with the API's reporting-day boundary or the actual edit time.

Audience mix, traffic sources, video age, and other changes can affect results.
Use YouTube Studio's experiment result when evaluating a controlled packaging test:

```bash
ytclaw experiment add VIDEO_ID --note "Tested a shorter title" \
  --result "Studio reported no clear winner" --started-at 2026-08-01 --ended-at 2026-08-14
```

These are manual notes. Public thumbnail polling cannot enumerate every concurrent
experiment variant or download an experiment's result automatically.

## API references

- [Supported channel reports and scopes](https://developers.google.com/youtube/analytics/channel_reports)
- [Google installed-app OAuth](https://developers.google.com/identity/protocols/oauth2/native-app)
- [Python installed-app flow](https://googleapis.dev/python/google-auth-oauthlib/latest/reference/google_auth_oauthlib.flow.html)
- [YouTube Studio title and thumbnail experiments](https://support.google.com/youtube/answer/16391400?hl=en)
