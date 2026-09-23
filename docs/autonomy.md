# Staged acceptance and bounded unattended execution

The host plans and interprets evidence; `gate.py` executes admitted work. A task
DONE means its implementation passed its admitted check, not that the product
is ready to deliver. Do not manufacture tasks to keep a review busy.

## 1. Stage, impact and risk

| Stage | Objective | Default checkpoint |
|---|---|---|
| development | A runnable user flow; tolerate recorded ordinary defects | Task checks and mapped affected callers |
| module | The module behaves correctly within its supported inputs | Module checks and mapped affected callers |
| integration | Interfaces and critical cross-module user flows work | Scoped checks plus the declared integration command |
| delivery | The candidate meets the delivery promise | Complete `verify_cmd` and any required risk review |

`delivery.stage` is set before a batch starts. New `flow init` projects start
in development. Existing projects with no declaration retain delivery/full
acceptance, so installing a new engine cannot silently weaken their checks.
A stage transition is a planned milestone, never a way to bypass a failure.

Keep observable acceptance, supported inputs, non-goals, the delivery budget
and defect checkpoints in the existing work spec. CONTEXT carries current
stage, actual progress and time spent/remaining across sessions. No second
queue, workflow directory, or permanent budget service is introduced.

Ordinary small reversible tools leave `judge.enabled` off unless the project
explicitly requires it. Sensitive data, money, permissions or destructive
behavior justify focused review of those invariants. Explicit comprehensive
audit requests keep their requested scope. Do not review every module as if
it were a fresh whole-project security audit.

## 2. Test selection

Each task declares its implementation files, local check and impact:

```markdown
<!-- task:WP-1 files: src/export.py, tests/test_export.py -->
<!-- task:WP-1 verify: {"cmd": "pytest tests/test_export.py -q", "timeout_s": 900} -->
<!-- task:WP-1 impact: local -->
```

Choose `local`, `shared`, or `unknown` from the actual callers, contracts and
data effects, not the number of files or directory name. Shared changes need
explicit test mappings covering the declared paths, including affected callers.
No static dependency inference is promised by this engine.

```json
"delivery": {
  "stage": "module",
  "checks": [
    {"files": ["src/export.py", "tests/test_export.py"],
     "cmd": "pytest tests/test_export.py tests/test_export_callers.py -q",
     "timeout_s": 900}
  ],
  "full_globs": ["src/shared/**", "requirements.lock"],
  "integration_cmd": "pytest tests/integration -q"
}
```

At queue exhaustion, the runner checks the current stage. For development or
module work it deduplicates admitted task commands and selected mapping commands.
Integration adds `integration_cmd`. Commands execute separately, stopping on a
failure; no string-built shell pipeline joins them.

Full acceptance is selected for delivery, unknown/missing impact, a missing
local check, shared paths without a complete caller mapping, a `full_globs`
match, undeclared actual code changes, or integration without its declared
command. The reason appears in the verdict, journal and report. Unknown scope
must not be marked local merely to avoid an expensive test.

Task DONE still requires `verify --task ID`, a real command exit code and the
existing file-byte guard. A scoped stage verdict cannot close an individual
task. `verify --queue` remains the explicit full-suite command. For a planned
stage transition without new implementation tasks:

```bash
flow verify --stage module --tasks WP-1,WP-2 --repo <project>
flow verify --stage delivery --repo <project>
```

A manual scoped check trusts the listed tasks' admitted impact; use the runner
or `review --tasks ... --base SHA` when actual commit-range checking is needed.
Neither an old local PASS nor an old full PASS proves modified code correct.
Run the checks for the new impact and use a complete candidate check at delivery.
There is no acceptance cache.

Full verdicts also fingerprint the project content (including non-ASCII paths),
excluding gate output and task status documents. A later code change invalidates
that verdict at the commit hook; a check that changes candidate content is not
a passing validation of a stable candidate. No environment hashing is used.

Only complete-suite commands take the machine-wide OS file lock under
`~/.gate/full-acceptance.lock`; task and scoped checks do not wait on that lock.
`serialize_full_acceptance: false` opts out. This lock protects resource use,
not an external permission boundary.

## 3. Review and defect records

Review the agreed change and acceptance. A blocker needs a concrete trigger,
reproduction or traceable code path, and the violated invariant. Missing tests
alone, style, hypothetical unsupported use and future extensibility are not
high findings. Scores are informational. One read-only reviewer is used, with
fallback chain members only for unavailable/unparseable results.

The structured finding fields are `severity`, `file`, `issue`, `fix`, `action`,
`id`, `blocking`, `due`. Existing outputs without new fields remain readable:
missing `blocking` means none and missing `due` means backlog; high still blocks.
Use `high` for demonstrated safety/data-loss defects or a broken current core
flow. Ordinary findings can set `blocking: stage` and a named due checkpoint
when the agreed acceptance requires fixing them by then. Optional suggestions
use `blocking: none`; they never create mandatory delivery work by themselves.
`revise` alone does not block an otherwise passing stage. `escalate` means an
unresolved decision, not permission to invent another repair task.

