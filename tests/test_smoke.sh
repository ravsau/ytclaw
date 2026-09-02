#!/usr/bin/env bash
# End-to-end smoke test on a fixture. Needs python3 + pyyaml.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
export YTCLAW_DB=$(mktemp -d)/t.sqlite
$PY ytclaw.py import --yaml tests/fixtures | grep -q '"yaml_imported": 1'
$PY ytclaw.py import --yaml tests/fixtures | grep -q '"yaml_unchanged": 1'
$PY ytclaw.py search "pricing" --in transcripts | grep -q 'youtu.be/vid1?t=3'
$PY ytclaw.py search "thank" --in comments | grep -q '@a'
$PY ytclaw.py video vid1 | grep -q '"transcript_segments": 2'
$PY ytclaw.py video vid1 | grep -q '"comment_count_local": 2'
$PY ytclaw.py stats | grep -q '"videos": 1'
! $PY ytclaw.py sql "delete from videos" 2>/dev/null
echo "ok: all smoke checks passed"
