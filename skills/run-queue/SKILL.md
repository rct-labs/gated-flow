---
name: run-queue
description: >
  $run: the launch protocol for a task queue that is already written and
  admitted. Admission-checked tasks, one worker process per task, a
  machine-checked verdict after each, one review and one full acceptance
  per package, progress reported as it happens, and a stop at the
  first thing that needs a human. Normally loaded by $flow-run at its launch
  step. Use directly only when the user types $run or /run-queue, or asks to
  run the existing queue as it stands. Not for a single task, and not for
  deciding what to do next: that is $flow-run.
---

# $run: unattended queue execution

You are the launcher and the narrator. The loop belongs to `gate.py run`: a
deterministic script that spawns one CLI process per task, checks each
commit with the pre-commit hook, reviews the closed tasks once and runs the
full oracle once when the queue has no TODO left, and decides when to stop.

Why the loop is not you: an agent that supervises its own work can be talked
out of stopping and can convince itself a task is done. The verdict comes
from a command's exit code.

## 1. Find the runner and preflight

`flow home` prints the checkout; `gate.py` is `<home>/gate/gate.py`.

```
flow doctor --repo <project>
flow admit  --repo <project>
```

Show both. Stop and report if the pre-commit hook is not installed, the
queue parses to zero tasks, or the head task is BLOCKED. If the head task is
REFUSE or UNDECLARED, say so before launching: advisory mode proceeds with a
warning, `strict_admit` stops. Offer to add the missing scope line:

```
<!-- task:WP-7 files: src/foo.py, tests/test_foo.py -->
```

## 2. Confirm once

Use the task count already authorized or supplied by flow-run; otherwise
the configured default. Do not ask again for decisions already settled.

## 3. Detach it from your session

A task takes 30 to 60 minutes. A foreground call, and a background tool
shell alike, can die with your session and orphan a worker mid-task. Launch
through Task Scheduler, which owns the process:

```
pwsh -NoProfile -File "$(flow home)/gate/launch-detached.ps1" `
     -Repo <project> -MaxTasks <N> -TaskName gate-<slug>
