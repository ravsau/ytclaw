#!/usr/bin/env bash
# Offline smoke test on a fixture. Needs python3 + pyyaml. PYTHON=... to pick an interpreter.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
YTCLAW_TEST_DIR=$(mktemp -d)
trap 'rm -rf -- "$YTCLAW_TEST_DIR"' EXIT
export YTCLAW_DB="$YTCLAW_TEST_DIR/t.sqlite"
export YTCLAW_HOME="$YTCLAW_TEST_DIR/config"
$PY ytclaw.py import --yaml tests/fixtures | grep -q '"yaml_imported": 1'
$PY ytclaw.py import --yaml tests/fixtures | grep -q '"yaml_unchanged": 1'
$PY ytclaw.py search "pricing" --in transcripts | grep -q 'v=vid1&t=3s'
$PY ytclaw.py search "thank" --in comments | grep -q '@a'
$PY ytclaw.py video vid1 | grep -q '"transcript_segments": 2'
$PY ytclaw.py video vid1 | grep -q '"comment_count_local": 2'
$PY ytclaw.py stats | grep -q '"videos": 1'
$PY ytclaw.py stats | grep -q '"quota_daily_limit": 10000'
! $PY ytclaw.py sql "delete from videos" 2>/dev/null
( YOUTUBE_API_KEY= $PY ytclaw.py sync @nobody 2>&1 || true ) | grep -qi 'no API key'
$PY ytclaw.py baseline vid1 | grep -q '"baseline_version"'
$PY ytclaw.py drift vid1 | grep -q '"drifted": false'
$PY ytclaw.py history vid1 | grep -q '"metadata"'
! $PY ytclaw.py sql "WITH x AS (SELECT 1) DELETE FROM videos" 2>/dev/null
$PY ytclaw.py backup "$YTCLAW_TEST_DIR/backup.sqlite" | grep -q '"integrity": "ok"'
$PY ytclaw.py export "$YTCLAW_TEST_DIR/export.zip" | grep -q 'ytclaw-archive-v1'
$PY ytclaw.py --db "$YTCLAW_TEST_DIR/restored.sqlite" restore "$YTCLAW_TEST_DIR/export.zip" | grep -q '"integrity": "ok"'
$PY ytclaw.py --db "$YTCLAW_TEST_DIR/restored.sqlite" search pricing --in transcripts | grep -q 'v=vid1&t=3s'
echo "ok: all smoke checks passed"
