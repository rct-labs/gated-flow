# Recovering a stopped claim

Recovery returns one proven stopped `IN_PROGRESS` claim to `TODO`. It never
closes a task, grants a general implementation retry, weakens verification, or
substitutes for independent review. Original implementation, claims, journal,
and previous recovery receipts remain intact.

## Ownership and locking

`run`, `recover-task`, and `review-task` share the stable OS advisory engine
lock. Run acquires it before preflight writes, execution freezing, run_start,
or any other state mutation, and releases it after final bookkeeping or on
exception. A rejected contender leaves no phantom run.

A closed run must have dispatched this task and ended `not_done`. Later task
dispatches, open runs, live owners and uncertain process queries refuse recovery.
Recorded runner PIDs are checked against current process census and an absent
process query. For legacy runs, `--stopped-pid` must match the supervisor session
in `.gate/supervisor.log`: repository identity, supervisor start, the named GATE
run, runner exit, and corresponding supervisor exit are required. The receipt
pins the original session bytes and offset; later log appends are permitted,
but edits to the session invalidate it. An unrelated recently-dead PID or an
operator's assertion is insufficient. No process is terminated by these checks.

The implementation must be an ordinary ancestor commit with actual declared
code changes. Declared sources must remain clean and identical. Task row
substance and the complete task acceptance heading subtree, including deeper
headings, must match the implementation commit, HEAD, index and worktree.
Unrelated task sections can evolve before verification; bookkeeping cells do
not redefine the task's acceptance requirements.

## Acceptance dependency contract

The engine captures all tracked and nonignored untracked input files, including
index blob identity, file bytes and filesystem change metadata. Git filenames
use NUL-delimited byte parsing, independent of `core.quotepath`. Symlink/external
inputs are unsupported and refused. The oracle is invalidated before monitor
startup, initial capture or any other fallible preparation; failures cannot
leave an older PASS usable.

Git ignore rules are not a declaration of irrelevance. Ignored inputs are
classified by the optional `recovery_inputs` contract in the existing engine
configuration, at the granularity a project can actually answer for: an ignored
**directory is one entry** (`node_modules/`), listed the way
`git ls-files --others --ignored --directory` lists it, so a dependency tree of
two million files is one decision rather than two million refusals and a
twenty-second scan on every acceptance run. An ordinary task's verify still
runs with unclassified entries present: capture records them, never reads them,
and a recovered completion is refused while any remain. `.gate/config.json` is
always an input. The engine's exact journal, verdict, lock, report, run and
recovery artifact paths are excluded explicitly; there is no arbitrary `.gate`
substring exclusion.

Example contract for a synthetic project:

```json
{
  "recovery_inputs": {
    "include": ["runtime/settings.json", "vendor/"],
    "exclude": ["node_modules/", "build-cache/", "private-data/"]
  }
}
```

`include` lists exact repository-relative ignored files, or directory prefixes
ending in `/` whose every file is then hashed like a tracked input. `exclude`
lists exact ignored paths or directory prefixes. Wildcards, absolute paths,
parent traversal and exclusion of tracked/nonignored inputs are refused. This
contract and acceptance globs are bound into receipt configuration. Changing it
invalidates existing receipts.

Exclusion is a supported **oracle isolation contract**, not a claim that every
ignored file is irrelevant. Excluded paths must be non-input outputs or external
dependencies held immutable by the operator's isolated verification environment.
For example node_modules must come from the pinned lockfile/environment and
remain immutable during verification. Production data must be excluded from the
verification environment; use synthetic fixtures. If an oracle reads mutable
excluded data, externally fetched content, or undeclared environment dependencies,
this protocol cannot prove its input identity and recovery must not be used.
The engine does not inspect production data or traverse/hash dependency caches
to discover their relevance. Configure the contract only after establishing that
isolation; recovery never adds exclusions or changes verification automatically.

