# Verification for 0.3.0

## Automated checks

The release suite contains 29 offline tests and one opt-in live test. Without the
optional owner dependencies, the OAuth construction test is also skipped.

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
PYTHON=python3 bash tests/test_smoke.sh
node --check web/app.js
uv build
```

The offline suite covers:

- Durable upload IDs/cursors and completed metadata batches after interruption.
- Full reply pagination, completed-scan pruning, and failed comment coverage.
- Bounded API retries and quota accounting for failed attempts.
- SQLite-enforced read-only queries, including CTE writes and FTS reads.
- Metadata reversions, pinned baselines, image changes at the same URL, legacy migrations,
  chronological stats, channel/date filters, transcript context, and actual growth boundaries.
- Project files compared with API observations even after YAML imports.
- Backup/restore, readable ZIP export, retained images and search, checksum tamper detection,
  overwrite refusal, locks, due jobs, and generated macOS timer configuration.
- Owner report pagination/resume, CSV units and atomic validation, missing-day comparisons,
  and read-only OAuth/PKCE/token-file construction using mocks.
- Local HTTP routes, report download, session-token protection, and Host/Origin checks.

The smoke script uses an isolated configuration and database, so it cannot accidentally
pick up the user's API key for its missing-key check. It exercises import, search,
history, baseline drift, SQL write rejection, export, and restore through the CLI.

Local runs passed under Python 3.10 and 3.14. The built wheel was installed in an
isolated Python 3.10 environment and checked outside the checkout: CLI reports,
web assets, owner-module imports, baseline drift, export, and restore all passed. The GitHub workflow covers Python 3.10,
3.12, and 3.14 on Linux. A configured workflow is not itself proof of a successful
remote run; inspect the Actions result for the relevant commit.

## Public API integration

Opt in with a configured API key:

```bash
YTCLAW_LIVE_CHANNEL=@yourchannel python3 -m unittest discover -s tests -p 'test_live.py'
```

This uses a temporary database, syncs public metadata, checks collection coverage,
and downloads one thumbnail. It makes no YouTube writes.

On 2026-09-07, the check passed for `@CloudYeti`: 178 videos refreshed, 9 Data API quota
units, and one thumbnail archived. The temporary database was removed afterward.
This was not a live transcript, full-comment, or owner-analytics verification.

## Browser checks

The local synthetic demo was checked in Chrome:

- Overview with thumbnail comparisons and observed growth.
- Video library showing older publications independently of the weekly report filter.
- Transcript search with surrounding text and timestamped source links.
- Video details, full transcript, baseline pinning, and persisted experiment notes.
- Narrow viewport rendering with no horizontal page overflow and no console errors.

The viewport override was reset after testing. Demo data is explicitly synthetic.

## Boundaries

Live OAuth consent and authenticated owner-report retrieval require the channel
owner and have not been completed as part of these checks. A mock verifies the flow's
construction, not Google's consent or report responses for a particular account.

OS timer generation is tested without installing a real background job. No production
scheduler, account permissions, or existing channel database were changed by the tests.
Linux timer activation and collection across sleep/login boundaries still need an
operator check on the intended host.
