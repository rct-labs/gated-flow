# Staged acceptance and bounded unattended execution

The host plans and interprets evidence; `gate.py` executes admitted work. A task
DONE means its implementation passed its admitted check, not that the product
is ready to deliver. Do not manufacture tasks to keep a review busy.

## Model routing authority

Read `~/.config/mini-model-routing/POLICY.md`
and its authoritative `policy.json` before planning, coding, reviewing or
dispatching. Those files own model/effort eligibility and subscription routes;
these skills do not maintain a second model matrix. Design/review and coding
roles remain separate. An explicit CLI preference does not waive role policy.
Use only existing real subscription/login/TLS routes, with no paid fallback,
credential changes or ceremonial inference/quota probes. Unknown quota remains
unknown; a backup requiring exhaustion is not eligible on missing evidence.

Before admitting a new package, reconcile worker commands/models, worker and
revision prompts, review contracts and queue pins against that policy. Preserve
closed configs/receipts, raw failed attempts and returned model metadata. Do not
change a running config. A native `model_policy:` refusal is a configuration
problem to reconcile while stopped, never a reason to weaken validation. Custom
worker keys need verified local executable/flags and edit/shell/git capability;
local help is not proof of live entitlement. Observe availability at the first
useful authorized call, not an extra probe. Required review stays independent.

### Required local configuration

Before running implementation workers, create
`~/.config/mini-model-routing/policy.json` and a companion `POLICY.md` describing
your authorized model routes. The engine resolves the policy from `Path.home()`;
it refuses missing, malformed or duplicate-key policies and mismatched commands
before launching a worker or charging a call. The JSON schema is version 1:

```json
{
  "version": 1,
  "coding": {
    "codex": {"model": "gpt-6.1-sol", "effort": "medium"},
    "gemini": {"model": "gemini-4-hydrogen-high", "effort": "high"},
    "claude_fallback": {
      "model": "claude-sonnet-5-5", "effort": "medium",
      "only_when": "primary_coding_unavailable"
    }
  }
}
```

These example pins must match your authorized, available subscription routes.
All three coding entries are required. Worker argv must explicitly pin exactly
one model and effort matching its policy entry; unresolved placeholders,
wrappers and route overrides are refused. Claude implementation additionally
requires `model_policy_fallback_reason: primary_coding_unavailable` in the
stopped package config, backed by actual primary-route unavailability. Keep
review models in the design role under your written policy and preserve any
project-required independent review. The test fixture in `gate/test_policy.json`
is isolated test data, never an installed or automatic fallback policy.

## Adaptive workflow

The host selects the smallest sufficient process from observed behavior, without
requiring a mode flag or asking the user to choose workflow machinery:

| Workflow | Evidence and action |
|---|---|
| simple | Small reversible local change: one usable slice, decisive acceptance; retain explicit project review requirements |
| scoped | Shared behavior or affected callers: survey implementation/callers/tests, bounded tasks and mapped checks, milestone acceptance |
| sensitive | Data, permissions, money, destructive operations or difficult rollback: preserve irreversible approvals and a focused independent delivery review |

Risk and impact are separate: few files can still affect sensitive behavior.
Unknown impact expands verification. Use existing queue/spec/config/journal seams;
no classifier, extra agent framework or duplicate state store. A status-only
question is read-only; explicit continue/finish requests resume only authorized
work. Analysis-only, planning-only, standalone debate and paused-business requests
never authorize launch. Explicit task-worker instructions outrank host routes;
workers do not recursively load or launch the host workflow.

Per-task irreversible approval remains necessary even within an authorized goal.
Keep irreversible tasks at the queue tail and add their `approved:` line only
from actual user authorization. Preparation or an audit alone never authorizes
moves, deletion, activation or deployment. No acceptance/verify weakening to
force admission; no silent packet truncation. Optional verified handoff anchors
must be checked for related-file drift; stale assumptions require rereading.
Host resume/recall/history stays out of bounded worker packets; acceptance already
embedded in a current packet need not be redundantly reread. `external_context`
projects retain their own handoff conventions. Follow the
[continuity procedure](../skills/flow/references/continuity.md) for atomic
checkpoints and stale-anchor reconciliation.

