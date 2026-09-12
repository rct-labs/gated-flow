# gate — the acceptance gate a lying commit cannot pass

Stdlib Python, no external dependencies. It exists to close one specific hole.

An earlier unattended orchestrator marked roughly 60% of its tasks as done by
committing only the queue file: the status flag plus the existence of a commit was
treated as evidence, with no automatic check that the commit touched the declared
files. There *was* a gate, but its judgement had a hole, and those commits went
through it. This gate exists so that completion is derived from the diff and an
exit code, never from a model's claim.

Everything else in an unattended pipeline is convenience. This is the part that must not
be a model's promise.

## Why a git hook and not a CLI plugin

A `pre-commit` hook is enforced by git, so it works identically in Claude Code, Codex,
Grok Build, Kimi Code, and your own hands. No plugin, no framework, nothing to maintain
per tool. It is also exactly the repair the postmortem asked for: a mandatory diff check
before every task commit.

## Install

```sh
python gate.py init         --repo <project>   # writes .gate/config.json
python gate.py install-hook --repo <project>   # writes .git/hooks/pre-commit
python gate.py doctor       --repo <project>   # check it parsed your queue
```

Uninstall is one command: delete `.git/hooks/pre-commit`.

## What it checks, at commit time

The gate only engages when a commit **flips a task to DONE** in the queue file. Ordinary
commits, and commits for other projects in the same repo, pass untouched.

| Gate | Blocks |
|---|---|
| **scope** | a commit that touches the queue *and* paths outside `project_prefix` — the "don't drag my other projects into this history" rule |
| **fake completion** | a DONE flip whose diff contains only documentation (the fake-completion pattern) |
| **frozen oracle** | a DONE flip that also edits a path in `frozen_globs` — weakening the test is how a loop converges on rewriting its own oracle |
| **no verdict** | a DONE flip with no recent `gate.py verify` result |
| **count mismatch** | the queue claims end baseline *N* while the acceptance command measured *M* |

The first three are derived from the diff itself, so there is no field to lie in.

### Optional continuous quality hook

Projects may opt in with a strict `quality` object in `.gate/config.json`.
When enabled, gate calls the shared provider-neutral `continuous-quality`
runner (provided by a separately installed skill that is not part of this
repository; absent by default) for configured deterministic `commit` checks on
every staged commit and for `task` checks during `gate.py verify`. Required
failures block by exit code; advisory failures are recorded without blocking.
The runner owns policy, snapshots, and Project Scorecard composition under
`.gate/quality/`; gate knows only the lifecycle phase and process result. With
no `quality` object, behavior is unchanged.

This repository does not ship that runner. Gate looks for it at
`CONTINUOUS_QUALITY_SCRIPT` or under `<agent skills dir>/continuous-quality/scripts/continuous_quality.py`
(`~/.agents`, `~/.codex`, `~/.claude`, `~/.grok`). If a project enables
`quality` and no runner is found, the phase is recorded as `FAIL` with exit
code 127 and a required check blocks — so leave `quality` out of
`.gate/config.json` unless you provide a runner with that contract.

## Related Codex tasks: optional session reuse

Enable per project with `"session_reuse": {"enabled": true}`. Defaults are
`max_tasks: 3`, `max_input_tokens: 1000000`, and
`context_files: ["AGENTS.md", "CLAUDE.md"]`. Add any other shared rule files to
`context_files` (project-relative paths). Aggregate input tokens include cached
tokens across calls: this is a conservative work budget, not context occupancy.

Explicitly group only adjacent, related Codex tasks:

```markdown
<!-- task:WP-1 session: docs/work/widget -->
<!-- task:WP-2 session: docs/work/widget -->
```

`docs/work/widget/spec.md` must exist. One CLI process still executes one task.
The next process resumes the exact ID from Codex JSON events only after fresh
PASS evidence and a passing independent judge without revision. Reuse also
requires a first-attempt success, unchanged shared instructions, matching HEAD
and dirty-file contents, and remaining budgets. Missing metadata, custom Codex
commands, irreversible tasks, retries, and non-Codex workers use fresh sessions
or the existing dispatch path. A disabled/skipped judge cannot seed a checkpoint.

The runner compares file contents, not status flags. Background includes the
package spec, configured context/rules, project gate config, and Codex config.
On Python 3.11+, TOML normalization ignores newly registered `trusted` project
entries (Codex adds these at startup), while retaining `untrusted` entries and
all other settings. Older Python uses conservative raw-file hashing.
An unchanged pre-existing dirty file is allowed; a second edit invalidates reuse.
Markers express shared background, not authority to skip admission or tests.
Queue scope and authorization rules still apply to each task independently.

State is in memory during a run. `.gate/runs/<run>/session.json` is an audit
snapshot, never a recovery input; a new run always starts fresh. The journal
adds `session_checkpoint`, and RUN-REPORT contains Sessions with input/cache
counts. No benefit is inferred when telemetry is absent. Long command output
belongs in task-specific `.gate` logs with exit status and useful excerpts
returned to the model; this is prompt guidance, not a command-output interceptor.