Findings are recorded in the existing `REVIEW-NOTES.md` next to the queue, with
stable BUG ids, issue/evidence, location, fix, due checkpoint, status and resolution.
Repeated identical findings reuse an id; line-number changes do not create a new
bug. The record is committed separately and never adds queue rows. Older prose
notes remain intact. The host groups ordinary defects by common cause at module,
integration or delivery checkpoints instead of interrupting work for each one.

Structured metadata is a one-line `<!-- defect: {...} -->` comment above the
visible finding. Update the latest record and visible note together. A resolved
record needs `status: resolved` and nonempty `resolution` naming the actual
verification evidence. A status label alone cannot waive a high or due blocker.
A repeated finding reopens a resolved record. Deferral records its reason and
next checkpoint; changing product promises requires the user's decision.

For a bounded local/module check, a located blocker outside the selected files
and mapped caller scope is recorded but does not stop unrelated work. Unlocated
blockers, full acceptance and integration are conservative: all relevant open
blockers must be resolved. The report still lists unresolved records. The host
can continue independent work after a safety stop without claiming that the
blocked capability is ready.

At each checkpoint the gate refuses acceptance when a blocking record is due
and unresolved. Ordinary nonblocking records remain a visible defect list, not
a task generator. Invalid structured records stop with a diagnostic rather than
silently dropping defects. Resolution evidence is an auditable host assertion;
the gate does not infer that a prose claim proves the fix. The selected executable
checks still have to pass.

## 4. Repair verification and progress

Tag repair tasks `origin: review`, preserve original BUG ids and evidence, and
add a failing regression before fixing the cause where practical. The reviewer
checks original defects and direct repair regressions, not unrelated old code
or newly invented requirements. Incidentally encountered serious defects are
reported honestly, never hidden to force a pass.

A repair with unchanged recorded blockers stops as `review_loop`.
A smaller blocker set is progress, but does not authorize another round.
The journal persists repair review results; subsequent review-origin tasks
stop as `repair_paused` without a valid explicit continuation grant.

## 5. Execution, evidence and limits

The existing dispatch guarantees remain: one worker per task attempt, a bounded
task packet, one retry on a dirty tree, quota fallback without spending an attempt,
per-task irreversibility approval, and no unrequested push or PR. Model processes
and probes consume `max_model_calls`; `run_timeout_s` bounds the run. These are
per-run limits. The host preserves the agreed overall delivery budget in CONTEXT
and must not reset it with a new run or task name.

At queue exhaustion the optional review and stage checks cover tasks closed in
this or previous runs. Successful `stage_acceptance` closes only its named tasks;
full acceptance covers the complete candidate. Failed scoped checks leave their
boundary pending, so a later acceptance retry need not repeat a passed review.
A configured review outage is reported as unreviewed, not passed. Tests may still
run, but the host cannot claim a required review was completed.

Reports show stage, selection reason, each selected command/log, unverified scope,
unresolved defect checkpoints and the stopping fact. A task DONE, a stage PASS
and a product delivery are deliberately distinct outcomes.

| Stop | Meaning |
|---|---|
| review_failed | Current-scope safety/due blockers, or an unresolved decision |
| review_loop | Same recorded blockers persist without improvement |
| stage_acceptance_failed | A scoped check failed or due defect records remain |
| full_acceptance_failed | The selected full oracle failed |
| scope_request / prompt_too_large | Re-shape the task before dispatch |
| needs_approval | An irreversible task lacks its own approval |
| budget:model_calls / run_timeout | Per-run limit; also respect the host's delivery budget |

Keep full output under `.gate/runs/<run-id>/`: `full-acceptance.log` or selected
`stage-acceptance-N.log`. `verify` uses `.gate/` directly. Read saved output rather
than rerunning a long test merely to see a traceback.

Measure delivery lead time, time in checks, repeated defects, defect backlog and
post-delivery failures. Fewer reviews alone is not success. Stage records and
existing journal events supply evidence; do not add another monitoring system
just to measure the workflow.

Use `flow admit --plan` before dispatch to preview scope and selected checks.
It catches malformed rows and oversized packets, and reports missing/broad task
checks and earlier scope requests. It cannot infer callers or validate coverage.
The host surveys implementation, caller contracts and tests as one behaviour,
then resolves warnings without weakening promised acceptance. Task checks are
scoped when coverage permits; complete candidate acceptance remains mandatory
at delivery. There is still no acceptance cache.

CLI probes are lazy, once per used tool per run. Admission and packet checks
precede probes; unused fallbacks, historical pins and future reviewers are not
probed at startup. `usage` remains an explicit all-candidate diagnostic.
Budget exhaustion is not a provider outage and cannot close pending review work.

`flow metrics --last 5 [--json]` and the report's Efficiency section expose the
existing journal's timings: startup, probes, CLI requests, checks, lock waits
and scope stops. Worker time includes task checks; the durations overlap.
Legacy missing measures and unfinished calls are unknown. Commands outside the
gate are not measured, and CLI request counts are not provider token/billing
records. Compare similar completed delivery batches and actual defect outcomes;
do not claim a production speedup from a synthetic call-count reduction.


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
