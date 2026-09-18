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

### Model roles (hard rule)

Two roles, two model sets. They never cross. The names below are the
authors' defaults; a deployment changes them in `.gate/config.json`
(`judge.chain`, `judge_cmds`, `workers`, `worker_cmds`) and in
`FORBIDDEN_WORKER_MODELS` in `gate.py`, not in this skill.

| role | who | model |
|---|---|---|
| host / judge | the CLI running this skill: inspect, decide, audit, write the queue | the strongest model available (`fable` first); if quota is short, fall back to `opus`, then `codex` — and say so in one line |
| worker | the process `gate.py run` spawns per task | `opus`, `codex`, `kimi`, `grok` only. The host/judge model (`fable`) is **forbidden** as a worker |

Why: the host/judge model is the judgement model and the most expensive; a
headless `claude -p` inherits whatever the user last set as their CLI
default, which may be that model. `gate.py` enforces this — its built-in
`claude` worker pins `--model opus`, and `ensure_worker_model()` injects
`opus` into any `worker_cmds.claude` override that omits `--model` and
refuses one that names a forbidden model. When you write
`worker_cmds.claude` yourself, still write `--model opus` explicitly; do not
rely on the injection.

Audits and surveys: gather with cheaper subagents where the host supports
them, judge with the host model. Never run a whole-project audit's data
collection on the host model.

## 1. Inspect (always)

Cwd is the project. Read, do not invent:

1. `CONTEXT.md` (create via `flow init --repo .` only if missing)
2. `TASK_QUEUE.md`
3. `git status` + `git log --oneline -10`
4. `.gate/journal.ndjson` tail and `.gate/RUN-REPORT.md` if they exist
5. `flow doctor --repo .` and `flow admit --repo .`

Tell the user, in their language, five facts: goal, now, last checkpoint,
next step, anything still waiting on a human. Then continue — this skill
does not stop at the report.

**Stop instead of running** if: pre-commit hook missing, head task
`BLOCKED` or `IN_PROGRESS`, or another session has this worktree dirty in a
way you did not make. Report and wait.

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

Append `TODO` rows to `TASK_QUEUE.md` in gate format. Each row needs a
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

Run `flow usage --repo .` first. It sends one tiny request per CLI and
benches the quota-dead ones in `.gate/tool-status.json` (a probe that never
reached the provider is benched briefly, not for the cooldown). Then fill the
`worker` column from this table, skipping benched CLIs. Put the reasoning in
the package `spec.md`.

| task kind (from the scope line) | first choice | fallback |
|---|---|---|
| mechanical implementation inside one module, tests included | `codex` | `claude` |
| cross-package change, tricky semantics, security / permission logic | `claude` (opus) | `codex` |
| docs, manual-test checklists, config, small text edits | `kimi` | `codex` |
| UI polish against a design spec | `codex` | `claude` |

Leave the cell empty when two rows are equally good; the run-wide list decides.
Never pin the host/judge model — the runner refuses it as a worker.


### Reversibility tiers

Every declared file is classified by `.gate/config.json` `irreversible_globs`
(migrations, `data/**`, hooks, `.claude/settings*`, apply scripts). A task that
touches one:

- goes to the **tail** of the queue, so the run finishes everything reversible
  first and the user approves once, not mid-run;
- needs its own line `<!-- task:ID approved: <who/date> -->` before the runner
  will dispatch it. Without it `admit` prints `APPROVAL` and `run` stops with
  `needs_approval:<id>` even in advisory mode. Approval is per task id; never
  copy one forward.

You write the approval line only when the user said yes to *that* task in this
conversation. Otherwise leave it out and list the task under "waiting on you".

External actions (`git push`, broadcasts) are never a task: the host does them
after the run when CONTEXT or the user already allowed it, with the
`git rev-list --left-right --count origin/main...main` 0/0 re-check.

Baselines must match the live oracle (`flow verify` / whatever
`.gate/config.json` `verify_cmd` is). Then `flow admit --repo .`. A `REFUSE`
or `UNDECLARED` head task is split or declared now, not argued with later.

## 4. Pin the worker and launch

Edit `.gate/config.json` `workers` (and `worker_cmds` when needed).

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

Check whether `judge.enabled` is `true` for this project. When it is,
every closed task is reviewed by the judge chain (`judge.chain`, read-only)
and revised at most `judge.max_revisions` (2) times by a *different* worker
before the run either moves on (score ≥ `pass_score`) or stops with
`judge_escalated:<id>`. Do not raise `max_revisions` or lower `pass_score`
to get a task through; that is the user's call. Design:
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
Relay those two sections to the user in their language; they are the only
thing they have to read.

User-facing updates: follow the user's format, lead with a measured result or
the current blocking fact, and omit repeated process narration. Do not add a
separate rewriting model or a second draft. Preserve paths, numbers, errors,
and uncertainty.

- `judge_escalated:<id>` — the task is DONE by the acceptance gate but not to
  standard. Read the last findings in the report. If they are within the
  task's declared scope and clearly right, queue one follow-up task with the
  findings as its spec; if they need a decision or scope growth, put them in
  CONTEXT §5 and stop. Never re-run the judge loop by hand.
- `revision_broke_verify:<id>` — inspect `git log` / `git status` yourself
  before anything else runs; a revision commit may need reverting (ask).
- `needs_approval:<id>` — show the user the task's scope line and ask for a
  yes; on yes, add the approval line and relaunch.
- `skipped` judge rows are not blocking; mention them once.

Rebuild `CONTEXT.md`. If the user asked to finish everything and TODOs
remain for a reason other than `blocked` / `no_workers`, say so plainly;
do not silently start a second run.

## Host notes (all four CLIs)

Same files, same commands. `flow` is on PATH and `flow home` locates
`gate/gate.py`. Launch with `pwsh`, not a tool-shell Job Object.

The repository installer keeps one canonical copy of each skill in
`~/.agents/skills/<name>` (read by Codex, Grok Build and Kimi Code) and links
it into `~/.claude/skills` and `~/.codex/skills`; Kimi loads `~/.agents/skills`
via `extra_skill_dirs`.
