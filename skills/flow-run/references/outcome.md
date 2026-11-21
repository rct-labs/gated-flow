# Interpret results and preserve continuity

## 5. After the run

Read `RUN-REPORT.md` bottom-up: **Waiting on you** first, then **Review**,
then **Stage acceptance** / **Full acceptance** and **Defect checkpoints**,
then the task table. Relay those in the user's
language, leading with the measured result or the blocking fact.

- `review_failed:<ids>`: consolidate evidenced defects within the authorized
  goal; preserve BUG ids and add a failing regression. Verify the changed flow
  and direct regressions only. Reuse existing authorization under the continuation
  [shared repair contract](../../../docs/autonomy.md#repair-continuation); an ordinary failed repair is not a new approval gate.
- `review_loop:<location-or-task>`: stop repeating the failed approach. Inspect
  the recorded cause once; continue only with a different evidenced fix within
  scope and cumulative limits. Repeated no-progress failure is a technical blocker.
- `stage_acceptance_failed:<ids>` / `full_acceptance_failed:<ids>`: read saved
  check logs, fix the demonstrated cause in scope and rerun affected checks.
  Do not restart broad review or repeat an unchanged failure.
- `scope_request:<id>`: keep the runner stopped and follow
  [scope request recovery](../../../docs/autonomy.md#scope-request-recovery).
  Inspect the saved result once, preserve edits/budgets, reconcile necessary
  scope under existing authority and refresh admission before resuming. Ask only
  for genuinely missing authority; do not dispatch a duplicate worker.
- `worker_protocol_error:<id>`: keep the runner stopped and follow
  [scope request recovery](../../../docs/autonomy.md#scope-request-recovery).
  Inspect the saved protocol diagnostic and actual task/commit state before
  reconciling authority and re-admitting. A worker's DONE row or commit without
  a `task_done` receipt is not runner completion, review or settlement; never
  manufacture those receipts or retry the conflicting packet automatically.
- `prompt_too_large:<id>`: remove unrelated context or split the task; never truncate or weaken acceptance/stop conditions.
- `needs_approval:<id>`: show the scope line, ask once, add the line on yes.
- `budget:model_calls` / `run_timeout`: reconcile per-run versus cumulative limits
  using the [budget continuation decision](../../../docs/autonomy.md#budget-continuation-decision)
  (distinguish owner caps from host-created batches, size any authorized
  increment to the actual next slice + bounded repair/review, and never infer an
  extension from a new session or restored provider quota).

A stage is finished when its selected checks and required reviews pass.
Disclose untested scope and unavailable reviews. A final delivery needs
`delivery` acceptance; when no new tasks remain, run `flow verify --stage
delivery --repo .` instead of inventing a task or claiming an old local PASS.

Use [review and defect records](../../../docs/autonomy.md#3-review-and-defect-records)
and [repair continuation](../../../docs/autonomy.md#repair-continuation).
DONE and passing tests cannot settle a required review that is unavailable.

Before `flow checkpoint` (and on resume for any settled task not yet recorded), actively record eligible task outcomes once per distinct `run`/`task` via `python3 "$(flow home)/skills/flow-run/scripts/route.py" record` (consult `python3 "$(flow home)/skills/flow-run/scripts/route.py" record --help` for exact flag shapes). Pass the actual run ID, a durable package-qualified task ID for `--task-id` (e.g. `billing-v2/T01`; reuse that same logical ID across retries and resumed runs, and qualify separately for independently scoped packages that reuse `T01`, because the helper deduplicates reports per run/task and counts distinct logical task IDs to prevent unrelated work from collapsing or repeated retries from masquerading as independent evidence), class (`routine`|`complex`), exact primary model+effort, and outcome: distinguish a **first-pass accept** from an **attributable implementation miss**, and when a backup worker executed pass the optional exact actual backup model+effort as **backup rescue** or **backup failure** (backup rescue is never recorded as first-pass accept). Record only when both runner verdict and execution trace confirm the exact model+effort identity and implementation attribution; ignore provider outages, quota/rate-limit stops, infrastructure or protocol errors, and unknown model identities, and never treat generic review commentary without an evidenced defect verdict as a quality miss. All ranking remains subordinate to installed owner policy: skip `route.py record` when learning is inapplicable (explicit one-off/forced exact pins or off-table models) so overrides do not contaminate default order, and never label empty state as good.

Rebuild `CONTEXT.md` and save it with `flow checkpoint`: a stopped or
interrupted task is written as such, with its failing command, the
uncommitted paths and the log path, never as done; read the checkpoint's
warnings. If the run brought a user correction or a verified failure and fix,
record one candidate lesson in the package's `evidence.md` ([evidence learning](../../../docs/autonomy.md#evidence-learning-and-bounded-experiments)) and the decision in the spec; otherwise none. When a learning-backed or skill experiment completes (accepted, rejected, or inconclusive), record the project-owned experiment/review outcome and any natural-language matching preference via `flow learn feedback` without relying on model opinion alone. Checkpoint again at
package closure. If the user asked to finish everything and TODO rows
remain for a reason other than a genuine blocker, say so and launch again.

For supervised batches, inspect `flow supervise status --repo ...` once on
reconnect (also exposed by `flow resume`). Relay pending delivery explicitly.
`run_end` is distinct from acceptance of every bound task; report settlement and
its actual stage from gate evidence. `needs_host` is a durable hold: reconcile
partial edits, side effects, ownership and cumulative limits before any successor
launch; do not clear state to replay. `stop` persists across watcher restarts and
blocks subsequent dispatch, including reviewers. Existing outages remain
availability evidence, never implementation-quality misses.

For native notifications, report `delivery_evidence` literally: exact-session turn
completion is distinct from pending, busy, unsupported or uncertain delivery.
A clean `no_workers` run can remain `waiting_routes` under the original envelope;
a host hold still requires explicit reconciliation. Never reset cumulative limits
or replay an uncertain status turn to make delivery appear successful.


Technical escalation or a mapped repair retaining all blockers now arranges one
blind senior consultation round in the gate. Read the reported episode in
`.gate/consult/state.json` once, check both replies against primary evidence and
record one repair/test plus dissent using `flow consult-settle`. Pending seats,
outages and unresolved advice remain holds; do not repeat an unchanged review or
reset reservations. Advice cannot settle acceptance. Follow
[host settlement](../../../docs/autonomy.md#senior-consultation-after-technical-rejection)
and hand the repair to the coding pool before fresh independent review.
