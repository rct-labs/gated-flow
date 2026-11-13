# Configure a stopped run and launch

## 4. Configure and launch

`.gate/config.json`: `workers` (selected worker first, verified fallbacks after, unless project policy requires exactly one active
worker; then let the host select a fallback only after reconciling the stopped run),
`worker_cmds` or `worker_models` when a CLI needs pinned model/effort flags,
`judge.enabled` and `judge.chain` when the project wants a package review,
`judge.checkpoints` for rows that must be reviewed on their own, `max_model_calls`
for the run. Keep required reviews independent of the implementation worker:
order `judge.chain` / `judge_cmds` so a distinct model/vendor reviews closed
tasks rather than the implementer reviewing its own diff (disclose as unreviewed
if no independent reviewer is usable). Resolve model and effort from the owner policy for the actual role. Never raise limits or weaken admission to get a task through.

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
