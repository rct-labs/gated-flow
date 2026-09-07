---
name: run-queue
description: >
  $run — run the project's task queue unattended: admission-checked tasks, one
  worker process per task, a machine-checked verdict after each, progress
  reported as it happens, and a stop at the first thing that needs a human.
  Use when the user says run unattended, run the queue, keep executing, or
  continue until blocked, or types $run or /run-queue; also responds to
  equivalent requests in other languages. (Named run-queue because some hosts
  have a built-in `run` skill that launches apps.) Not for a single task —
  invoke the project's own next-session skill for that.
---

# $run — unattended queue execution

You are the launcher and the narrator. The loop belongs to `gate.py run`: a
deterministic script that spawns one CLI process per task, runs the
acceptance command itself, and decides when to stop.

Process and conversation boundaries are separate. An opt-in `session_reuse`
policy lets adjacent Codex tasks with the same explicit
`<!-- task:ID session: docs/work/<package> -->` reuse a conversation. Gate owns
the session ID, eligibility checks, budgets, and fresh-session fallback. Never
resume by `--last`, carry a checkpoint into a new run, or manually dispatch a
second task in a worker turn. See the runner's Sessions report for actual reuse.

**Why the loop is not you:** an agent that supervises its own work can be talked
out of stopping and can convince itself a task is done. The authors measured
that failure at roughly 60% of tasks in an earlier orchestrator and retired it
over that. The verdict comes from a command's exit code.

## 1. Find the runner and preflight

`flow home` prints the checkout directory of this workflow; `gate.py` is
`<home>/gate/gate.py`. A project may also carry its own `gate/gate.py`. If
neither exists, say so and stop.

```
flow doctor --repo <project>
flow admit  --repo <project>
```

(`flow` is the PATH shim for gate.py/audit.py; `python <gate.py> doctor|admit`
is the same thing if the shim is missing.)

Show both outputs. **Stop and report** if the pre-commit hook is not installed
(an unattended run without the gate is the exact failure that killed the
previous orchestrator), if the queue parses to zero tasks, or if the head task
is `BLOCKED`.

The `admit` report is the sweet-spot check (small / isolated / verifiable —
the third leg of the design). If the **head** task is `REFUSE` or
`UNDECLARED`, tell the user before launching: in advisory mode the run will
proceed with a warning; with `strict_admit` it will stop immediately. Offer to
add the missing scope line first:

```
<!-- task:WP-7 files: src/foo.py, tests/test_foo.py -->
```

A queue table may also carry a `worker` column (`claude` / `codex` / `kimi` /
`grok`). gate dispatches that row to the named CLI first; `attempt_start`
journal lines then carry `pin` and `pinned`, and the run report shows both the
dispatched worker and the pin, so a fallback is visible. `admit` refuses an
unknown id as `unknown-worker`.

## 2. Confirm once

Use the task count already authorized by the user or supplied by flow-run;
otherwise use the configured default. Check repository state for conflicting
work. Do not ask again for decisions already settled in this conversation.

## 3. Detach it from your session — never foreground, never a tool shell

A task takes 30–60 minutes. A foreground call would leave the user staring at a
silent terminal, which is the complaint this design exists to fix. A
**background shell command is just as wrong**: your tool shell may live in a
Windows Job Object with kill-on-close, so when the harness reaps that shell the
runner dies with it, orphaning a worker mid-task and leaving the queue row
`IN_PROGRESS`. `Start-Process` does not save you — the child stays inside the
job. The authors measured this twice; once a worker 70 minutes in,
implementation finished, was killed one step before its commit.

Launch it through Task Scheduler, which owns the process instead of you:

```
pwsh -NoProfile -File "$(flow home)/gate/launch-detached.ps1" `
     -Repo <project> -MaxTasks <N> -TaskName gate-<slug>
