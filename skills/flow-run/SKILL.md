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
a fork that is uncertain, high-impact and hard to undo -> `model-debate`
(its two rounds are part of the decision; do not shorten it to finish sooner).
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

Survey only the code and docs the next step touches. If CONTEXT section 4 is
already a testable slice, use it; otherwise write or update
`docs/work/<id>/brief.md` and `spec.md` from the flow templates and rebuild
`CONTEXT.md` (at most 200 lines, five questions, never append). Do not ask
the user to pick among options this skill exists to settle; record the choice
under Decisions in the spec.

## 3. Queue

Append TODO rows in gate format, one independently verifiable and reversible
behaviour per row, implementation and tests together. Each row needs:

```
| N | ID | name | `TODO` | 12 passed | 14 passed |
<!-- task:ID files: src/a.py, tests/test_a.py -->
<!-- task:ID verify: {"cmd": "pytest tests/test_a.py -q", "timeout_s": 900} -->
```

At most 6 files, no overlap with other TODO rows unless
`<!-- task:ID2 after: ID1 -->` orders them, no human-decision wording in the
name, English ids and names. `<!-- task:ID spec: docs/work/x/spec.md -->`
points the worker packet at the acceptance section. Keep that section short:
the packet is capped at `worker_packet_max_bytes`.

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

Count remaining TODO rows and pass that as `--MaxTasks`. Then follow
`run-queue`: detached launch, one validated watcher, narrate, read the report.
Never mark DONE, never `--no-verify`, never take over IN_PROGRESS, never
push unless the user or CONTEXT already allowed it.

## 5. After the run

Read `RUN-REPORT.md` bottom-up: **Waiting on you** first, then **Review**,
then **Full acceptance**, then the task table. Relay those in the user's
language, leading with the measured result or the blocking fact.

- `review_failed:<ids>`: read the findings; admit one repair row inside the
  same package with the findings as its acceptance, then relaunch. Never
  revise by hand or lower the bar.
- `full_acceptance_failed:<ids>`: read the tail; admit one repair row.
- `scope_request:<id>`: widen the declared files or split the task.
- `prompt_too_large:<id>`: shorten the spec acceptance or split the task.
- `needs_approval:<id>`: show the scope line, ask once, add the line on yes.
- review rows the runner added (`origin: review`) run on the next launch.

Rebuild `CONTEXT.md`. If the user asked to finish everything and TODO rows
remain for a reason other than a genuine blocker, say so and launch again.

## Host notes

Same files and commands from every CLI. `flow` is on PATH; `flow home`
locates `gate/gate.py`. Launch with `pwsh`, not a tool-shell job object.
Skills live once in `~/.agents/skills/<name>` and are linked into
`~/.claude/skills` and `~/.codex/skills`.
