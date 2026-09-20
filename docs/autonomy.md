# flow-run autonomy: probe, one review and one full acceptance per package, bounded spend

> Design contract for the unattended half of `gate/gate.py` and the
> `flow-run` / `run-queue` skills. Config keys below are the single source of
> truth; `gate/README.md` documents mechanics, `docs/usage.md` the human view.
> `$flow` settles the requirement interactively; `$flow-run` runs it unattended.

## 0. Goal

One `$flow-run` ends in one of two states, without a human in between:

1. every admitted task closed, reviewed once as a package, and the full
   oracle green; or
2. a short list of things that genuinely need the user: a review with a high
   finding, a failing full oracle, a scope request, or an irreversible task
   waiting for approval.

Everything else (worker choice, quota outages, a worker that stopped halfway)
is handled by the runner. The runner's own machinery must cost less than the
work it supervises: no evidence caches, no receipts, no environment hashing,
no revision loops.

## 1. Roles

| role | who | model |
|---|---|---|
| host | the interactive CLI running `$flow-run`: inspects, decides, writes the queue | whatever the user is talking to |
| worker | one headless process per task attempt | `workers` list and row pins; `worker_cmds` / `worker_models` in `.gate/config.json` |
| reviewer | one read-only headless process per run (plus checkpoints) | `judge.chain`, tried in order, next member on outage or unparsable output |

Model names in the shipped defaults are the authors' choices, not a contract.
The `pi` worker runs any OpenRouter model (`worker_models.pi`); pi reads
`OPENROUTER_API_KEY` from the environment.

## 2. Probe before spend

At run start every candidate CLI (run-wide list, row pins, review chain) gets
one tiny real request. A quota-shaped answer benches it for `quota_cooldown_s`;
a probe that never reached the provider benches it for `probe.retry_s` only.
State lives in `.gate/tool-status.json`; `gate.py usage` prints it on demand.
Probes count against `max_model_calls`.

## 3. Dispatch

- The worker prompt is `worker_prompt` plus a **task packet**: the queue row,
  declared files, the local check command, the acceptance section of the work
  package spec, and `git diff --stat`. A packet over `worker_packet_max_bytes`
  is refused (`prompt_too_large:<id>`), never trimmed silently.
- A worker that exits with uncommitted changes and no DONE gets one retry on
  the same tree with a one-line hint. A second attempt that still does not
  close the task stops the run (`worker_left_changes:<id>`).
- A worker that stops to ask for a scope decision stops the run
  (`scope_request:<id>`) instead of being asked the same question again.
- Two identical failures stop the task (`no_progress`). Tool outages bench
  the CLI and restore the row without spending an attempt.

## 4. Verification

- `gate.py verify --task ID` runs the task's admitted local command
  (`<!-- task:ID verify: {"cmd": ..., "timeout_s": N} -->`) and records a
  task-scoped verdict bound to the bytes of the declared files. The commit
  hook accepts the DONE flip of that task while those bytes are unchanged.
- `gate.py verify --queue` runs the full `verify_cmd`. The runner does this
  once per package, after the review (section 5), journals `full_acceptance`,
  and stops with `full_acceptance_failed:<ids>` when it fails. Nothing is
  rolled back.
- No verdict cache. Re-running a local check is the cheap path.

## 5. The review is a gate, once per package

Three rules. None has a threshold to tune, so none is fitted to a project.

**1. A review blocks or it does not. It never creates work.** With
`judge.enabled`, the review chain reads the commits once with `judge_prompt`
(tasks, commit range, declared files, verify tail).

- Pass: verdict `pass` and no `high` finding. The score is recorded, never
  gated on: a single-model score moves by several points between rounds, and a
  threshold on it produced a five-round revise loop on a real project.
- Every finding carries `action` (`required`, `optional`, `none`; a missing
  value reads as `required`). Its definition is appended after the project's
  `judge_prompt`, so an override cannot drop it. It is information for the
  people planning the next work; the runner gates on nothing but `high`.
- Findings below high are recorded in full and never become queue rows: the
  `review_findings` journal event, the report's Review section, and
  `REVIEW-NOTES.md` next to the queue file, committed (`.gate/` is gitignored
  in some projects; the queue's directory is the one tracked place the gate
  already commits to). What is worth doing from that record enters the queue
  the way all work does: planned through `$flow`, with an acceptance of its
  own. Acceptance is a command and its exit code (design.md section 2); a
  reviewing model is not an acceptance authority, and any new diff gives it
  something to say, so a review that feeds the queue has no last round.
