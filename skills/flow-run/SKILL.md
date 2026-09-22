---
name: flow-run
description: >
  The default entry for this workflow: inspect the project's current state,
  decide the next work as the host CLI, write an admission-ready TASK_QUEUE,
  then unattended-run it. It loads $flow and $run for their steps, so the
  user never has to pick between them. Optional worker pin: claude /
  claude-code, codex, kimi, grok / grok-build, pi. No pin means the CLI
  running this skill. Use when the user types $flow-run or /flow-run, or says
  check progress, continue, keep going, keep executing, run unattended,
  continue until blocked, flow run, or continue and finish; also responds to
  equivalent requests in other languages. Works from
  Claude Code, Codex CLI, Kimi CLI and Grok Build. Analysis-only, plan-only
  and paused-business requests do not authorize execution.
---

# $flow-run: inspect, decide, queue, run

You are the host CLI. You survey, you decide, you write the queue.
`gate.py run` executes. You never mark a task DONE yourself.

This skill is a router. Load the others when that step starts:
shape / brief / spec / CONTEXT / queue format -> `flow`;
launch, journal watch, stop reasons -> `run-queue`;
a fork that is uncertain and high-impact -> `model-debate`. It picks its own
profile: `focused` by default, `debate` only when the fork is truly hard to
undo. Its verification and dispute rounds are part of the decision; do not
drop them to finish sooner.
`flow home` prints the checkout; the design is `<home>/docs/autonomy.md`.

## 0. Worker pin

| user said | worker id |
|---|---|
| claude, claude code | `claude` |
| codex, codex cli | `codex` |
| kimi, kimi cli | `kimi` |
| grok, grok build | `grok` |
| pi, openrouter, or an OpenRouter model id | `pi` (set `worker_models.pi`) |

No match: the CLI you are running in (`grok` if `GROK_HOME`, `claude` if
`CLAUDECODE`, `kimi` if `KIMI_HOME`, `codex` if `CODEX_HOME`, else the first
of claude, codex, kimi, grok, pi on PATH). Say one line:
`host: <this CLI> / worker: <id>`. Unknown name: list the ids and stop.

## 1. Inspect (always)

Read, do not invent: `CONTEXT.md`, the configured queue, `git status` and
`git log --oneline -10`, the tail of `.gate/journal.ndjson` and
`.gate/RUN-REPORT.md` if present, then `flow doctor --repo .` and
`flow admit --repo .`. Tell the user five facts in their language: goal, now,
last checkpoint, next step, anything waiting on a human. Then continue.

Do not launch when the pre-commit hook is missing, the head task is BLOCKED
or IN_PROGRESS, or the worktree is dirty with work you did not make. No
`verify_cmd`: say admission will refuse code tasks until the user sets one.

## 2. Decide

Read `<home>/docs/autonomy.md` for the stage/check selection and defect record
contract. Set `delivery.stage` for the next batch, before launch:

| stage | completion check |
|---|---|
| development | runnable progress; local tests, ordinary defects recorded |
| module | module behavior and affected callers |
| integration | interfaces and critical user flows; batch due defects |
| delivery | complete acceptance on the candidate and required risk review |

Queue exhaustion closes a batch, not the product. Never run every module's
batch as a delivery. Conversely, a scoped PASS is not a delivery PASS.
Advance stages based on the planned milestone, not to escape a failed check.

Choose the cheapest delivery process that fits the actual risk:

- Small, reversible local tools: implement a usable end-to-end slice, run its
  acceptance, deliver. Leave `judge.enabled` false by default; do not add a
  debate, scorecard, hardening audit or a review for each module. Keep existing
  explicit project review requirements; never toggle a live run's config.
- Sensitive data, permissions, money, destructive operations or difficult
  rollback: keep one focused review of those invariants at the delivery
  boundary. Risk attaches to behavior, not every file in that project.
  A redactor needs deletion/privacy checks; ordinary UI and dictionary
  plumbing do not each need a fresh security audit.
- Explicit comprehensive audit requests retain their requested scope.

