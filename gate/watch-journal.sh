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
pattern='"event": ?"(run_start|attempt_start|task_done|review_start|review_verdict|review_skipped|review_findings|stage_acceptance|full_acceptance|full_acceptance_wait|scope_request|prompt_too_large|needs_approval|task_end|tool_disabled|admit_refused|run_end)"'
filter() { grep --line-buffered -oE "^.{0,320}" | grep --line-buffered -E "$pattern"; }
if [ "$mode" = "--selftest" ]; then
  n=$(tail -n 400 "$journal" | filter | wc -l)
  if [ "$n" -gt 0 ]; then echo "watch-journal selftest: OK ($n historical events pass the pipeline)"; exit 0; fi
  echo "watch-journal selftest: FAIL — pipeline emits nothing on history; do not arm"; exit 1
fi
N="$mode"
# Why the pid plumbing: 2026-09-19 a host held 23 dead watchers per repo set.
# `tail -f` on a quiet journal never writes, so it never sees the closed pipe
# and outlives the session that armed it. Two guards: tail follows the life of
# the process that launched this script, and arming reaps the previous watcher
# of the same journal (newest wins).
pidfile="$repo/.gate/watch-journal.pid"
if [ -f "$pidfile" ]; then
  old=$(tr -cd '0-9' < "$pidfile")
  if [ -n "$old" ] && grep -qa 'journal.ndjson' "/proc/$old/cmdline" 2>/dev/null; then kill "$old" 2>/dev/null; fi
fi
follow=()
if [ "$PPID" -gt 1 ] && kill -0 "$PPID" 2>/dev/null && tail --help 2>&1 | grep -q -- '--pid'; then
  follow=(--pid="$PPID")
fi
exec 3< <(exec tail ${follow[@]+"${follow[@]}"} -n +$((N+1)) -f "$journal")
echo $! > "$pidfile"
filter <&3
