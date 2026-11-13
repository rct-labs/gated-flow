# Watch incremental events

Then watch the journal: append-only NDJSON at `<repo>/.gate/journal.ndjson`.

| event | meaning |
|---|---|
| `run_start` | workers resolved, budget set |
| `probe` | optional explicitly enabled probe; normal execution skips it (`usable`, `reason`, `seconds`); unused fallbacks are not probed |
| `admit_refused` | task outside the sweet spot; strict mode stops here |
| `attempt_start` | a task went to a worker; `log` is the live transcript |
| `heartbeat` | still alive: elapsed seconds, lines, last output line |
| `task_done` | the runner verified it: commit sha and duration |
| `worker_left_changes` | worker stopped with uncommitted work; `retry` says whether it gets another go |
| `scope_drift` | the task's commits touched undeclared paths; listed under Waiting on you, not a stop |
| `scope_request` / `prompt_too_large` | needs the host; run stops |
| `review_start` / `review_verdict` / `review_skipped` | the one package review (or a checkpoint); a skip reason `<member>:no_access` means that reviewer could not read the repository: fix its tool or sandbox, then `gate.py review --tasks <ids>` |
| `review_findings` | defect records with stable ids and due checkpoints; recorded in `REVIEW-NOTES.md`, never automatic rows |
| `full_acceptance_wait` | another project's full oracle is running on this machine; this one waits for its turn (`holder`), nothing is wrong |
| `full_acceptance` | complete suite selected for delivery or broad/unknown impact: `result`, `count`, `reason` |
| `stage_acceptance` | selected local/module/integration checks or a due-defect stop; never proof of full delivery |
| `needs_approval` | head task touches an irreversible path without approval |
| `tool_disabled` | a CLI hit its quota and was benched |
| `task_end` / `run_end` | outcome per task / stop reason |

### Watch incremental events, not repeated transcripts

Arm a cursor BEFORE launch; initialization skips history. Use one cursor per host,
and never reinitialize during a run (that would discard unseen events):

```
python <flow-home>/gate/watch-journal.py <repo> --init
```

With a persistent Monitor, attach it to the same helper with `--follow`; output is
flushed only for relevant events and it exits after `run_end`. Retire that monitor
before starting a replacement. The existing `watch-journal.sh` remains a legacy
line-number interface; do not stack both watchers or append buffering filters.

Without a Monitor, call `--once`: the byte cursor reads only new complete records
and returns bounded event summaries. Follow `next_poll_s` (60 → 120 → 300 seconds
while idle; reset on events), subject to the host's wait/response limits. For a
host that cannot wait that long, `--once --wait 55` waits internally for an event
for at most 55 seconds; do useful independent work between checks. Keep the active
turn open while the authorized run proceeds. Do not invent a persistent tool or
rely on a tool-shell background loop surviving the session.

`more: true` means unread events remain: drain another bounded page immediately.
`idle` is not success, failure or a reason to inspect the transcript. The cursor
survives sessions; invalid or oversized records and journal resets emit warnings.
New runners include their PID; the watcher emits `run_interrupted` once if that
process exits without `run_end`, and follow mode exits. Treat it as missing
completion evidence, not a failed test or success. Inspect the owner/exit status
once, then resume only the unfinished stage using passed review evidence. For
older runs without a PID, check owner liveness at a suspected stall before waiting
again; do not spend the whole test timeout polling a dead runner.
On a warning, inspect the affected journal segment before treating it as healthy.
On `run_end`, read RUN-REPORT once, bottom-up. Review the worker's bounded final
excerpt only for a failure/scope request, a user status question, or suspected
stall (no journal activity for more than twice the configured heartbeat interval).
Do not repeatedly read full logs, diffs, config or skills. Keep a separate offset
if more worker diagnostics are needed. No acceptance/review rules are weakened.

### Narrating

Report task completion, blockers, review/acceptance and meaningful changes. Combine
adjacent start/end events where possible. No repeated “still running” messages or
minor test-by-test updates; obey any higher-priority host update requirement with
the shortest necessary status, without extra transcript reads merely to fill it.

```
WP-A -> claude, started
WP-A running 12m, last: "pytest tests/test_a.py -q"
WP-A DONE in 56m, commit 8ebf530
review of WP-A, WP-4: pass (independent reviewer), 2 findings recorded
full acceptance: PASS, 561 passed
```

If heartbeats stop for much longer than the interval, say so.