Run `python -m unittest discover -s gate -p "test_*.py" -q` from the repository
root for regression coverage, including resume, invalidation, revision, and
partial-work stopping. Change session policy separately from verification
policy when comparing performance.

## Admission — before a task ever enters the loop

The postmortem of the retired orchestrator asked for three things: exit-code
verdicts, a diff guard, and admission control. The gate above covers the first
two by catching a bad attempt; admission keeps out-of-sweet-spot tasks (small /
isolated / implicitly verifiable) out of the unattended loop entirely. Scope is
declared per task in the queue file:

```markdown
<!-- task:WP-7 files: src/foo.py, tests/test_foo.py -->
```

```sh
python gate.py admit --repo <project>            # three-state report, read-only
python gate.py run --repo <project> --strict-admit
```

`admit` reports `ok` / `REFUSE` (too many files, overlap with another TODO
task, human-decision wording, no oracle, unknown worker) / `UNDECLARED`. `run` warns by
default; `--strict-admit` or `"strict_admit": true` stops on a refused head
task. Flip a project to strict only after one advisory cycle has been
human-checked for false refusals. Knobs: `max_task_files` (6),
`human_decision_regex`, `strict_admit`.

### Declared order (2026-09-02)

Two strictly sequential slices may share a file — the second extends what the
first created — without tripping the overlap refusal, if the queue says so:

```markdown
<!-- task:CON-2 after: CON-1 -->
```

`admit` then ignores the CON-1/CON-2 overlap, and refuses `out-of-order` if a
dependency sits *after* its dependent in the queue while still open, or
`unknown-dependency` if it is not in the queue at all. Overlaps between tasks
with no declared order are still refused: that rule exists to stop independent
tasks stepping on each other.

### Per-task worker pin (2026-09-02)

A queue table may carry an optional `worker` column. The value on a row is the
CLI that task goes to first; an empty or dash cell means "no pin":

```markdown
| # | ID | name | status | worker | start baseline | end baseline |
|---|---|---|---|---|---|---|
| 71 | CON-1 | plain console shell | `TODO` | codex | 34 passed | 35 passed |
| 72 | CON-2 | executor contract doc | `TODO` | kimi | 35 passed | 35 passed |
```

Rules, all enforced in `gate.py`:

- The column index is **per table**. Older tables without the column are
  untouched; a later header without it resets the pin.
- Dispatch order is `[pin] + workers` from `.gate/config.json`, deduped, minus
  benched or uninstalled tools. So a pin works even when it is not in the
  run-wide list, and a benched pin falls through to the run-wide list; the
  retry rotation then continues down that order.
- `admit` refuses `unknown-worker` when the id is not a key of `WORKER_CMDS`
  or `worker_cmds` (`claude`, `codex`, `kimi`, `grok`).
- `attempt_start` journal lines carry `pin` and `pinned`; the run report has
  `worker` and `pin` columns, so a fallback dispatch is visible after the fact.
- Preflight: the run only dies with `no usable worker` when neither the
  run-wide list nor any pinned worker is usable.

Precedence, top wins: row `worker` column > the run-wide `workers` list (which
is what a verbal pin in the flow-run skill fills) > host CLI default.

`run` binds every dispatch to the selected task ID. A retry may resume that
same task, but it may never fall through to a later TODO. The runner compares
the committed DONE transition with the dispatched ID and stops with
`task_mismatch` if a worker changes another task. A real BLOCKED transition
stops immediately instead of dispatching a retry.

Tool outages (quota and `.git/index.lock` permission denial) do not consume the
task attempt budget. The runner restores only the dispatched task's queue row,
benches the failing worker, and rotates to another worker. It refuses automatic
recovery if HEAD moved, another task changed, or non-queue worktree changes were
left behind.

The built-in Codex writer command uses `--sandbox danger-full-access` because
Codex `workspace-write` intentionally protects `.git/**` as read-only while the
gate contract requires workers to create claim and completion commits. Projects
may pin or override this under `worker_cmds.codex` in `.gate/config.json`.

## Autonomy: probe, judge, revise, approve (2026-09-03)

Design: `../docs/autonomy.md`. Four additions to `run`, all config-gated:

**Usage probe** (`probe`, default on). At run start every candidate CLI — the run-wide
list, row pins, and the judge chain's tools — gets one tiny real request
(`gate.py usage --repo <project>` does the same on demand). A quota-shaped reply
benches it for `quota_cooldown_s`; a probe that never reached the provider (timeout,
not installed, other error) benches it for `probe.retry_s` (600) only, so a local
failure cannot occupy the quota window. One probe per CLI per run, lazily repeated
when a cooldown expires mid-run. `tool-status.json` records `reason: probe:<why>`.

**Judge after every task** (`judge`, default **off**; opt in per project). After
`task_done` the runner spawns a read-only judge — chain `fable → opus → codex`, next
member on outage or unparsable output — with the task's declared files, commits, and
acceptance tail. The chain members and their command lines are configuration defaults
(`judge.chain`, `judge_cmds`), not a fixed contract; replace them as models change.
It scores 0–100 against the owning spec (acceptance 40 · real tests
20 · scope discipline 15 · conventions 15 · regression risk 10) and returns strict JSON
(`score`, `verdict` pass|revise|escalate, `findings[]`, `revision_brief`).
The judge never touches the queue row: DONE was earned by the exit-code gate and stays.

