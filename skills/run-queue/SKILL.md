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

# $run — launch and observe the engine

For separately installed skills, `flow home` locates repository documents:
resolve shared rules at `<home>/docs/autonomy.md` and other repository links
against that home. Within-skill references stay relative to this skill.

Run only the existing admitted queue within its authorized task count. Decisions
about new work belong to `flow-run`; a single task worker never launches a queue.
Read [shared workflow rules](../../docs/autonomy.md) for model routing, scope,
approval, risk review, repair and cumulative limits; loading this skill adds no
authority or budget.

Load conditional procedures as needed:

1. [Preflight](references/preflight.md): reuse unchanged admission and hook checks.
2. [Detached launch](references/launch.md): Linux user systemd transient service
   when available; Windows Task Scheduler. Never install a scheduler.
3. [Watch](references/watch.md): one incremental journal cursor, meaningful events.
4. [Outcome and stops](references/outcome.md): read the saved report, reconcile
   unfinished state, return decisions to the host.

`gate.py run` owns dispatch, one process per task attempt, verification, review
and stage checks. Never mark DONE as host, bypass the hook, take over an
IN_PROGRESS writer, raise limits to bypass a stop, or push/merge/open a PR.
Missing required review remains pending across restart; passing tests and DONE
alone cannot settle the package. Preserve `.gate/` as the only machine state.