Before and after snapshots are supplemented on Windows by recursive kernel
filesystem change notifications armed before the first capture. Write-and-restore,
including restored mtime, invalidates acceptance. Notification overflow, monitor
startup failure or I/O error fails closed. POSIX uses inode/ctime metadata, which
ordinary processes cannot restore; privileged filesystem/timestamp tampering is
outside this local evidence trust boundary. A successful verdict must still match
all input identities at completion; only the queue path can change for closure.
Requirements and declared source checks still apply to that queue change.

## Durable recovery transaction

An immutable receipt is written first. A flushed transaction intent, with exact
before/after queue bytes, exists **before** the atomic queue replacement. Index
and journal append follow; a final committed marker removes the transaction
barrier. Any interrupted transaction blocks all runner dispatches, even if the
queue already says TODO. It never becomes a normal task with a fresh budget.

Repeat the original recovery command to finish a pending transaction. The engine
rechecks receipt evidence, stopped ownership, arguments and exact queue bytes.
Unrelated intervening queue edits retain the barrier and require diagnosis; they
are never overwritten. Each resumed step is idempotent. Original intents remain
in `.gate/recovery/transactions.ndjson` as history.

## Independent review and budgets

All recovered closures must satisfy the recovery evidence checks, including
mixed code/document commits. The hook records exact parent, queue blob, receipt
and acceptance evidence and establishes review responsibility. The runner also
raises responsibility before invoking a recovered worker. DONE plus a provider
error retains it. Recovery history plus an unreviewed DONE row reconstructs an
obligation even when closure happened outside the runner or without the hook.

Only an affirmative independent judge result bound to receipt and reviewed HEAD
can clear review responsibility. Clearing also binds the exact closure and
completion record. There is no `review-task --accept` or named-human override.
Missing barriers never imply successful review. Audit requires the affirmative
record for the exact receipt and closure; otherwise it reports REVIEW PENDING
or an unproven/fabricated completion.

The one revalidation invocation is accounted by task and original implementation
lineage across all receipts. Outage rotation cannot invoke another worker after
that allowance is spent, and the dispatch goes to the preferred (pinned) worker:
the attempts a recovered task carries were not that CLI's failures, so they do
not rotate it away. Receipt renewal is rejected once used. Original attempt
counts, outcomes and failure signatures remain visible.

One exception exists, and it is explicit: `renew-revalidation --task <task>
--reason "<why>"` grants a second dispatch for a lineage whose only dispatch
ran the canonical oracle to a recorded PASS and was then refused at the gate
— the shape an engine defect produces, not a worker failure. It is refused
when the oracle did not pass, when the task closed, when the run is still open,
or when the lineage was already renewed; the reason is journalled with the
grant. The stopped claim that dispatch left behind is then recovered from its
own run as usual. Independent review
retains revision invocation counts, prior scores, no-progress and cap exhaustion
across commands; repeating review cannot renew implementation allowances.

## Commands

These are examples only; implementation tests run synthetic repositories.

```sh
python gate/gate.py recover-task --repo <project> --dry-run \
  --task <task> --implementation <commit> --run <run> --reason not_done:<task> \
  --stopped-pid <recorded-legacy-supervisor-pid>
python gate/gate.py recover-task --repo <project> \
  --task <task> --implementation <commit> --run <run> --reason not_done:<task> \
  --stopped-pid <recorded-legacy-supervisor-pid>
python gate/gate.py review-task --repo <project> --task <task>
```

Omit `--stopped-pid` when the stopped run recorded its owner. `flow recover-task`
and `flow review-task` route to the same engine. No command commits the restored
row automatically. The host reviews the evidence and controls actual recovery.

## Trust and limits

Receipts and journals are local evidence, not signed attestations. An actor able
to forge all local history can fabricate a consistent chain. A dead owner cannot
be proven by missing lock files alone. Process census cannot identify a worker
whose command line and ancestry carry no repository/task identity; uncertain
ownership must be resolved with supervisor records, not guessed. Verification
requires the explicit dependency isolation contract above. No real production
recovery or provider review is established by synthetic regression results.