Fix observable acceptance, supported inputs, non-goals and a reasonable
delivery time budget in the existing spec before dispatch. Carry the start
time, time already spent and remaining allowance in CONTEXT across restarts.
This is a host planning limit, not an engine-enforced cumulative timer. Never
reset it with a new run, task ID or spec revision. At the limit, stop automatic
dispatch and report usable work and actual blockers; never label an unsafe
or failing result complete. Choose the budget for the scope and user constraint,
not a universal two-hour limit.

Prefer vertical tasks that produce a runnable user flow early. Necessary
implementation slices share a delivery acceptance; do not split each module
into a separately reviewed package merely to satisfy a file cap. Cleanup and
future extensibility stay outside the current delivery.

Survey only the code and docs the next step touches. If CONTEXT section 4 is
already a testable slice, use it; otherwise write or update
`docs/work/<id>/brief.md` and `spec.md` from the flow templates and rebuild
`CONTEXT.md` (at most 200 lines, five questions, never append). Do not ask
the user to pick among options this skill exists to settle; record the choice
under Decisions in the spec.

The survey ending here is a phase boundary: `CONTEXT.md` is fresh, so the
run can start from a new session if this one is heavy. `flow` has the rule
(Hand off at a phase boundary); it is advice, never a required step.

## 3. Queue

Append TODO rows in gate format, one independently verifiable and reversible
behaviour per row, implementation and tests together. Each row needs:

```
| N | ID | name | `TODO` | 12 passed | 14 passed |
<!-- task:ID files: src/a.py, tests/test_a.py -->
<!-- task:ID verify: {"cmd": "pytest tests/test_a.py -q", "timeout_s": 900} -->
<!-- task:ID impact: local -->
```

At most 6 files, no overlap with other TODO rows unless
`<!-- task:ID2 after: ID1 -->` orders them, no human-decision wording in the
name, English ids and names. `<!-- task:ID spec: docs/work/x/spec.md -->`
points the worker packet at the acceptance section. Keep that section short:
the packet is capped at `worker_packet_max_bytes`.

Declare impact from callers and shared behavior, not the directory name:
`local`, `shared`, or `unknown`. Shared changes need `delivery.checks` mappings
including affected callers; `delivery.full_globs` names project-wide impact.
An undeclared/unknown impact, uncovered shared paths or undeclared actual code
changes escalates to full acceptance. Do not mark uncertain work local merely
to avoid a slow test. At integration, configure `delivery.integration_cmd` or
accept the full-suite fallback. Delivery always uses `verify_cmd`.

Optional `worker` column per row; `admit` refuses an unknown id. A task whose
files match `irreversible_globs` goes to the tail and needs
`<!-- task:ID approved: <who/date> -->`, written only when the user approved
that task in this conversation. `git push` is never a task.

Then `flow admit --repo .`; split or declare a REFUSE / UNDECLARED head task
now.

## 4. Configure and launch

`.gate/config.json`: `workers` (the pin first, fallbacks after),
`worker_cmds` or `worker_models` when a CLI needs a model, `judge.enabled`
and `judge.chain` when the project wants a package review, `judge.checkpoints`
for rows that must be reviewed on their own, `max_model_calls` for the run.
Never raise limits or weaken admission to get a task through.

Count remaining TODO rows and pass that as `--MaxTasks`: any enabled review
and the selected stage checks run when the queue has no TODO left, so one launch per
package is the cheap shape and a launch per task buys nothing. Then follow
`run-queue`: detached launch, one validated watcher, narrate, read the report.
Never mark DONE, never `--no-verify`, never take over IN_PROGRESS, never
push unless the user or CONTEXT already allowed it.

## 5. After the run

Read `RUN-REPORT.md` bottom-up: **Waiting on you** first, then **Review**,
then **Stage acceptance** / **Full acceptance** and **Defect checkpoints**,
then the task table. Relay those in the user's
language, leading with the measured result or the blocking fact.

