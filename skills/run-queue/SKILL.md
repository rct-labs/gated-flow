---
name: run-queue
description: >
  $run: the launch protocol for a task queue that is already written and
  admitted. Admission-checked tasks, one worker process per task, a
  machine-checked verdict after each, risk-based review and stage/impact
  acceptance per batch, progress reported as it happens, and a stop at the
  first thing that needs a human. Normally loaded by $flow-run at its launch
  step. Use directly only when the user types $run or /run-queue, or asks to
  run the existing queue as it stands. Not for a single task, and not for
  deciding what to do next: that is $flow-run.
---

# $run: unattended queue execution

You are the launcher and the narrator. The loop belongs to `gate.py run`: a
deterministic script that spawns one CLI process per task, checks each
commit with the pre-commit hook, reviews closed tasks when configured and runs
selected stage checks when the queue has no TODO left, and decides when to stop.

Why the loop is not you: an agent that supervises its own work can be talked
out of stopping and can convince itself a task is done. The verdict comes
from a command's exit code.

## 1. Find the runner and preflight

Use the delivery scope and risk choice made by `flow-run`: ordinary small
tools need acceptance, not an automatically enabled model review. Honor
explicit project requirements. When review is enabled, review once at the
delivery boundary; repairs get targeted verification of original blockers
and directly introduced regressions, never another general audit. Preserve
the original findings and BUG ids in the repair acceptance. Stage checks are
selected by `delivery.stage` and declared impact; queue exhaustion is not
product delivery. A new run does not reset the host-managed delivery budget.
Further repair requires explicit continuation authorization, not merely progress.

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

An unattended task can outlive the interactive session. A foreground call,
and a background tool shell alike, can die and orphan a worker mid-task. Launch
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
| `review_failed:<ids>` | safety/current-stage defects or ambiguous acceptance | consolidate evidenced blockers, preserve BUG ids and tag repairs `origin: review`; targeted verification only |
| `review_loop:<location-or-task>` | recorded repair blockers show no progress | diagnose the common cause before another dispatch; stop; further repair needs explicit continuation authorization |
| `stage_acceptance_failed:<ids>` | a selected stage check failed or due defects remain open | read the named logs/defect records, fix within the affected scope |
| `full_acceptance_failed:<ids>` | the full oracle fails | read `.gate/runs/<run-id>/full-acceptance.log`; repair the cause with a regression test, never blindly repeat it |
| `needs_approval:<id>` | irreversible path without `approved:` | the user approves that task, then relaunch |

Stopping is good. The only unacceptable outcome is a green light that lies.


## Repair continuation

Automatic continuation is OFF by default, including when blockers decrease.
Allow one consolidated high-risk/core-flow repair batch and targeted verification;
then stop if still blocked. Ordinary findings are recorded for later maintenance.
Further repair requires explicit user authorization, named tasks and an expiry;
spare budget or a generic continuation request is not authorization. Follow the
continuation contract in `flow-run` and `docs/autonomy.md` before dispatch.