- Chain down or unparsable: `review_skipped`, the full acceptance still runs,
  the report says so. Visibility replaces a silent quality-off.

**2. The boundary is the package, not the run.** The review and the full
oracle run when the queue has no eligible TODO left, and cover every task
closed since the last full acceptance, including tasks of earlier runs
(`task_done` events in the journal; a `full_acceptance` event or a failed
review closes the boundary). A run that ends with TODO rows left reports the
closed tasks as deferred and costs nothing more. A run that finds nothing to
dispatch but tasks still waiting closes the package. Rows in
`judge.checkpoints` are still reviewed alone right after they close, and
`gate.py review --tasks <ids>` reviews on demand.

**3. A failed review gets one repair round.** `review_failed:<ids>` stops the
run with the findings; the host admits one repair row for all of them, tagged
`<!-- task:ID origin: review -->`. When that row draws another high finding on
a file it declared itself, the stop is `review_loop:<file>`: a second repair
row is not admissible, the answer is a spec revision or the user's decision
to accept the risk. The runner never revises on its own. The gate cannot see
who wrote a row, so tagging the repair row is the host's duty under the
`flow-run` skill; everything else in this section is mechanical.

Why, measured on the three live projects between 2026-09-18 and 2026-09-20,
when findings below high became TODO rows and the boundary was the run:

| measurement | value |
|---|---|
| reviews that covered exactly one task (alltom) | 27 of 28 |
| full oracle time against worker time (AugurNext) | 8.1 h against 5.8 h |
| rows made from findings, and closed (both projects) | 165 made, 36 closed |
| share of worker time spent on those rows (alltom) | 5.5 h of 16.6 h |
| one block of string parsing (alltom, 2026-09-19) | 4 rounds, 5 full oracles, about 2 h, a few lines of wording |

## 6. Spend

Two numbers: `max_model_calls` (workers, reviewers and probes of one run)
and `run_timeout_s`. Reaching either stops the run with `budget:model_calls`
or `run_timeout`. No persistent ledger; a new run is a new budget by design,
and the host decides whether to start one.

## 7. Reversibility tiers

| tier | examples | runner behaviour |
|---|---|---|
| read-only | tests, git diff, code queries | free |
| reversible | src edits, local commits | free; every commit is in the journal |
| external | `git push`, broadcasts | worker prompt forbids; the host does it after the run when allowed |
| irreversible | `irreversible_globs`: migrations, `data/**`, hooks, settings | needs `<!-- task:ID approved: <who/date> -->`; `run` stops with `needs_approval:<id>` even in advisory mode |

## 8. Config keys (defaults)

```json
"probe": { "enabled": true, "timeout_s": 90, "retry_s": 600 },
"judge": { "enabled": false, "chain": ["fable", "opus", "codex"], "checkpoints": [], "timeout_s": 1200 },
"judge_cmds": { "fable": [...], "opus": [...], "codex": [...] },
"judge_prompt": "<template; {tasks} {base} {commits} {files} {verify_tail}>",
"max_model_calls": 40,
"worker_packet_max_bytes": 6000,
"worker_models": { "pi": "" },
"irreversible_globs": ["drizzle/**", "data/**", ".git/hooks/**", ".claude/settings*", "scripts/apply-*"]
```

## 9. Journal events and stop reasons

Events: `probe`, `attempt_start`, `heartbeat`, `task_done`, `scope_drift`, `worker_left_changes`,
`scope_request`, `prompt_too_large`, `review_start`, `review_verdict`,
`review_skipped`, `review_findings`, `full_acceptance`, `needs_approval`,
`task_end`, `tool_disabled`, `run_end`.

Stop reasons that need a human: `review_failed:<ids>`, `review_loop:<file>`,
`full_acceptance_failed:<ids>`,
`scope_request:<id>`, `prompt_too_large:<id>`, `needs_approval:<id>`.

## 10. What the user reads

`RUN-REPORT.md`: the task table, a **Review** section, a **Full acceptance**
line (or the note that both are deferred to the package boundary), and
**Waiting on you**, which is empty on a clean run and is the only
section the user has to read.
