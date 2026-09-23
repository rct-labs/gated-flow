# flow / run — user manual

> The manual for people. Design rationale: [design.md](design.md). Machine
> details: [../gate/README.md](../gate/README.md). Unattended-run behaviour:
> [autonomy.md](autonomy.md).
>
> One-sentence mental model: **want the work to move forward → `$flow-run`. It
> loads the other two as needed. Only shaping an idea, handing off or tidying up,
> with nothing run → `$flow`; only running a queue that is already written →
> `$run`.**

Each agent CLI has its own sigil for invoking a skill: Claude Code `/flow`,
Codex `$flow`, Kimi Code `/skill:flow`, Grok Build `/flow`. This manual writes
`$flow` and `$run` as shorthand for whichever form your CLI uses.

---

## 0. What the installer puts in place

| Thing | Where |
|---|---|
| `flow` command | `~/.local/bin/flow` (bash shim) plus `flow.cmd` for PowerShell/cmd on Windows; both on PATH |
| Execution engine | `gate/gate.py` in the checkout (admission + acceptance + the unattended loop) |
| Tree auditor | `flow/audit.py` in the checkout (read-only) |
| Skills | `flow`, `flow-run`, `run-queue`, `model-debate`, installed into each CLI's global skills directory |

`flow home` prints the checkout directory, so every engine script can be
addressed as `"$(flow home)"/gate/<script>` without remembering where the
repository was cloned.

**Naming note:** the unattended skill's real name is `run-queue`, because `run`
collides with a built-in Claude Code skill. Saying `$run`, "unattended", or
"run the queue" routes to it correctly.

---

## 1. One-time onboarding per project

```powershell
cd <your-project>
flow init --repo . --verify-cmd "<acceptance command>"
```

- Creates only the files that are missing (`TASK_QUEUE.md`, `CONTEXT.md`,
  `.gate/config.json`, the pre-commit hook). **Existing files are never
  overwritten**; re-running is side-effect free.
- `--verify-cmd` is the only thing you must decide yourself: one command that
  exits non-zero when the project is broken (for example `uv run pytest tests -q`).
  **If you do not give one, leave it empty. While it is empty, admission refuses
  every task with `no-oracle`** — loud beats a guessed oracle.
- Check: `flow doctor --repo .`

## 2. The daily loop

### 2.1 Have an idea → tell your agent `$flow ...`

Examples: `$flow I want to add employee growth records`, `$flow show me a few
clickable options first`, `$flow switch session`, `$flow insert an urgent fix`,
`$flow is this project getting messy`.

`$flow` will: ask until the goal is testable → write `docs/work/<id>/brief.md`
and `spec.md` → queue tasks into `TASK_QUEUE.md` with a scope declaration →
run admission and show you the result.

### 2.2 Queue row format (the same when you queue by hand)

```markdown
| 1 | WP-1 | add greeting function | `TODO` | 0 passed | 1 passed |

<!-- task:WP-1 files: src/greet.py, tests/test_greet.py -->
```

- The status must be wrapped in backticks (`` `TODO` ``); otherwise the row does
  not count.
- Below the table, one scope declaration per task (an HTML comment, invisible
  when rendered).

### 2.3 Admission check

```powershell
flow admit --plan --repo .
```

Three states:

| Result | Meaning | What you do |
|---|---|---|
| `ok` | In the sweet spot (≤6 files, no overlap with other TODO tasks, an oracle exists, no "design/decide"-style wording) | Nothing |
| `REFUSE` | Outside the sweet spot; the reason follows | **Split the task smaller; do not loosen the thresholds.** Something that genuinely needs a human decision goes back to `$flow` interactively |
| `UNDECLARED` | No scope line | Add one |

The default is **advisory mode**: a refusal warns but does not block. After one
full cycle on a real project, human-checked for false refusals, set
`"strict_admit": true` in that project's `.gate/config.json`; from then on a
refused task stops the run outright. A malformed table width always stops dispatch:
a shifted test-count cell must not become a worker name.

`--plan` also previews selected stage commands without executing them, flags
invalid checks/spec links or oversized packets, and warns about earlier scope
requests and broad/repeated checks. Errors exit 2; warnings do not rewrite
acceptance. Inspect implementation, callers and tests before narrowing a command.
Fresh code still needs fresh evidence. Use `verify --task` as the final task
check rather than running the same underlying command immediately before it.

