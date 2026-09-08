# flow / run — user manual

> The manual for people. Design rationale: [design.md](design.md). Machine
> details: [../gate/README.md](../gate/README.md). Unattended-run behaviour:
> [autonomy.md](autonomy.md).
>
> One-sentence mental model: **have an idea, want to continue, want to tidy up →
> `$flow`; let the already-queued tasks run themselves → `$run`.**

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
flow admit --repo .
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
refused task stops the run outright.

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

- **Probe before dispatch.** At run start every candidate CLI gets one tiny real
  request (`flow usage --repo .` shows the same table on demand). A quota-shaped
  reply benches the CLI for the cooldown period; a probe that never reached the
  provider benches it for only ten minutes and does not occupy the quota window.
- **Judge every task as it closes.** A read-only judge chain scores the closed
  task 0–100 against its spec; ≥85 passes. Below that, preferably a different
  worker revises; one usable authorized worker may repeat with `rotated: false`
  recorded. Revision is limited to 2 rounds (initial + 2 = 3 rounds; the per-round gain of
  self-correction drops under 2% after that), with an early stop when the score
  gains less than 5. After each revision the runner re-runs the acceptance
  command itself and stops with `revision_broke_verify` if it now fails. The
  judge never edits a queue row: DONE is earned by the acceptance command.
- **Irreversible actions need prior approval.** A task whose declared files hit
  `irreversible_globs` (migrations, `data/**`, hooks, settings) is dispatched
  only when the queue carries `<!-- task:ID approved: <who/date> -->`; otherwise
  the run stops with `needs_approval`, even in advisory mode. Approval is per
  task id: an already-approved concrete scope may cover a repair, with the
  original instruction and scope mapping recorded for the new ID. Never blindly
  copy another task's approval or infer authorization for new effects.
- `judge.enabled` defaults to `false`; each project opts in. `probe` defaults
  to on.

The judge chain and the worker set (`fable`, `opus`, `codex`, `kimi`, `grok` in
the shipped defaults) are configuration in `.gate/config.json` — `judge.chain`,
`judge_cmds`, `workers`, `worker_cmds` — not fixed properties of the engine.
Change them there when your CLIs or model names differ.

### 2.5 Reading a stop

| Stop reason | Meaning |
|---|---|
| `queue_empty` | Everything ran |
| `blocked:<id>` / `admit_refused:<id>` | The host reads the blocker and reshapes an authorized scope; only missing user decisions require input |
| `no_progress:<id>` / `not_done:<id>` | The system refused to record work it could not verify — read the log under `.gate/runs/<run-id>/` |
| `no_workers` | Every CLI is quota-benched; wait for the cooldown (`.gate/tool-status.json`) |
| `judge_escalated:<id>` | The task is DONE but review failed — the host reads findings and automatically admits a supported in-scope repair before dependents under existing completion authorization; preserve failed review and never accept below-threshold quality |
| `revision_broke_verify:<id>` | A judge-driven revision commit broke acceptance or left the tree dirty — look at `git log` / `git status` first; nothing is rolled back automatically |
| `needs_approval:<id>` | The task lacks required approval evidence — the host checks existing authorization for this exact scope before asking, then records the task-specific `approved:` line |

The host reads **Waiting on you**, judge findings and logs before deciding whether
a user decision is needed. Under existing completion authorization, a clear
in-scope repair is admitted and launched automatically with the same gates and
limits. Repeated same-cause failure requires bounded diagnosis and a materially
different supported correction, not a blind rerun. See the run-queue skill's
**Host recovery after a stopped run** procedure.

**Stopping is good. The only unacceptable outcome is a green light that lies.**

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