**Bounded revision.** `revise` dispatches a fix to a worker *other than* the one that
made the last commit (rotation), at most `judge.max_revisions` (2) times — initial + 2
= 3 rounds, where self-correction's per-round gain drops under 2% — and stops early
when the score gains less than `judge.min_gain` (5). A revision commit is not a DONE
flip, so `check-commit` does not engage; the runner re-runs the acceptance command
itself. HEAD moved + FAIL → `revision_broke_verify:<id>`, nothing is auto-reset.
`escalate`, cap, or no progress → `judge_escalated:<id>`; the report carries the score
history and last findings. Whole chain down → `judge_skipped`, run continues, report
says so under **Waiting on you**.

**Reversibility tier** (`irreversible_globs`). A TODO task whose declared files match
(migrations, `data/**`, hooks, `.claude/settings*`, apply scripts) is refused
`needs-approval` by `admit` (printed `APPROVAL`) unless the queue carries
`<!-- task:ID approved: <who/date> -->`. `run` stops on it with `needs_approval:<id>`
**even in advisory mode**. Approval is per task id and therefore single-use.

Exit code 3 now also covers `judge_escalated`, `revision_broke_verify` and
`needs_approval`. Journal events added: `probe`, `judge_start`, `judge_verdict`,
`judge_skipped`, `revision_start`, `revision_end`, `judge_escalated`, `needs_approval`.
Regression suite: `python -m unittest test_autonomy` (20 cases, each mutation-checked:
strip the feature, its test goes red).

## What it checks, after the fact

```sh
python gate.py verify --repo <project>            # run the real acceptance command
python gate.py audit  --repo <project> --last 80  # re-derive the verdict over history
```

`verify` runs the acceptance command in the project directory, records the exit code and
the count, and marks a zero-count PASS as `VACUOUS` — an oracle that asserts nothing is
not an oracle.

`audit` re-derives the structural verdict from git history alone. A commit made with
`--no-verify` shows up here. That is the design: the hook makes fake completion hard, the
audit makes it *visible*, and neither depends on anything a model wrote.

## Config (`.gate/config.json`)

```json
{
  "project_prefix": "my-project",
  "queue_file": "TASK_QUEUE.md",
  "doc_only_globs": ["TASK_QUEUE.md", "NEXT_SESSION.md", "NEXT_SESSION.*.archive.md", "SESSION.md"],
  "verify_cmd": "uv run pytest tests -q",
  "count_regex": "(\\d+)\\s+passed",
  "frozen_globs": [],
  "verdict_max_age_s": 5400,
  "require_verdict_on_done": true
}
```

`project_prefix` is repo-relative and may be empty when the repo holds one project.
`frozen_globs` is empty by default; set it to `["tests/*"]` for a refactor package whose
acceptance is "the existing tests must not move".

`verify_steps` (optional) declares the same oracle as ordered pieces, and is honoured
only when the pieces joined with ` && ` are byte-for-byte `verify_cmd` — it can split
the oracle, never redefine it:

```json
"verify_cmd": "pnpm typecheck && pnpm vitest run",
"verify_steps": [
  {"name": "typecheck", "cmd": "pnpm typecheck"},
  {"name": "tests", "cmd": "pnpm vitest run"}
]
```

`verify` then runs the steps in order and stops at the first red one (a failed
typecheck does not earn a ten-minute test run), recording each step's exit, time and
tail in the verdict. `verify --step <name>` runs one step per call for shells with a
hard per-command cap: the verdict stays `PARTIAL` (not accepted by the DONE gate)
until every step has passed over the *same* inputs — the tree is captured before and
after each step and compared to the chain's start, so an edit between steps yields
`CHAIN_BROKEN` and the chain restarts from the first step. The recorded `cmd` is
always the canonical `verify_cmd`, so receipts and allowances see one oracle.

`GATE_CONFIG=<path>` points the gate at a repo without writing into it — use it to
`audit` a project before deciding to install.

## Verified behaviour

Ten cases, run against a synthetic repo that reproduces the fake-completion shape
(`selftest.sh`):

1. queue-only DONE flip → **blocked** (fake completion)
2. queue + a path outside the project → **blocked** (scope)
3. real code, no verdict → **blocked**
4. verdict PASS but count 99 vs queue's 12 → **blocked**
5. code + matching verdict → **passes**
6. unrelated commit in another project in the same repo → **passes untouched**
7. DONE flip that also edits a frozen test path → **blocked**
8. `--no-verify` bypass → commit succeeds, `audit` reports `FAKE_COMPLETION`
9. `admit` report → three-state ok / REFUSE / UNDECLARED per TODO task
10. `run --strict-admit` on an undeclared head task → stops with `admit_refused`, no worker spawned
# Explicit task execution profiles

Unattended calls require queue-bound model and reasoning profiles. See
[the execution contract](../docs/execution.md) for defaults, per-task overrides,
`lock-execution`, compatibility and controlled updates. Verification and
inspection do not require a model profile; inference does.