- `review_failed:<ids>`: safety defects and a broken current core flow qualify for the one
  automatic repair batch. Other defects are recorded for grouped maintenance;
  failed promised acceptance remains incomplete. Consolidate related blockers into a bounded
  batch tagged `<!-- task:ID origin: review -->`; preserve BUG ids, original
  evidence and a failing regression check. Verify the repair and direct
  regressions only. After this one repair batch, stop if verification still fails.
  Further repair requires explicit continuation authorization. Ambiguous findings
  need evidence clarification, not an invented patch.
- `review_loop:<location-or-task>`: the same recorded blockers remain without
  improvement. Stop repeating the approach, reproduce the common cause and
  report the cause and stop. A different fix is still another repair round
  and requires explicit continuation authorization.
  Never rename bugs/tasks to erase history or accept a privacy leak for speed.
- `stage_acceptance_failed:<ids>` / `full_acceptance_failed:<ids>`: read the
  saved selected-check logs or due-defect list. Repair the demonstrated high-risk/core cause only within the one-batch
  allowance; otherwise record it and stop without claiming acceptance.
  Do not restart broad review, rerun a long check just to read its output, or
  repeatedly retry an unchanged failure.
- `scope_request:<id>`: widen the declared files or split the task.
- `prompt_too_large:<id>`: shorten the spec acceptance or split the task.
- `needs_approval:<id>`: show the scope line, ask once, add the line on yes.

A stage is finished when its selected checks and required reviews pass.
Disclose untested scope and unavailable reviews. A final delivery needs
`delivery` acceptance; when no new tasks remain, run `flow verify --stage
delivery --repo .` instead of inventing a task or claiming an old local PASS.

`REVIEW-NOTES.md` holds defects, not a second task queue. Ordinary defects get
a due checkpoint and are grouped at that checkpoint, not dispatched on each
finding. Preserve structured ids and severity. To resolve a defect, update its
latest metadata to `status: resolved` with nonempty `resolution` naming actual
verification evidence, and keep the visible note consistent. Deferral needs a
reason and next checkpoint; it cannot waive safety or an agreed stage blocker.
Only revise acceptance with the user's authorization where product promises
change. Do not reopen finished work for style or optional hardening.

Rebuild `CONTEXT.md`: a stopped or interrupted task is written as such, with
its failing command, the uncommitted paths and the log path, never as done.
If the run brought a user correction or a verified failure and fix, record
one candidate lesson in the package's `evidence.md` (`flow`: Capture a
lesson); otherwise none. If the user asked to finish everything and TODO rows
remain for a reason other than a genuine blocker, say so and launch again.

## Host notes

Same files and commands from every CLI. `flow` is on PATH; `flow home`
locates `gate/gate.py`. Launch with `pwsh`, not a tool-shell job object.
Skills live once in `~/.agents/skills/<name>` and are linked into
`~/.claude/skills` and `~/.codex/skills`.


## Repair continuation

Automatic repair policy: consolidate evidenced high-risk defects and broken
current core flows into one repair batch, followed by targeted verification.
Record ordinary defects for later grouped maintenance; do not dispatch them
just because review found them. Existing promised acceptance still cannot be
reported as passed while failing. If the repair or its verification fails,
stop automatic dispatch, even if blocker counts decrease or budget remains.
This includes review, local-test and stage-acceptance failures: do not alternate
stop types, rename tasks, or restart sessions to obtain another repair round.

Continuation is OFF by default. Only explicit user authorization for this
continuation permits setting `repair_continuation.enabled: true`, a nonempty
`approved_by` recording that authorization, a finite `tasks` list and an
`expires_at` Unix timestamp. Do not infer consent from "continue", spare quota,
or permission to finish the original delivery. Before enabling it, record the
user-authorized cumulative time, spending and round caps in the existing spec
and CONTEXT; stop at the first cap, missing usage evidence, or no progress.
Do not reset caps across runs. The engine enforces the named-task/expiry grant
following a failed repair review or failed repair run; cumulative spend
and failures outside the runner remain host-enforced. This is not a provider billing cap. Expire/disable the
grant at the end of the authorized batch; never carry it into another project
or delivery. Keep safety failures visible and never call them complete.