The host reports meaningful changes: task completion, a blocker, a changed
approach, review or acceptance. Empty polls and repeated transcript reads add no
evidence. Use one journal cursor and saved logs; obey host responsiveness limits.
Detached execution uses the platform procedure in
[run-queue launch](../skills/run-queue/references/launch.md): a real user-owned
Linux transient systemd service where available, Windows Task Scheduler otherwise
on Windows. Never install a scheduler or start business work as a side effect.

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

Task packets render executable local checks using the running Python interpreter
and platform shell quoting, including paths with spaces; no `python` PATH alias
is assumed by the generated command. Handoff freshness requires every literal
related path to exist at both the anchor and current tree, with clean related
Git diff/status. Existing directories are supported; missing paths, additions,
deletions and unavailable/nonancestor anchors remain stale or unverified.

## Critical safety assumptions

For changed behavior, record at most the few decisive safety assumptions in the
existing spec: the invariant, affected caller or running surface, evidence and
remaining risk. Start with source inspection, strengthen it with a safe executable
reproduction, then check the relevant running surface when available and authorized.
Use the strongest inexpensive evidence that can decide the assumption; a broad
hypothetical audit is not the default. Do not run destructive or unauthorized
reproductions. Unavailable checks and unresolved material risks stay explicit in
the package evidence and applicable defect records; they do not disappear because
a cheaper check passed.

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

Use a bounded packet of the actual base-to-candidate change manifest, relevant
acceptance, verification and open defects; record both hashes, scope and explicit
omissions. Original diffs/source remain available to the independent reviewer.
Packet inclusion never proves read coverage. Prefer one focused delivery review;
repair review covers the repair diff, original finding IDs and direct regressions.
Reuse a prior parsed review only with a compatible contract/stage and verified
ancestor anchor; changed policy/scope, invalid baseline or delivery requires the
full applicable review boundary. Preserve required independent-review contracts.

Review the agreed change and acceptance. A blocker needs a concrete trigger,
reproduction or traceable code path, and the violated invariant. Missing tests
alone, style, hypothetical unsupported use and future extensibility are not
high findings. Scores are informational. One read-only reviewer is used, with
fallback chain members only for unavailable/unparseable results.

In the existing finding's issue text, include the concrete trigger, exact safe
reproduction command if run, observed versus expected result and violated invariant.
When execution is unsafe or unavailable, cite the source path and traceable code
proof, label the reproduction unverified and explain why. Do not drop a suspected
material defect or automatically lower severity just because it was not executed;
assess the evidence against the same severity rules. Merge findings with the same
root cause, preserving stable IDs and scoped repair review. No new finding schema
or requirement to execute every suspected issue is introduced.

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

Admit repairs in the existing queue with both metadata comments, mapping each
repair to the original reviewed task IDs (comma separated for multiple originals):

```markdown
<!-- task:WP-7-FIX origin: review -->
<!-- task:WP-7-FIX review_of: WP-7 -->
<!-- task:WP-7-FIX files: src/loader.py, tests/test_loader.py -->
```

Targets must be existing original rows, not another review-origin repair or the
repair itself. Duplicate, missing or invalid mappings require full review; an
`after` declaration orders tasks but does not establish review ancestry. Preserve
original BUG IDs and evidence, and add a failing regression before fixing the
cause where practical. The reviewer
checks original defects and direct repair regressions, not unrelated old code
or newly invented requirements. Incidentally encountered serious defects are
reported honestly, never hidden to force a pass.

A mapped repair derives its original base and latest relevant complete parsed
receipt from the journal. Packet integrity and both base-to-reviewed-head and
reviewed-head-to-candidate ancestry must pass. The stable contract includes only
the relevant original task specs/acceptance, scope, checks, impact and approvals,
plus reviewer template, owner policy and semantic safety/verification settings.
Queue status, additional mapped repair rows, bounded continuation grants and
execution budget accounting do not alone change that contract. Unrelated old
specs do not enter it. Semantic changes require full review.

An incremental prompt requests the prior reviewed head to candidate diff first,
retains the original whole-range manifest for navigation and preserves original
findings/blocker IDs and direct-caller inspection. Delta code must be declared
by the repair; previous changes must be covered by the validated prior scope.
Declared direct repair paths may add regressions/callers. Undeclared code,
invalid baselines, delivery stage, high-risk paths and configured `full_globs`
or `irreversible_globs` force full review. Operational-only edits to the active
local gate config can retain a matching contract; safety overrides still win.

