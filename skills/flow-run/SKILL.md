---
name: flow-run
description: >
  Inspect this project's current state, decide the next work as the host CLI,
  write an admission-ready TASK_QUEUE, then unattended-run it. Optional worker
  pin: claude / claude-code, codex, kimi, grok / grok-build. No pin → the CLI
  running this skill. Use when the user types $flow-run or /flow-run, or says
  check progress, check status, keep going and finish, flow run, or continue
  and finish; also responds to equivalent requests in other languages. Works
  from Claude Code, Codex CLI, Kimi CLI, and Grok Build.
---

# $flow-run — inspect, decide, queue, run

You are the **host CLI** (the process the user is talking to). You survey,
you decide, you write the queue. `gate.py run` executes. You never mark a
task `DONE` yourself.

This skill is a router. Do not copy `flow` or `run-queue` into this file.
Load them when that step starts:

- shape / brief / spec / CONTEXT / queue format → `flow`
- unattended launch, journal watch, stop reasons → `run-queue`
- high-stakes fork you cannot settle → `model-debate`

`flow home` prints the checkout directory of this workflow; the gate scripts
live in `<home>/gate/` and the design of the unattended half is
`<home>/docs/autonomy.md`.

## 0. Parse the worker pin

From the user message, map to a gate worker id. First match wins.

| user said | worker id |
|---|---|
| claude, claude code, claude-code | `claude` |
| codex, codex cli | `codex` |
| kimi, kimi cli, kimi-code | `kimi` |
| grok, grok build, grok-build | `grok` |

No match → **host CLI**:

1. `GROK_HOME` set, or this session's tools are Grok's → `grok`
2. `CLAUDECODE` / `CLAUDE_CODE` / process `claude` → `claude`
3. process `kimi` or `KIMI_HOME` / `~/.kimi-code` in env → `kimi`
4. process `codex` or `CODEX_HOME` → `codex`
5. else first of `claude`, `codex`, `kimi`, `grok` that `where` / `which` finds

Say one line before doing work: `host: <this CLI> · worker: <id>`.

Unknown name → list the four ids and stop. Do not guess.

### Queue-bound models and reasoning

The host uses its current model, separately from the queue. Read
`<home>/docs/execution.md` before queueing or launching: declare concrete model
IDs and reasoning effort for workers, judges, revisions and every eligible
fallback. Resolve once while queueing and persist with `flow lock-execution`.
Never inherit a later host session or global CLI selection. Explicit user
package/task effort limits apply to all covered calls, including revisions.
Existing queues without profiles require deliberate profile preparation before
their next launch; do not silently snapshot today's host as their intended model.
Report configured and observed selections separately; missing metadata is unknown.

Audits and surveys: gather with cheaper subagents where the host supports
them, judge with the host model. Never run a whole-project audit's data
collection on the host model.

## 1. Inspect (always)

Cwd is the project. Read, do not invent:

1. `CONTEXT.md` (create via `flow init --repo .` only if missing)
2. The queue configured in `.gate/config.json` (default `TASK_QUEUE.md`);
   use that same path throughout this skill, never create a second queue.
3. `git status` + `git log --oneline -10`
4. `.gate/journal.ndjson` tail and `.gate/RUN-REPORT.md` if they exist
5. `flow doctor --repo .` and `flow admit --repo .`

Tell the user, in their language, five facts: goal, now, last checkpoint,
next step, anything still waiting on a human. Then continue — this skill
does not stop at the report.

**Do not launch into an unsafe state:** a missing pre-commit hook, head task
`BLOCKED` or `IN_PROGRESS`, or conflicting worktree changes. Inspect ownership
and the recorded reason first. Use run-queue's host recovery procedure for an
in-scope blocker; never take over `IN_PROGRESS` or overwrite another session.
Ask only for information or authorization that is actually missing.

No verify command → do not invent one. Say admission will refuse code tasks
until the user sets `flow init --verify-cmd`. You may still write brief/spec.

## 2. Decide (host CLI is the judge)

Survey the code and docs the next step actually touches. Then decide:

- If CONTEXT §4 is already a testable slice, use it.
- If not, write/update `docs/work/<id>/brief.md` and `spec.md` (flow
  templates). Rebuild `CONTEXT.md` (≤200 lines, five questions, never append).

**Do not ask the user to pick** among options this skill exists to settle.
Record the choice in the spec `Decisions` list.

### When to debate

Call `model-debate` only when **all three** are true: you are uncertain,
the impact is high, the choice is hard to undo. Never by default.

If the user already said to finish the work, skip model-debate's human
gates: one research+critique pass, verify against the repo, freeze
`final.md` into the spec, keep `decision-log.md`. Two distinct models
minimum. `--no-web` only for a purely internal repo question.

