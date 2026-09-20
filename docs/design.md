# Design contract

The standing contract behind the `flow`, `flow-run` and `run-queue` skills and
the `gate.py` engine: what is mechanised, what is only a convention, and what is
deliberately not built.

## 1. Two layers, one implementation each

| Layer | Entry | Implementation | Who is in the loop |
|---|---|---|---|
| Interactive | `$flow` skill (`skills/flow/`) | a thin router in the agent plus deterministic scripts (`flow/flow.py`, `flow/audit.py`, templates) | a human, who can change their mind |
| Unattended | `$run` skill (`skills/run-queue/`) | `gate/gate.py`: one worker process per task, exit-code verdicts, journal, run report | nobody; the executor is never its own judge |

The layers sit on opposite sides of a trust boundary: an agent that supervises
its own work can talk itself into calling a task done. `$flow-run`
(`skills/flow-run/`) routes through both layers in sequence and adds no third
implementation.

## 2. Three hard rules, all mechanised

1. **The verdict comes from an exit code.** A task is DONE only when the
   acceptance command in `.gate/config.json` (`verify_cmd`) exits zero and the
   pre-commit hook installed by `gate.py` lets the DONE commit through. No
   model claim, status flag or summary counts as a verdict.
2. **Admission before the loop.** `gate.py admit` keeps tasks that are not
   small, isolated and verifiable out of the unattended run (section 4).
3. **Tree governance is read-only.** `flow audit` classifies and reports; it has
   no write mode and never modifies the audited tree (section 7).

All three are enforced by scripts and git, not by instructions a model could
ignore. Gate checks: `gate/README.md`. Judge, probe and approval mechanics:
`docs/autonomy.md`.

## 3. State ownership: every kind of state has exactly one home

```text
<project>/
  .gate/                    machine truth, written only by gate.py
    config.json             project contract: verify_cmd, prefix, workers, admission knobs
    verdict.json            last verify result
    journal.ndjson          append-only event stream
    runs/<id>/              prompts and logs of each attempt
  TASK_QUEUE.md             the only task queue (table plus scope lines)
  CONTEXT.md                human snapshot, at most 200 lines, five questions
  AGENTS.md                 cross-tool project conventions (CLAUDE.md may just include it)
  docs/
    adr/                    long-lived decisions
    work/<work-id>/         one vertical work package per feature
      brief.md              ask, goal, non-goals, unknowns
      spec.md               approved behaviour; edit in place and bump `revision:`
      evidence.md           verification summary, pointers, candidate lessons
```

`CONTEXT.md` answers exactly five questions: goal and red lines; what is being
worked on now; last stable checkpoint; next step; decisions waiting on a human.
It is rebuilt, never appended.

**No second home.** Never create `.flow/`, `.pipeline/`, `tasks.yaml`,
`tasks.json` or any other queue or state directory. The queue format is the
`TASK_QUEUE.md` table that `gate.py` parses and the hook enforces.

## 4. Admission

Each task declares its scope on its own line under the queue table, as an HTML
comment so it does not disturb the table parser:

```markdown
<!-- task:WP-7 files: src/foo.py, tests/test_foo.py -->
```

Three deterministic criteria define the sweet spot:

| Criterion | Check | Refusal |
|---|---|---|
| small | declared files ≤ `max_task_files` (default 6) | `too-broad` |
| isolated | no declared file overlaps another TODO task, unless `<!-- task:B after: A -->` declares that B extends A in order | `overlaps <id>`, `out-of-order`, `unknown-dependency` |
| verifiable | `verify_cmd` is set, the last verdict is not VACUOUS, and the task name has no human-decision wording (`human_decision_regex`) | `no-oracle`, `needs-human` |

A task without a scope line is `UNDECLARED`. `gate.py admit` prints a
three-state report per TODO task: `ok`, `REFUSE <reason>` or `UNDECLARED`. It
is always read-only.

`gate.py run` is advisory by default: a refused head task is journaled as
`admit_refused` and the run continues with a warning. With `--strict-admit` or
`"strict_admit": true` the run stops rather than skips, because rows carry their
dependencies in order. Flip a project to strict only after one full advisory
cycle has been human-checked for false refusals.

## 5. What `$flow` routes