A repair with unchanged recorded blockers stops as `review_loop`.
A smaller blocker set is progress; the host assesses further in-goal work against
existing authorization, actual evidence and the cumulative budget.
The journal persists repair review results; subsequent review-origin tasks
stop as `repair_paused` without a bounded grant recording existing goal authorization.
This machine checkpoint does not require the user to approve each repair again.

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
A required review that is unavailable or skipped cannot settle the package.
Keep its pending closeout boundary across restart even if deterministic checks
pass. `package_settled` requires all required review and acceptance evidence;
DONE and status labels are insufficient. New `run_start` and `task_done` events
record `settlement_version: 1`; these tasks close only with `package_settled`.
For unversioned legacy tasks, successful `stage_acceptance` closes its named
tasks and successful `full_acceptance` closes the legacy pending boundary,
including implicit legacy review failures before that PASS. Explicit legacy
`review_required` obligations still require successful review; version-1 work
still requires `package_settled`. Failed acceptance never closes work. Historical
successful closures remain closed for both pending closeout and review requirement
checks, including in mixed old/new journals. Logs
are read without rewriting history. New skipped required reviews stay pending
across passing checks, disabled review configuration and restart. Restore the
authorized independent reviewer without silently dropping its requirement.

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

Normal execution dispatches useful work directly without inference probes.
`flow usage` remains an explicit opt-in all-candidate diagnostic, never routine
preflight or a capacity sweep. If probes are explicitly enabled, they are lazy,
count against model-call limits and follow admission/packet checks. Budget
exhaustion is not a provider outage and cannot close pending review work.

`flow metrics --last 5 [--json]` and the report's Efficiency section expose the
existing journal's timings: startup, probes, CLI requests, checks, lock waits
and scope stops. Worker time includes task checks; the durations overlap.
Legacy missing measures and unfinished calls are unknown. Commands outside the
gate are not measured, and CLI request counts are not provider token/billing
records. Provider usage is additive journal evidence: old journals remain
readable and missing usage stays unknown. Inspect coverage, cache semantics and
per-role totals; incomplete totals are unknown. Compare similar completed batches,
actual provider usage and defect outcomes before claiming token savings. No token
conversion from prompt bytes or cost claims from subscription call counts.


## Scope request recovery

A genuine worker scope request stops the runner immediately, including on a dirty
tree with retry budget left. It returns control to the host without a duplicate
worker call, task completion or settlement. A worker declaration never substitutes
for the task verdict, required review, irreversible approval or settlement gates.
Only actual terminal assistant output should express that decision. Structured
provider envelopes let the parser isolate assistant text from tool/command results;
quoted examples are filtered. For plain providers without structured envelopes,
the parser cannot distinguish arbitrary raw tool output from assistant text, so
unquoted tool output does not have universal isolation. Inspect that evidence
limitation during recovery. Legacy provider/prose support continues through the
existing result parser.

While the runner is stopped, inspect the one saved worker result and its journal /
report evidence: requested repository-relative paths, reason, task and partial edits.
For malformed or conflicting result markers, resolve the recorded protocol error
before dispatch rather than guessing success or retrying the same packet. Survey
only the requested files, their callers and relevant checks. Reconcile necessary
file/scope changes under the existing goal authority, preserving partial work,
failed receipts and cumulative time/call budgets. Do not clean away the evidence,
reset budgets, weaken acceptance or change a running configuration.

Refresh the existing queue/spec declarations and bounded task packet; preview
admission with `flow admit --plan` and pass admission before resuming the stopped
task. A scope stop can leave an IN_PROGRESS row and dirty edits: explicitly
reconcile that task's state and ownership for resumption, without marking it DONE
or discarding its edits.

A `worker_protocol_error` can instead leave a worker-marked or committed DONE row
without a runner `task_done` receipt. While stopped, inspect the actual commit,
its diff and task validation evidence, the saved protocol diagnostic and journal;
confirm task ownership and whether any runner is still active. Preserve commits,
partial work and failed receipts. Reconcile the row's status and repair authority
explicitly before refreshing the packet and re-admitting the unfinished task; a
DONE label alone is not completion. Never manufacture a `task_done` receipt or
settlement, bypass the task verdict, approval or required review, or silently
declare the commit reviewed. Routine in-scope repair uses existing authorization;
unrelated scope or effects still require the appropriate authority. This is host
recovery, not an automatic worker retry.

