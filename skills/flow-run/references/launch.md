# Configure a stopped run and launch

## 4. Configure and launch

`.gate/config.json`: only an **owner strict pin** (explicit one-off exact worker/model request or policy single-worker rule) constrains `workers` to that single exact worker with no automatic fallback and reconciles conflicting queued row pins while preserving other rows without broadening (reconcile only after stopping). An ordinary queued row worker pin is not a strict owner demand; `gate/gate.py` preserves its fall-through to the run-wide `workers` list. For a **tolerant preference**, explicit durable preference, or `route.py recommend` selection, order `workers` with the primary exact pin first and verified policy-eligible backup(s) (preserving conditionally eligible fallbacks) after, setting `worker_cmds` or `worker_models` to enforce exact model and effort flags,
`judge.enabled` and `judge.chain` when the project wants a package review,
`judge.checkpoints` for rows that must be reviewed on their own, `max_model_calls`
for the run. Keep required reviews independent of the implementation worker:
order `judge.chain` / `judge_cmds` so a distinct model/vendor reviews closed
tasks rather than the implementer reviewing its own diff (disclose as unreviewed
if no independent reviewer is usable). Resolve model and effort from the owner policy for the actual role. Reconcile
or replenish delivery/batch limits only under the
[budget continuation decision](../../../docs/autonomy.md#budget-continuation-decision)
(task-sized finite increment when authorized for a host-created batch; actual
owner grant for any owner-set cap); never silently raise caps or weaken
admission to get a task through.

With mini policy installed, an implicit review chain resolves to exact read-only
Carbon/Astra adapters. Explicit compatible `judge_cmds` stay in force; historical
explicit chains/templates must satisfy current role pins and read-only modes at
admission and review. A display alias alone cannot disqualify an otherwise healthy
authorized contract. Do not copy the old unpinned Fable/Opus/Codex templates into a
new package. Gate rechecks availability before review and independence against the
worker that actually closed each task after fallback. If no independent route is
eligible, retain DONE work as unreviewed and preserve the acceptance gate and budget.

For any custom worker, reuse its verified executable and existing TLS wrapper.
The gate resolves the worker key on PATH, replaces `template[0]` with that path,
and uses `{projdir}` as cwd; stdin is piped only for `codex`. Inspect local CLI
help to verify model/effort and prompt flags; do not invent directory flags or
replace a TLS wrapper with a raw binary. Set the interpreter environment for
the detached engine and checks. Preserve failed attempts and reconcile partial
edits and queue ownership before any fallback; no competing IN_PROGRESS writer.

A new package starts from a clean slate. Before its first launch, re-read every
package-scoped key the previous package left behind: `worker_prompt`,
`revision_prompt`, `judge_prompt`, `judge_cmds`, `delivery.checks`,
`delivery.stage`, `repair_continuation`, `recovery_inputs`, `max_model_calls`.
Rewrite each for the new package or reset it; disable an old repair grant. A
review prompt that still names the old tasks reviews nothing, and old checks
never cover the new files. `admit` does not detect this; the host must.

Count authorized remaining TODO rows for `--MaxTasks`; reviews and stage checks
run at queue exhaustion. Use one launch per admitted batch. Normal execution
makes no inference probes; `flow usage` is an explicit opt-in diagnostic only.
Then follow
`run-queue`: detached launch, one incremental event watcher, report meaningful changes,
read the final report. Keep a byte cursor; do not repeatedly scan full journals or
worker transcripts. Prefer event notifications; otherwise use idle backoff within
host responsiveness limits. Read worker log excerpts only for an error, a suspected
stall or a user status question. Empty polls are not progress to narrate.
Never mark DONE, never `--no-verify`, never take over IN_PROGRESS, never push without current explicit authorization.

For new authorized batches with a known exact origin session, select the real
`flow supervise launch` adapter by default, as described in
[run-queue launch](../../run-queue/references/launch.md). Bind immutable admitted
task IDs, package spec and finite cumulative call/deadline limits. For native Codex delivery,
use `--transport codex` with a verified local Codex UUID; no IPC receiver is
needed. Run `launch --watch` under the existing detached owner. Explicitly opt
out if the session metadata or durable host owner is unavailable, and disclose
that limitation. Preserve pins, fallback policy and acceptance gates. Pending
is not delivered; ambiguous native turns require exact-thread reconciliation.

Bind Codex delivery only from the explicitly invoked originating session with
its exact trusted `CODEX_THREAD_ID`; the repository may be nested under that
session's persisted working directory. Never use another thread or `--last`.
After a user stop, resume explicitly from the same bound session (the default
is the current trusted host ID; `--session <same-ID>` is supported). Terminal
watchers exit even if the final message remains pending on disk.


For systemd `--transport codex`, explicitly pass the initiating session with
`--setenv="CODEX_THREAD_ID=$CODEX_THREAD_ID"`; a transient user service does not
inherit that environment automatically. Review consultation runs inside the
ordinary guarded gate path; do not start a separate adviser/polling service.
