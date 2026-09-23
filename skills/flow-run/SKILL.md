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

Before dispatch, inspect implementation, affected callers and tests for each
behaviour. Include the files needed to connect the vertical slice; a file cap
is not a reason to omit a necessary caller. Order or split connected work
before launch. Reconcile earlier scope requests against the spec and saved log;
do not relaunch an unchanged request or automatically accept every requested file.

Then `flow admit --plan --repo .`. This read-only preview catches malformed rows,
invalid checks/spec links and oversized packets, shows earlier scope requests,
and lists declared stage checks. Fix errors and resolve relevant warnings.
It does not infer dependencies or prove coverage. Broad task checks can be
justified; otherwise select affected tests and mapped callers before admission.
Do not mark shared work local or weaken accepted requirements for speed.
Use `verify --task` for the final task check, avoiding a separate identical run
immediately before it. Changed candidates still require fresh evidence.

## 4. Configure and launch

`.gate/config.json`: `workers` (the pin first, fallbacks after),
`worker_cmds` or `worker_models` when a CLI needs a model, `judge.enabled`
and `judge.chain` when the project wants a package review, `judge.checkpoints`
for rows that must be reviewed on their own, `max_model_calls` for the run.
Never raise limits or weaken admission to get a task through.

Count remaining TODO rows and pass that as `--MaxTasks`: any enabled review
and the selected stage checks run when the queue has no TODO left, so one launch per
package is the cheap shape and a launch per task buys nothing. Probes are lazy:
only the selected CLI, a needed fallback, or a reviewer about to run is probed.
Do not run `flow usage` as routine preflight; it probes every configured candidate.
Then follow
`run-queue`: detached launch, one incremental event watcher, report meaningful changes,
read the final report. Keep a byte cursor; do not repeatedly scan full journals or
worker transcripts. Prefer event notifications; otherwise use idle backoff within
host responsiveness limits. Read worker log excerpts only for an error, a suspected
stall or a user status question. Empty polls are not progress to narrate.
Never mark DONE, never `--no-verify`, never take over IN_PROGRESS, never
push unless the user or CONTEXT already allowed it.

## 5. After the run

Read `RUN-REPORT.md` bottom-up: **Waiting on you** first, then **Review**,
then **Stage acceptance** / **Full acceptance** and **Defect checkpoints**,
then the task table. Relay those in the user's
language, leading with the measured result or the blocking fact.

- `review_failed:<ids>`: consolidate evidenced defects within the authorized
  goal; preserve BUG ids and add a failing regression. Verify the changed flow
  and direct regressions only. Reuse existing authorization under the continuation
  contract below; an ordinary failed repair is not a new approval gate.
- `review_loop:<location-or-task>`: stop repeating the failed approach. Inspect
  the recorded cause once; continue only with a different evidenced fix within
  scope and cumulative limits. Repeated no-progress failure is a technical blocker.
- `stage_acceptance_failed:<ids>` / `full_acceptance_failed:<ids>`: read saved
  check logs, fix the demonstrated cause in scope and rerun affected checks.
  Do not restart broad review or repeat an unchanged failure.
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

When evaluating efficiency, use `flow metrics --last 5 --repo .` and the report's
Efficiency section. Compare similar delivery batches, check time, scope stops and
defect outcomes. Worker duration includes its tests; do not add overlapping times.
Legacy missing measures are unknown. CLI calls are not token or spending measures.
A smaller call count alone does not prove faster correct delivery.

Same files and commands from every CLI. `flow` is on PATH; `flow home`
locates `gate/gate.py`. Launch with `pwsh`, not a tool-shell job object.
Skills live once in `~/.agents/skills/<name>` and are linked into
`~/.claude/skills` and `~/.codex/skills`.


## Repair continuation

Authorization attaches to the agreed goal, not to each attempt. When the user has
authorized implementation through completion, evidenced defects in that same
acceptance and direct repair regressions may be fixed without asking again.
Do not expand the product scope, external effects or explicit user limits.

Consolidate related defects into a small repair batch. Before dispatch, record
the failing acceptance, concrete cause, changed approach and cheapest decisive
check. Cover the supported branches of the changed flow together, including
refund versus reversal and save/select/use where applicable; a happy-path test
alone does not close an agreed branch. Optional findings stay in the backlog.
Reuse passed checks while their relevant inputs remain unchanged; run the final
required oracle once on the final candidate. Review repairs and direct regressions,
not the entire package again. Do not rerun commands just to retrieve their output.

A runner stop returns control to the host; it is not automatically a permission
request. On unchanged blockers, stop repeating the approach. Inspect the existing
failure evidence once and continue only with a materially different, evidenced
fix. If the same cause fails again without new evidence, stop model dispatch and
report the actual technical blocker. Do not rename tasks/findings to hide history.
New findings must map to existing acceptance or a serious direct regression;
an endless succession of unrelated improvements is not progress toward the goal.

Keep the cumulative deadline and any user-set call/token/spending caps in the
spec and CONTEXT across runs. Host estimates/checkpoints are scheduling tools,
not invented user-approval gates. Never silently extend an explicit user limit.
Track actual available usage; missing currency/token data is unknown, not zero
or a reason to invent a new spending approval. Call counts are not token costs.

The engine still requires a bounded `repair_continuation` grant after a failed
repair: `enabled: true`, `approved_by` citing the existing goal authorization,
a finite admitted `tasks` list and a finite `expires_at` within the cumulative
deadline. The host may update named tasks for evidenced in-goal repairs while
the runner is stopped; this is bookkeeping, not a new user approval. A bare
switch, spare quota or unrelated authorization is insufficient. Disable the grant
when the goal finishes or its budget/authority ends; do not carry it elsewhere.

Ask only when a necessary decision or permission is genuinely missing, an
external action exceeds authorization, or a user-set limit needs changing.
Otherwise continue toward the goal. Never report a failed acceptance as complete.
