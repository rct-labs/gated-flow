#!/usr/bin/env bash
# Canonical gate journal watcher for Monitor / narration channels.
#
#   watch-journal.sh <repo> <N>            follow events after line N (line-buffered, no cut/awk)
#   watch-journal.sh <repo> --selftest     replay history through the SAME pipeline; exit 1 if nothing comes out
#
# Why a script: 2026-09-03 two gate runs finished without waking the session
# because the ad-hoc pipeline ended in `cut -c1-300` (not line-buffered) —
# events sat in a buffer forever. The pipeline below has exactly one stage
# after tail and it is line-buffered; truncation happens inside grep -o.
set -u
repo="${1:?repo}"; mode="${2:?N or --selftest}"
journal="$repo/.gate/journal.ndjson"
pattern='"event": ?"(run_start|attempt_start|task_done|judge_verdict|revision_start|revision_end|judge_escalated|judge_skipped|needs_approval|task_end|tool_disabled|admit_refused|run_end)"'
filter() { grep --line-buffered -oE "^.{0,320}" | grep --line-buffered -E "$pattern"; }
if [ "$mode" = "--selftest" ]; then
  n=$(tail -n 400 "$journal" | filter | wc -l)
  if [ "$n" -gt 0 ]; then echo "watch-journal selftest: OK ($n historical events pass the pipeline)"; exit 0; fi
  echo "watch-journal selftest: FAIL — pipeline emits nothing on history; do not arm"; exit 1
fi
N="$mode"
tail -n +$((N+1)) -f "$journal" | filter