```

That wraps `supervise.ps1`, which restarts the runner if it vanishes without
a report and honours every stop the gate decides. Off Windows, fall back to
`nohup python <gate.py> run --repo <project> --max-tasks <N>` and say the
run may not survive this session.

Then watch the journal: append-only NDJSON at `<repo>/.gate/journal.ndjson`.

| event | meaning |
|---|---|
| `run_start` | workers resolved, budget set |
| `probe` | usage probe per CLI (`usable`, `reason`) |
| `admit_refused` | task outside the sweet spot; strict mode stops here |
| `attempt_start` | a task went to a worker; `log` is the live transcript |
| `heartbeat` | still alive: elapsed seconds, lines, last output line |
| `task_done` | the runner verified it: commit sha and duration |
| `worker_left_changes` | worker stopped with uncommitted work; `retry` says whether it gets another go |
| `scope_drift` | the task's commits touched undeclared paths; listed under Waiting on you, not a stop |
| `scope_request` / `prompt_too_large` | needs the host; run stops |
| `review_start` / `review_verdict` / `review_skipped` | the one package review (or a checkpoint) |
| `review_findings` | findings below high, full text; recorded in the report and `REVIEW-NOTES.md`, never rows |
| `full_acceptance` | the full oracle after the review, once per package: `result`, `count` |
| `needs_approval` | head task touches an irreversible path without approval |
| `tool_disabled` | a CLI hit its quota and was benched |
| `task_end` / `run_end` | outcome per task / stop reason |

### Arm one validated watcher

gate.py writes `"event": "run_start"` with a space after the colon. Use the
tolerant form and validate it against an earlier run before arming:

```
'"event": ?"(run_start|attempt_start|task_done|worker_left_changes|scope_request|prompt_too_large|review_verdict|review_skipped|full_acceptance|needs_approval|task_end|tool_disabled|admit_refused|run_end)"'
grep -cE '<pattern>' <repo>/.gate/journal.ndjson     # must be > 0
N=$(wc -l < <repo>/.gate/journal.ndjson)             # BEFORE launching
```

Where a persistent `Monitor` exists, arm exactly one with `run_end` in the
filter through the canonical pipeline, and run its selftest first:

```
bash "$(flow home)/gate/watch-journal.sh" <repo> --selftest     # must exit 0
Monitor(persistent: true, command: bash "$(flow home)/gate/watch-journal.sh" <repo> $N)
```

Never append `cut`, `awk`, `sed` or `head` after it (block-buffered, events
vanish). Never use a `run_in_background` Bash `until` loop as the wake
channel on a multi-hour run; it is reaped at ten minutes. Hosts without a
Monitor poll `tail -n +$((N+1)) ... | grep -qE '"event": ?"run_end"'` every
60 seconds and keep the turn open. On any status question read the journal
and the report before answering.

### Narrating

One short line per new event, nothing between events:

```
WP-A -> claude, started
WP-A running 12m, last: "pytest tests/test_a.py -q"
WP-A DONE in 56m, commit 8ebf530
review of WP-A, WP-4: pass (fable), 2 findings recorded
full acceptance: PASS, 561 passed
```

If heartbeats stop for much longer than the interval, say so.

## 4. Report the outcome

From `.gate/RUN-REPORT.md`: lead with **Waiting on you** (empty on a clean
run), then how many tasks closed and why it stopped, then **Review** and
**Full acceptance**, then the task table and anything left uncommitted. If a
task did not close, quote the last lines of its log from `.gate/runs/<run-id>/`.

## Never

- Never mark a task done, edit the queue status, or touch a verify command.
- Never `git commit --no-verify`, and never suggest it. The block is the product.
- Never take over an IN_PROGRESS task. Never push, merge or open a PR.
- Never turn a review finding into a queue row. `REVIEW-NOTES.md` is a record.
- Never restart a run just because it stopped, and never raise `--max-tasks`,
  `max_model_calls` or `max_task_files` to push past a stop.

## Reading a stop reason

| stop | meaning | what to do |
|---|---|---|
| `queue_empty` | nothing eligible left | nothing |
| `budget` / `budget:model_calls` / `run_timeout` | a configured limit | start another run if the user wants more |
| `blocked:<id>` | head task needs decisions | answer them, set it TODO |
| `in_progress:<id>` | someone holds it, or a runner died | check `git status` before restarting |
| `admit_refused:<id>` | outside the sweet spot (strict) | split or declare scope |
| `no_progress:<id>` / `not_done:<id>` | the system refused work it could not verify | read the log |
| `worker_left_changes:<id>` | two attempts left the tree dirty without closing | read the log, finish or reset by hand |
| `scope_request:<id>` | the worker asked for a scope decision | widen the declared files or split |
| `prompt_too_large:<id>` | packet over `worker_packet_max_bytes` | shorten the spec acceptance or split |
| `timeout:<id>` | worker exceeded `task_timeout_s` | check for a half-finished tree |
| `no_workers` | every CLI is benched | wait for the cooldown (`.gate/tool-status.json`) |
| `review_failed:<ids>` | review reported a high finding or did not pass | read the findings; a `score 0 / escalate / no findings` verdict means the reviewer could not run its tools: fix the tool, then `gate.py review --tasks <ids>`; otherwise admit ONE repair row for all the findings, tagged `<!-- task:ID origin: review -->` |
| `review_loop:<file>` | the repair row (`origin: review`) drew another high finding on a file it declared | no second repair row; take the findings to the user: revise the spec, or accept the risk |
| `full_acceptance_failed:<ids>` | the full oracle fails after the run | admit one repair row |
| `needs_approval:<id>` | irreversible path without `approved:` | the user approves that task, then relaunch |

Stopping is good. The only unacceptable outcome is a green light that lies.