Ask the user only for genuinely missing authority or a necessary decision outside
the authorized goal, naming paths, reason and preserved partial work. Keep the
runner stopped until that authority exists.

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

## Evidence learning and bounded experiments

`docs/work/<id>/evidence.md` owns candidate lessons, using the existing
[evidence template](../flow/templates/evidence.md). Record a lesson only after a
concrete user correction, reproduced failure with verified fix, or recurring
observation. Keep observed fact apart from generalization; cite durable commits,
files/tests or logs, never transcripts, credentials or runtime artifacts.
No confidence scores, speculative learning database or automatic policy overlay.

Promote through these evidence-backed steps in the same record:

1. **Observed failure**: preserve trigger, supported input, violated invariant,
   source and baseline. Update a repeated lesson rather than duplicating it.
2. **Proposed scoped change**: name the owning rule, affected consumers, intended
   behavior and non-goals. Check existing authority before proposing a new rule.
   Prefer eliminating the invalid state or giving the fact one owner; next use
   deterministic checks/types, then behavior regressions. Use documentation for
   judgments that cannot be enforced mechanically, rather than another reminder
   for a preventable structural failure.
3. **Regression/evaluation**: reproduce the failure and evaluate the scoped fix
   against acceptance and safety invariants. Keep failing as well as passing
   evidence; lack of proof leaves the proposal a candidate. Before promoting a
   guard, prove it fails on the observed bad case and passes the corrected case.
   Link the owning rule and enforcement location in the existing lesson row's
   destination/evaluation cells; do not introduce another registry.
4. **Authorized activation**: identify the user's actual authorization, scope,
   destination and activation conditions. Existing explicit scope authorization
   may cover the change; never ask for a repeated approval merely for bookkeeping.
   Missing permission remains pending. A candidate itself grants none.
5. **Evidence and rollback**: record activated revision, result, remaining limits
   and a concrete rollback target/procedure. Maintain the rule once at its owner;
   the evidence row is its history, not a second active instruction.

Legacy candidate/accepted/rejected/superseded rows remain valid history and need
no migration; missing evaluation or activation evidence remains unknown. New
fields are optional and additive. A candidate is not active policy. Mark accepted
only after scoped evaluation and authorized activation; preserve rejected and
superseded records. A lesson conflicting with a standing user instruction is
flagged for the user and never applied. Scope defaults to this repository; use in
multiple projects supplies evidence, never permission for global propagation.
This package's authorization covers flow workflow improvements only, not other
projects' policies, business execution or a new global installation.

Adopt external ideas through existing seams, not plugin infrastructure. Record a
source and concrete failure it may address; choose one bounded experiment in an
existing work package with a baseline/candidate, scope, stop condition, cumulative
budget and rollback. Reuse the task packet/handoff for bounded implementation
context, existing `model_call_end`/metrics usage for measured provider evidence,
and `verify --task` plus required stage/delivery/review gates for evaluation.
Compare supported behavior, failures and comparable observed usage, marking
missing usage unknown. A promising experiment does not activate itself; retain
ordinary admission, fingerprints, review and irreversible approval boundaries.
No new plugin registry, automatic learned config or speculative inference calls.

For a bounded comparison, freeze representative supported tasks/inputs, baseline
and candidate revisions, and quality/safety floors before running it. Measure the
end-to-end successful result (including required checks), failures and retries,
actual provider usage and cache coverage/semantics, not just prompt size or one
fast intermediate step. Distinguish offline fixtures/replays from live model
behavior; fixtures prove only the exercised deterministic contract, not live
quality or savings. Missing usage or cache data remains unknown.

Keep a change only after meaningful comparable evidence meets the frozen floors;
otherwise revert it or mark the experiment inconclusive with the remaining risk.
Only when subjective output comparison needs judgment, blind the candidate labels
and use one scoped judge under existing authority and budget. No routine multi-model
fanout, minimum iteration count or automatic evaluation model calls. Record the
comparison in the existing evidence template; it does not bypass activation gates.