## 3. Queue

Append `TODO` rows to the configured queue in gate format (`TASK_QUEUE.md`
throughout this document is an alias for that path). Each row needs a
scope line. ≤6 files, no overlap with other TODOs, no human-decision wording
in the name (`human_decision_regex`: design / decide / choose and their
Chinese equivalents by default). English for ids and names.

```
| N | ID | name | `TODO` | 12 passed | 14 passed |

<!-- task:ID files: src/a.ts, tests/a.test.ts -->
```

**Sequential slices sharing a file** declare their order so admission does
not refuse the overlap: `<!-- task:ID2 after: ID1 -->`. Independent tasks must
still have disjoint files.

**Optional per-task worker.** A table may add a `worker` column
(`claude` / `codex` / `kimi` / `grok`, empty = no pin). gate dispatches that
row to the named CLI first and falls through to the run-wide list if it is
benched or fails. Use it when tasks in one package suit different CLIs; put
the reasoning in the package `spec.md`, not in the row. `admit` refuses an
unknown id.

```
| # | ID | name | status | worker | start baseline | end baseline |
| N | ID | name | `TODO` | kimi | 12 passed | 14 passed |
```

### Assign workers by usage and task kind

Declare execution profiles from local capability discovery first, then run
`flow usage --repo .`. It sends a tiny request per distinct configured profile and
benches the quota-dead ones in `.gate/tool-status.json` (a probe that never
reached the provider is benched briefly, not for the cooldown). Then fill the
`worker` column from this table, skipping benched CLIs. Put the reasoning in
the package `spec.md`.

| task kind (from the scope line) | first choice | fallback |
|---|---|---|
| mechanical implementation inside one module, tests included | `codex` | `claude` |
| cross-package change, tricky semantics, security / permission logic | `claude` (declared model/effort) | `codex` |
| docs, manual-test checklists, config, small text edits | `kimi` | `codex` |
| UI polish against a design spec | `codex` | `claude` |

Leave the cell empty when two rows are equally good; the run-wide list decides.
All runnable profiles need explicit model and reasoning settings. Choose routine
defaults from the approved constraints and task needs; do not ask the user to
fill a model matrix. Worker and judge roles may use the same model family.

### Related Codex tasks may share a session

When `session_reuse.enabled` is true, explicitly group adjacent Codex tasks
that share code/background with `<!-- task:ID session: docs/work/<package> -->`.
The package must contain `spec.md`. Omit the marker for unrelated work; do not
reorder tasks or change their worker merely to preserve a session.

Gate still dispatches one task per process and checks it separately. It resumes
the previous session only after a first-attempt success, fresh PASS evidence,
and a passing independent judge with no revision. Changed background or workspace,
retries, irreversible paths, custom Codex commands, and task/input budgets force
a fresh session or the existing non-reuse path. Checkpoints are local to one run;
the runner never resumes an old run or uses `--last`.

On a resumed task, read its current acceptance and newly relevant source; reuse
unchanged shared background already in the conversation. Keep confirmed decisions
and code entry points in the existing work package, not a second memory system.
Use task-specific `.gate` files for long command output, surface exit codes and
failure excerpts, and read the full log when needed. Report the Sessions table
alongside the task results; input counts are aggregate usage, not context occupancy.

### Reversibility tiers

Every declared file is classified by `.gate/config.json` `irreversible_globs`
(migrations, `data/**`, hooks, `.claude/settings*`, apply scripts). A task that
touches one:

- goes to the **tail** only when independent; preserve dependency order and
  keep required repairs before their dependents;
- needs its own line `<!-- task:ID approved: <who/date> -->` before the runner
  will dispatch it. Without it `admit` prints `APPROVAL` and `run` stops with
  `needs_approval:<id>` even in advisory mode. Approval is per task id; never
  copy one forward.

Write each task's approval line only when current conversation authorization
covers its concrete scope. A named, already-approved implementation package may
cover an in-scope repair: record the original user instruction and why it covers
this task ID; never blindly copy another task's approval. New irreversible effects
or scope require authorization. List only genuinely unapproved tasks as waiting.

External actions (`git push`, broadcasts) are never a task: the host does them
after the run when CONTEXT or the user already allowed it, with the
`git rev-list --left-right --count origin/main...main` 0/0 re-check.

Prepare per-task defaults/overrides now; freeze after finalizing the worker and
judge lists below. A chat setting switch does not change queue execution.

Baselines must match the live oracle (`flow verify` / whatever
`.gate/config.json` `verify_cmd` is). Then `flow admit --repo .`. A `REFUSE`
or `UNDECLARED` head task is split or declared now, not argued with later.

## 4. Pin the worker and launch

