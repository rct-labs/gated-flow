# gate — the acceptance gate a lying commit cannot pass

One file, stdlib Python, no dependencies. It exists to close one specific hole.

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
| **wrong task** | a task-scoped verdict (`verify --task ID`) closing a different task, or a declared file of that task edited after its local check |
| **count mismatch** | a queue-scoped verdict whose measured count differs from the end baseline the queue claims |

The first three are derived from the diff itself, so there is no field to lie in.

A task may admit its own local check in the queue:

```markdown
<!-- task:WP-7 verify: {"cmd": "pytest tests/test_a.py -q", "timeout_s": 900} -->
```

`gate.py verify --task WP-7` runs that command and records a task-scoped
verdict bound to the bytes of WP-7's declared files. The hook accepts the DONE
commit while those bytes are unchanged; it never inspects the environment, the
interpreter or the Git toolchain, so verifying in one shell and committing from
another is fine. `gate.py verify --queue` runs the full `verify_cmd`; the runner
does that once per run after the package review.

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

Run `python -m unittest discover -s gate -p "test_*.py" -q` from the repository
root for regression coverage: hook gates, task-local verify, package review,
budget and partial-work retry.

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

## Autonomy: probe, review once, accept once, approve

Design: `../docs/autonomy.md`. Config-gated additions to `run`:

**Usage probe** (`probe`, default on). At run start every candidate CLI (run-wide
list, row pins, review chain) gets one tiny real request; `gate.py usage` does the
same on demand. A quota-shaped reply benches it for `quota_cooldown_s`; a probe that
never reached the provider benches it for `probe.retry_s` only.

**Task packet.** Each worker prompt carries the queue row, declared files, the local
check command, the spec's acceptance section and `git diff --stat`, capped at
`worker_packet_max_bytes` (6000). A larger packet stops the run with
`prompt_too_large:<id>` instead of being sent.

**Partial work.** A worker that exits with uncommitted changes gets one retry on the
same tree with a hint; a second attempt that still does not close the task stops the
run with `worker_left_changes:<id>`. A worker that asks for a scope decision stops the
run with `scope_request:<id>`.

**One review per batch** (`judge`, default off). When the queue has no eligible TODO
left, the review chain (`judge.chain`, next member on outage or unparsable output) reads
once everything closed since the last full acceptance, earlier runs included, and returns
strict JSON (`score`, `verdict`, `findings[]`, `revision_brief`). A member that answers
`escalate`, score 0, no findings has said it could not read the repository: that is
`<member>:no_access`, and the next member reviews. Pass means no current-scope
safety or due-stage defect and no unresolved escalation; a `revise` recommendation
alone does not block. The score is recorded only. The review never writes queue
rows: defects go to `REVIEW-NOTES.md` with stable ids, due checkpoints and status.
The host groups ordinary defects at planned checkpoints. Stage checks follow.
A run that ends with TODO rows left defers both. Full oracles of different projects
take turns on the machine (an OS lock under `~/.gate/`, config
`serialize_full_acceptance`); tasks never wait. A failed review stops with
`review_failed:<ids>`. Related blockers are repaired together. Repair rows
(`origin: review`) receive targeted verification of original findings and
direct repair regressions, not another general audit. The same recorded blocker
set persisting on a repair stops with `review_loop:<location-or-task>`;
a chain outage journals `review_skipped` and the run continues. Rows in
`judge.checkpoints` are reviewed alone right after they close. There is no revision
loop: the host requires explicit user authorization for further repair,
after the first repair batch. Renaming defects does not erase history. Small tools
default to acceptance without model review; sensitive behavior receives focused
review at the delivery boundary. Explicit project requirements still apply.

**Stage acceptance.** `delivery.stage` selects development, module, integration
or delivery. Task `impact` declarations and `delivery.checks` caller mappings
select local checks. Integration adds its declared command. Delivery, broad or
unknown impact selects full `verify_cmd`. Reports name the reason and untested
scope. A `stage_acceptance` PASS is not full acceptance; a stage verdict cannot
close a task. See [the policy](../docs/autonomy.md) for configuration and defect
metadata. `verify --stage delivery` establishes a new delivery checkpoint.

**Budget.** `max_model_calls` counts every model process of the run (workers,
reviewers, probes); reaching it stops the run with `budget:model_calls`.

**Host-driven review.** `gate.py review --repo <p> --tasks A,B [--base SHA]` runs the
same batch review and stage acceptance for DONE tasks outside a run: after a review
chain outage, or for tasks closed by hand. Windows note: `codex exec --sandbox read-only`
needs its elevation helper, which cannot show a UAC prompt inside a Task Scheduler
session (error 1223). Give the reviewer `-c windows.sandbox=unelevated` in `judge_cmds`.

**Reversibility tier** (`irreversible_globs`). A TODO task whose declared files match
is refused `needs-approval` by `admit` unless the queue carries
`<!-- task:ID approved: <who/date> -->`; `run` stops on it with `needs_approval:<id>`
even in advisory mode.

Exit code 3 covers stops needing host action: `review_failed`, `review_loop`,
`stage_acceptance_failed`, `full_acceptance_failed`, `scope_request`,
`prompt_too_large`, `needs_approval`. Only user-owned decisions need a person.

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


## Repair continuation

Automatic continuation is OFF by default, including when blockers decrease.
Allow one consolidated high-risk/core-flow repair batch and targeted verification;
then stop if still blocked. Ordinary findings are recorded for later maintenance.
Further repair requires explicit user authorization, named tasks and an expiry;
spare budget or a generic continuation request is not authorization. Follow the
continuation contract in `flow-run` and `docs/autonomy.md` before dispatch.