For efficiency comparisons, `flow metrics --last 5 --repo .` reads recent runs;
add `--json` for structured output. New runs record probe/model/check durations
and lock waits. Older missing measures are unknown. Worker duration includes
its checks, so do not add the two. Compare similar batches and defect outcomes,
not just task or call counts. No extra monitoring service is needed.

### 2.4 Unattended → tell your agent `$run`

The agent will: run the doctor + admit preflight and show it to you → confirm
once (how many tasks) → launch detached through Task Scheduler (the run survives
the agent session ending) → poll the journal and narrate in real time → read
`RUN-REPORT.md` to you at the end.

The manual equivalent on Windows:

```powershell
pwsh -NoProfile -File "$(flow home)\gate\launch-detached.ps1" -Repo <project> -MaxTasks 3 -TaskName gate-<name>
```

Detached launch uses Windows Task Scheduler and `pwsh`. On macOS/Linux a
foreground `python "$(flow home)/gate/gate.py" run --repo <project> --max-tasks 3`
(or the same under `nohup`) runs the identical loop, but that path has not been
exercised by the authors.

Iron rules (enforced by mechanism, not by convention): a worker cannot mark
itself done — the pre-commit gate blocks a "DONE commit that only changes the
queue"; the verdict comes only from the acceptance command's exit code; two
identical failures stop the run; a CLI whose quota is exhausted is benched and
the next CLI takes over without burning an attempt.

### 2.4b Autonomy (design in [autonomy.md](autonomy.md))

- **Probe before dispatch.** Every candidate CLI gets one tiny real request at
  run start; a quota-shaped reply benches it for the cooldown.
- **Local checks while working.** A row may admit its own test command
  (`<!-- task:ID verify: {"cmd": ..., "timeout_s": N} -->`); the worker runs
  `flow verify --task ID` and the hook accepts the DONE commit while the
  declared files keep the tested bytes.
- **One review per batch.** With `judge.enabled`, a read-only reviewer reads
  the commits once, when the queue has no TODO left; tasks closed by earlier
  runs are included. Current-scope safety and due acceptance defects block;
  ordinary defects go to `REVIEW-NOTES.md` with stable BUG ids and checkpoints.
  They never automatically become rows. No automatic revision loop, no
  score threshold. Ordinary small reversible tools leave review off by default;
  sensitive behavior gets a focused review. Repairs verify the original
  blockers and direct regressions only, not another general audit.
- **Stage and impact select checks.** Set `delivery.stage` to development,
  module, integration or delivery. Declare each task's `impact: local`,
  `shared` or `unknown`; shared work needs caller checks in `delivery.checks`.
  Full acceptance is for delivery, broad or unknown impact; local batches use
  task checks and mapped callers. Integration also runs `integration_cmd`.
  Missing declarations conservatively fall back to full checks. Several projects
  can run at the same time on one machine; their full oracles take turns, so
  one never slows another into false failures.
- **Bounded spend.** `max_model_calls` and `run_timeout_s` cap a run. The host
  also carries the agreed delivery budget across restarts in CONTEXT; this
  cumulative planning limit is not enforced by an engine ledger.
- **Irreversible actions need prior approval.** A task whose declared files
  hit `irreversible_globs` runs only with `<!-- task:ID approved: <who/date> -->`.

### 2.5 Reading a stop

| Stop reason | Meaning |
|---|---|
| `queue_empty` | The batch closed at its configured stage; this is not automatically product delivery |
| `budget` / `budget:model_calls` / `run_timeout` | A configured limit with work left; start another run if you want more. The review and the full oracle wait for the package boundary |
| `blocked:<id>` / `admit_refused:<id>` | The task needs decisions or a smaller shape |
| `no_progress:<id>` / `not_done:<id>` | The system refused to record work it could not verify; read the log under `.gate/runs/<run-id>/` |
| `worker_left_changes:<id>` | Two attempts left the tree dirty without closing the task |
| `scope_request:<id>` / `prompt_too_large:<id>` | Widen or split the task |
| `no_workers` | Every CLI is benched; wait for the cooldown |
| `review_failed:<ids>` | Current-scope safety/due blockers or unclear acceptance; consolidate evidenced repairs tagged `origin: review` |
| `review_loop:<location-or-task>` | Recorded repair blockers show no progress; diagnose the cause before dispatching again |
| `stage_acceptance_failed:<ids>` | A selected check failed or a due defect is unresolved; read the named logs and defect records |
| `full_acceptance_failed:<ids>` | The full oracle fails after the run; its whole output is in `.gate/runs/<run-id>/full-acceptance.log`; admit one repair row |
| `needs_approval:<id>` | Add the task's `approved:` line after reading its scope |

