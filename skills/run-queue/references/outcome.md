# Report outcome and diagnose stops

## 4. Report the outcome

From `.gate/RUN-REPORT.md`: lead with **Waiting on you** (empty on a clean
run), then how many tasks closed and why it stopped, then **Review** and
**Full acceptance**, then the task table and anything left uncommitted. If a
task did not close, quote the last lines of its log from `.gate/runs/<run-id>/`.

The report also records Efficiency. `flow metrics --last 5 --repo <project>`
compares runs without model calls or tests. Legacy missing measures are unknown;
worker time includes its checks. Read metrics when assessing workflow cost, not
on every poll. Do not run `flow usage` at startup; normal execution dispatches useful work directly.

## Never

- Never mark a task done, edit the queue status, or touch a verify command.
- Never `git commit --no-verify`, and never suggest it. The block is the product.
- Never take over an IN_PROGRESS task. Never push, merge or open a PR.
- Never turn a review finding into a queue row. `REVIEW-NOTES.md` is a record.
- Never put `flow resume` / `flow recall` output, CONTEXT or host history into
  a worker prompt or queue row; workers get the bounded task packet only.
  After `run_end` the host (`flow-run`) saves state with `flow checkpoint`.
- Never restart a run just because it stopped, and never raise `--max-tasks`,
  `max_model_calls` or `max_task_files` to push past a stop.

## Reading a stop reason

| stop | meaning | what to do |
|---|---|---|
| `queue_empty` | nothing eligible left | nothing |
| `budget` / `budget:model_calls` / `run_timeout` | a configured limit | stop; preserve cumulative limits, obtain authorization for any increase |
| `blocked:<id>` | head task needs decisions | answer them, set it TODO |
| `in_progress:<id>` | someone holds it, or a runner died | check `git status` before restarting |
| `admit_refused:<id>` | outside the sweet spot (strict) | split or declare scope |
| `no_progress:<id>` / `not_done:<id>` | the system refused work it could not verify | read the log |
| `worker_left_changes:<id>` | two attempts left the tree dirty without closing | read the log, finish or reset by hand |
| `scope_request:<id>` | the worker asked for a scope decision | host reconciles authorized scope before widening or splitting |
| `prompt_too_large:<id>` | packet over `worker_packet_max_bytes` | remove unrelated context or split; preserve acceptance and stop conditions |
| `timeout:<id>` | worker exceeded `task_timeout_s` | check for a half-finished tree |
| `no_workers` | every CLI is benched | wait for the cooldown (`.gate/tool-status.json`) |
| `review_unavailable:<ids>` | required independent review missing | retain pending boundary across restart; restore the authorized reviewer, never settle from tests alone |
| `review_failed:<ids>` | safety/current-stage defects or ambiguous acceptance | consolidate evidenced blockers, preserve BUG ids and tag repairs `origin: review`; targeted verification only |
| `review_loop:<location-or-task>` | recorded repair blockers show no progress | stop repeating the approach; diagnose once and require a different evidenced fix within the authorized goal and cumulative limits |
| `stage_acceptance_failed:<ids>` | a selected stage check failed or due defects remain open | read the named logs/defect records, fix within the affected scope |
| `full_acceptance_failed:<ids>` | the full oracle fails | read `.gate/runs/<run-id>/full-acceptance.log`; repair the cause with a regression test, never blindly repeat it |
| `needs_approval:<id>` | irreversible path without `approved:` | the user approves that task, then relaunch |

Stopping is good. The only unacceptable outcome is a green light that lies.
