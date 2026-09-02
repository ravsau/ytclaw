#!/usr/bin/env bash
# Offline smoke test on a fixture. Needs python3 + pyyaml. PYTHON=... to pick an interpreter.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
export YTCLAW_DB=$(mktemp -d)/t.sqlite
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
echo "ok: all smoke checks passed"