```

That wraps `supervise.ps1`, which restarts the runner if it vanishes without a
report and honours every stop the gate decides. If the scripts are not beside
`gate.py`, or the host is not Windows, fall back to a background
`python <gate.py> run --repo <project> --max-tasks <N>` (for example under
`nohup`) and say plainly that the run may not survive this session.

Then **poll the journal** and narrate. The journal is append-only NDJSON at
`<project-repo-root>/.gate/journal.ndjson`, one object per event:

| event | meaning |
|---|---|
| `run_start` | workers resolved, budget set |
| `admit_refused` | a task is outside the sweet spot; strict mode stops here |
| `attempt_start` | a task went to a worker; `log` points at the live transcript |
| `heartbeat` | still alive: elapsed seconds, lines so far, last line of output |
| `task_done` | the runner verified it: commit sha and duration |
| `judge_start` / `judge_verdict` | read-only judge round: `score`, `verdict`, `member` (a `judge.chain` entry) |
| `revision_start` / `revision_end` | a judge-driven fix by a *different* worker; `outcome` is the runner's own re-verify |
| `judge_escalated` | cap, no progress, or the judge asked for a human; run stops |
| `judge_skipped` | whole judge chain down; task stays DONE unreviewed (report says so) |
| `probe` | usage probe result per CLI at run start (`usable`, `reason`) |
| `needs_approval` | head task touches an irreversible path without an approval line |
| `task_end` | outcome for that task (+ `judge` final) |
| `tool_disabled` | a CLI hit its quota and was benched |
| `run_end` | stop reason |
| `session_checkpoint` | fresh/resumed Codex session, usage, and whether it may be reused |

### Arm the watchers before you narrate — and validate the filter first

**gate.py writes `json.dumps` output: `"event": "run_start"`, with a space
after the colon.** A grep for `"event":"..."` matches nothing, forever, and a
watcher that matches nothing is indistinguishable from a run that is going
fine. Measured: one run finished both of its tasks and the session reported
nothing, because the filter had no space. Always use the tolerant form:

```
'"event": ?"(run_start|attempt_start|task_done|judge_verdict|revision_start|revision_end|judge_escalated|judge_skipped|needs_approval|task_end|tool_disabled|admit_refused|run_end)"'
```

**Validation rule — never arm an unvalidated filter.** Before starting any
watcher, dry-run its pattern against journal lines from an earlier run and
require at least one match. Zero historical matches means the filter is wrong,
not that the journal is quiet:

```
grep -cE '<pattern>' <repo>/.gate/journal.ndjson     # must be > 0
```

Take the pre-launch line count **before** launching, and scope every later read
past it — the journal already holds `run_end` lines from every earlier run, and
a watcher that reads from line 1 reports a run that never started:

```
N=$(wc -l < <repo>/.gate/journal.ndjson)          # BEFORE launching
```

**Where a persistent `Monitor` exists (Claude Code, and any host with one), it
is the channel.** Arm exactly one, non-negotiable, and put `run_end` inside the
filter so the same watcher narrates the run *and* wakes you at the end:

```
Monitor(persistent: true, command:
  bash "$(flow home)/gate/watch-journal.sh" <repo> $N)