**Stopping is good. The only unacceptable outcome is a green light that lies.**

At a planned checkpoint without new implementation tasks, use
`flow verify --stage module --tasks A,B` or `flow verify --stage delivery`.
New projects start in development; old configs keep full acceptance until an
explicit stage is set. Read [autonomy.md](autonomy.md) for the complete mapping,
defect-resolution and migration contract. A local PASS never substitutes for
the delivery candidate's complete check.

## 3. The project is getting messy

```powershell
flow audit <project-dir> --out <somewhere-else>/audit.md
```

Read-only: nine categories, oversize-active-document warnings, duplicate file
groups. **It only proposes; v1 has no write mode at all.** When files really
should move, a human approves one batch at a time, starting with the
`generated` and `runtime` categories. The report may not be written inside the
audited tree (the script refuses).

## 4. Switching sessions / handing off

Tell your agent `$flow switch session`. It rebuilds `CONTEXT.md` (≤200 lines,
five questions: goal and red lines / what is being worked on / last checkpoint /
next step / decisions waiting on a human). **CONTEXT.md is the handoff document;
there is no separate long handoff write-up.**

The agent also considers a handoff on its own at three phase boundaries:
research done and implementation about to start, a work package closed, a
switch to another task. It is advice. The agent uses a context-usage figure
only when its CLI states one; without one the usage is "unknown", never a
guessed percentage, and a count of tool calls never forces anything. It does
not hand off in the middle of a failing test or a half-made edit: it first
writes the recoverable state into `CONTEXT.md`. An interrupted task keeps its
failing command, its uncommitted paths and the path of its log under "Now",
and is never described as done. Compaction stays the CLI's own feature; this
workflow installs no hook for it and prunes no transcript.

### 4.1 Lessons

When you correct the agent, or a failure is reproduced and its fix verified,
the agent may add one row to the Lessons table of that work package's
`evidence.md`: the observed fact, the proposed rule, the evidence (commit,
file and line, test or log), a status and a date. A row is a candidate and
changes nothing. It becomes a rule only when you approve it; the agent then
writes it once into `AGENTS.md` or an ADR and records that destination in the
row. Lessons stay in the repository they came from; nothing is promoted to a
global skill or to another project unless you say so. Your standing
instructions always win over a lesson.

## 5. Troubleshooting

| Symptom | Fix |
|---|---|
| `flow` command not found | Open a new terminal; confirm `~/.local/bin` is on PATH |
| The pre-commit hook blocked a commit | Read the reason it printed. **Do not use `--no-verify`** — the block is the product; a bypass is recorded as FAKE_COMPLETION by `flow audit-gate --repo . --last 30` |
| `admit` refuses everything with `no-oracle` | `verify_cmd` in `.gate/config.json` is empty, or the last acceptance run was VACUOUS; set it or fix it |
| `doctor` says the hook is not installed | `flow install-hook --repo .` |
| `flow audit .` refuses to write the report | Expected — the report may not land in the audited tree. Pass `--out` pointing outside the project (desktop, temp directory) |
| Regression self-test | `bash "$(flow home)/gate/selftest.sh"` (expect 10/10) |

## 6. Uninstall

Delete `.git/hooks/pre-commit`, `.gate/`, and `~/.local/bin/flow*`. For the
skills, delete `~/.agents/skills/{flow,flow-run,run-queue,model-debate}` and the
same-name junctions or copies under `~/.claude/skills` and `~/.codex/skills`.
Project documents (CONTEXT, queue, work packages) are ordinary files; keep or
discard them as you like.
## Worker models

Each worker CLI runs whatever model its own configuration selects, unless
`.gate/config.json` pins one in `worker_cmds` (the full command line) or, for
the `pi` worker, in `worker_models.pi` (an OpenRouter model id). The engine
never reads the host session's model; a queue runs the same way from any CLI.