| The user asks for | Action |
|---|---|
| a new idea or requirement | survey the code first, restate, discuss in small rounds, then `docs/work/<id>/brief.md` and `spec.md` from the templates |
| options or a prototype | the cheapest proof that answers the question; conclusions go back into `spec.md` |
| queue it | rows in `TASK_QUEUE.md` with scope lines, then `flow admit` shown to the user |
| an urgent task | a row above the current head, the preempted id noted in `CONTEXT.md` |
| continue or hand off a session | read `CONTEXT.md`, `git log`, the queue and the journal tail; rebuild `CONTEXT.md` |
| "is the project getting messy?" | `flow audit`, report only |
| high uncertainty, high impact, hard to undo | only then suggest `model-debate` |

Honesty boundary: shape, prove and freeze are documented conventions backed by
templates, not a state machine in code. The model is the router; the skill only
supplies the action list and the file conventions.

## 6. What `$run` is

Three steps: preflight (`flow doctor`, `flow admit`, shown to the user), a
detached `gate.py run` that outlives the agent session, and narration of
`.gate/journal.ndjson` events until `run_end`. The loop, the verdicts and the
stop reasons belong to `gate.py`; the skill is a launcher and a narrator. It
never marks a task done, never bypasses the hook, and never takes over a task
that is `IN_PROGRESS`.

## 7. The read-only auditor

`flow audit` places every file into one of nine categories with deterministic
heuristics only: `generated`, `runtime`, `historical`, `duplicate`, `stale`,
`active`, `canonical`, `domain`, `unknown`. Signals are git tracking state,
modification time, size, state words in names, extension groups, reference
counts and a size threshold for active documents. Anything the heuristics
cannot place confidently is `unknown`, never guessed.

The report is written outside the audited tree; the script refuses a report
path inside it. Read-only behaviour is verifiable: hash the target tree before
and after an audit and the digests must match. Any move or delete that follows
an audit is a separate, human-approved batch.

## 8. Not done, by design

- No `.flow/`, `.pipeline/` or second queue format.
- No write mode in the auditor; no automatic moves or deletes.
- No cross-CLI natural-language router; other CLIs reach the unattended layer
  through the `flow` and `gate.py` commands.
- No automatic commit, push, branch or worktree outside the per-task commits
  that the gate contract requires of a worker.
- No independent model as the acceptance authority; acceptance is a command
  and its exit code.
- No context-usage hook, tool-call counter or transcript pruning. A handoff is
  considered at phase boundaries and `CONTEXT.md` carries it; compaction is
  the host CLI's feature, and an unknown usage figure stays unknown.
- No learning daemon, session observer, lesson generator or confidence score.
  A lesson is a row in a work package's `evidence.md`, backed by evidence,
  scoped to its repository, and a rule only after the user approves it.

## 9. Removed on 2026-09-18, and why

Between 2026-09-08 and 2026-09-17 the engine grew a per-task judge with a
score threshold and revision loop, execution profile freezing, receipt-based
recovery of stopped tasks, an acceptance cache keyed on environment hashes, a
persistent cost ledger and 130 KB of policy prose. On the one real project
that ran it, throughput fell from 0.34 hours per closed task to two hours,
then to zero, with most runs stopping because the machinery refused finished
work. All of it is gone. What replaced it: task-local verify bound to file
bytes, one package review per run gated on high findings, a flat call budget,
one retry on a dirty tree, and a bounded worker packet. The archive branch
`archive/codex-rework-20260918` keeps the removed code for reference.

## 10. Removed on 2026-09-20, and why

The lean rebuild still let the review write queue rows: every finding below
high became a TODO row, and the review plus the full oracle ran at the end of
every run. Runs were launched one task at a time, and review rows were
reviewed like any other row. Measured over two days on two projects: 165 rows
made from findings, 36 of them closed; on one project a third of the worker
time went into them and 27 of 28 reviews covered a single task; on the other
the full oracle took longer than the work (8.1 h against 5.8 h). One block of
string parsing took four rounds and two hours for a few lines of wording.

A first repair bounded the chain (a generation counter on review rows and a
configurable depth); a second added a history check with three thresholds
fitted to one project. Both were removed the same day. Bounding a loop keeps
the loop. What replaced them is section 2 applied to the review: a reviewing
model is not an acceptance authority, so it can block a package and cannot
create work; the review and the full oracle belong to the package boundary;
a failed review gets one repair round. No thresholds (docs/autonomy.md
section 5).