```

`watch-journal.sh` is the canonical pipeline: `tail -f` → one `grep --line-buffered`
(truncation is done inside `grep -o`). **Never append `cut`, `awk`, `sed`, `head`
or any other stage after it** — those are block-buffered on a pipe, so every
event sits in their buffer and the Monitor delivers nothing while the run
finishes in silence. The authors measured this: two runs completed with zero
notifications because the ad-hoc pipeline ended in `cut -c1-300`. Before
arming, run the selftest — it replays history through the *same* pipeline and
must print `OK`:

```
bash "$(flow home)/gate/watch-journal.sh" <repo> --selftest     # must exit 0
```

Leave `heartbeat` out of the pattern unless the user asks for ticks; five-minute
pings are noise. Call `TaskStop` on it once you have read `RUN-REPORT.md`, or a
later run in the same session narrates into stale context.

**Do not arm a `run_in_background` Bash `until … sleep` loop as the wake
channel.** A gate run lasts hours and that tool caps at ten minutes, so the loop
is reaped mid-run and reports nothing. The authors measured this twice in one
session: killed at exactly 600 s, then killed again 600 s after being re-armed.
Re-arming it every ten minutes is noise for no coverage the Monitor does not
already give. It is only correct where a run is known to be shorter than the
cap, or as the Codex fallback below.

**Hosts without `Monitor`.** Arm the Bash loop instead and accept the cap:

```
until tail -n +$((N+1)) <repo>/.gate/journal.ndjson | grep -qE '"event": ?"run_end"'; do sleep 60; done
```

**Codex lifecycle rule.** A unified `exec_command` / `write_stdin` session is
pull-based: receiving a session id does not make it callback into a later
conversation turn. In Codex, keep the current assistant turn open and poll the
journal at intervals no longer than 60 seconds until a post-launch `run_end` is
observed. Do not send a final response while the run is active; doing so
silently abandons narration. End the turn early only when a real
session-independent `notify_cmd` is already configured and the user explicitly
accepts asynchronous handoff. In that case say plainly that this chat will not
automatically receive the terminal report.

`notify_cmd` in `.gate/config.json` (for example a chat notifier) is a second,
session-independent channel that reaches the user whether or not your session
is alive. Do not treat it as a substitute for the watcher — it tells the user,
not you.

**On any status question, read `.gate/journal.ndjson` and `.gate/RUN-REPORT.md`
before answering.** Never answer "still running" from the absence of
notifications.

### Narrating

Read only the lines you have not reported yet and post a short line per new
event. Something like:

```
WP-A → claude, started
WP-A running 12m, last: "uv run pytest tests -q"
WP-A DONE in 56m, commit 8ebf530, 561 passed
WP-A judged 72 (revise, fable) → revision 1 via codex
WP-A judged 91 (pass, fable)
WP-4 → claude, started
```

Between events, stay quiet. Do not summarise, do not speculate about progress,
do not open the worker log unless something failed. If a heartbeat stops
arriving for much longer than the heartbeat interval, say so plainly — that is
the signal the user actually needs.

## 4. Report the outcome

From `.gate/RUN-REPORT.md`. Lead with its **Waiting on you** section (empty on
a clean run), then how many tasks closed and why it stopped, then the
**Judge** table (rounds / scores / workers) and the per-task table, then
anything left uncommitted. If a task did not close, quote the last lines of
its log from `.gate/runs/<run-id>/` instead of guessing why.

## Never

- **Never mark a task done yourself.** You may not edit the queue, the
  acceptance file, or any verify command. Only a worker closes a task, and only
  the gate lets that commit through.
- **Never `git commit --no-verify`**, and never suggest it as a way past a
  blocked commit. The block is the product.
- **Never take over an `IN_PROGRESS` task.** Report and stop.
- **Never push, merge, or open a PR.**
- Never restart a run because it stopped early, and never raise `--max-tasks` to
  push past a stop. It stopped for a reason.
- Never weaken admission to get a task through: shrink the task, don't grow
  `max_task_files`.

## Reading a stop reason

| stop | what it means | what the user should do |
|---|---|---|
| `queue_empty` | nothing eligible left | nothing — the queue is done |
| `blocked:<id>` | head task needs decisions | answer its blocking questions, set it TODO |
| `in_progress:<id>` | someone holds it, or a runner died | check `git status` before restarting |
| `admit_refused:<id>` | head task is outside the sweet spot (strict mode) | re-shape it: split, declare scope, or take it interactive via $flow |
| `budget` / `run_timeout` | hit the configured limit | start another run |
| `no_progress:<id>` | two attempts failed identically | read the log; the task or its acceptance is wrong |
| `not_done:<id>` | worker exited without closing the task | usually a rejected commit — read the log |
| `worker_incomplete:<id>` | worker returned while background work was pending | read the log; do not dispatch into the still-active work |
| `worker_left_changes:<id>` | worker exited with new partial changes | inspect the tree; never hand it to a concurrent retry |
| `timeout:<id>` | worker exceeded `task_timeout_s`; its process tree was killed | check for a half-finished tree |
| `no_workers` | every CLI is quota-disabled (reactively, or by the run-start probe) | wait for the cooldown; see `.gate/tool-status.json` |
| `judge_escalated:<id>` | task is DONE by the gate but the judge could not get it to `pass_score` within 2 revisions, or asked for a human | read the Judge findings; fix / accept / re-shape — the user's call |
| `revision_broke_verify:<id>` | a judge-driven revision commit fails acceptance or left the tree dirty | inspect `git log` / `git status` before any run; nothing is auto-reset |
| `needs_approval:<id>` | head task touches an `irreversible_globs` path without `<!-- task:ID approved: … -->` | user approves that task explicitly, then relaunch |

`no_progress` and `not_done` are not failures of the system. They are the system
refusing to record work it cannot verify.