Prepare `.gate/config.json` `workers` (and `worker_cmds` when needed) before
freezing queue execution. Preserve existing frozen defaults when no new worker
instruction was given; the host CLI default applies only to unconfigured queues.
Preserve the project's `session_reuse` policy; it is not a per-run tuning knob:

- `workers`: `[ "<id>" ]` — the pin from step 0, one id. This is the
  **run-wide default**. Precedence, top wins: row `worker` column > this
  list (verbal pin) > host CLI default. A verbal pin therefore only fills rows
  whose `worker` cell is empty; it never overrides a pinned row. When the
  queue pins tasks to several CLIs, list every one of them here in fallback
  order so a benched pin has somewhere to go.
- `kimi` must set
  `"worker_cmds": { "kimi": ["kimi", "-p", "{prompt}", "-m", "<qualified kimi model id>", "--output-format", "text"] }`
  if missing (`kimi provider list --json` prints the valid ids). Do not strip
  other keys.

After finalizing dispatch lists and profiles, run `flow lock-execution --repo .`.
For an explicit change to future tasks, use `--reason` with the actual instruction;
preserve started tasks and prior receipts. New calls use explicit flags from the lock.

Check whether `judge.enabled` is `true` for this project. When it is,
every closed task is reviewed by the judge chain (`judge.chain`, read-only)
and revised at most `judge.max_revisions` (2) times, preferably by a different
worker. If only one authorized worker is usable, the runner may reuse it and
records `rotated: false`; report that limitation. The run then either moves on (score ≥ `pass_score`) or stops with
`judge_escalated:<id>`. Do not raise `max_revisions` or lower `pass_score`
to get a task through. A stop triggers host diagnosis and an admitted repair,
not a weaker quality threshold. Design:
`<home>/docs/autonomy.md`.

Leave `max_tasks` alone; pass `--MaxTasks` at launch. Count remaining
`TODO` rows; use that number (minimum 1). Do not ask how many.

Then follow **run-queue** exactly: Task Scheduler launch through
`"$(flow home)/gate/launch-detached.ps1"`, journal filter with the space
after `"event":`, skip the pre-launch line count, one persistent `Monitor`
with `run_end` in its filter where the host has one (never a
`run_in_background` Bash `until` loop — it is reaped at ten minutes),
narrate events, `TaskStop` the Monitor and read `RUN-REPORT.md` at the end.

Never mark DONE, never `--no-verify`, never take over `IN_PROGRESS`, never
push unless CONTEXT or the user already allowed it.

## 5. After the run

Read `RUN-REPORT.md` bottom-up: **Waiting on you** first (empty on a clean
run), then **Judge** (rounds, scores, worker per round), then the task table.
Interpret those sections using the recovery procedure below. Report the actual
remaining user decision, if any, plus verified results and automatic recovery.

User-facing updates: follow the user's format, lead with a measured result or
the current blocking fact, and omit repeated process narration. Do not add a
separate rewriting model or a second draft. Preserve paths, numbers, errors,
and uncertainty.

When the user authorized completion, a stopped runner does not revoke that
scope. Apply **run-queue → Host recovery after a stopped run** before deciding
whether human input is needed. The host reads the evidence, prepares an admitted
repair when justified, and automatically launches the next run. Announce the
reason and repair; do not ask again for the same authorized work.

For `judge_escalated`, keep the original DONE row and adverse judge history;
put one scoped follow-up before dependent TODOs, with the findings and regression
acceptance. It must earn a fresh gate verdict and independent review. Do not
manually repeat the judge loop, accept a subthreshold result, or create endless
renamed copies of the same unsuccessful repair. For `revision_broke_verify`,
inspect commits and changes first; prefer a scoped forward repair, preserve
unrelated work, and ask only if a necessary destructive action lacks approval.
For `needs_approval`, check existing scope authorization before asking and record
any valid approval for that exact task ID. Mention skipped judge rows once;
do not call unreviewed work independently approved.

Rebuild `CONTEXT.md` with the stop evidence, recovery decision, and remaining work.
Continue authorized TODOs through normal admission, launch, and monitoring until
complete, explicitly cancelled, or blocked on genuinely unavailable information,
authorization, or an external condition. Run-queue defines the bounded recovery
and no-progress rules; the shell supervisor does not diagnose or write repairs.

## Host notes (all four CLIs)

Same files, same commands. `flow` is on PATH and `flow home` locates
`gate/gate.py`. Launch with `pwsh`, not a tool-shell Job Object.

The repository installer keeps one canonical copy of each skill in
`~/.agents/skills/<name>` (read by Codex, Grok Build and Kimi Code) and links
it into `~/.claude/skills` and `~/.codex/skills`; Kimi loads `~/.agents/skills`
via `extra_skill_dirs`.
