# flow-run autonomy — judge chain, usage-aware dispatch, bounded iteration, reversibility tiers

> Design contract for the 2026-09-03 upgrade of `gate/gate.py` + the `flow-run` / `run-queue`
> skills (`skills/flow-run`, `skills/run-queue`). Config keys below are the SSOT; `gate/README.md`
> documents mechanics, `docs/usage.md` the human view. Design decision (2026-09-03): `$flow`
> settles the requirement interactively; `$flow-run` runs it unattended. This document only
> covers the unattended half.

## 0. Goal

One `$flow-run` should end in one of two states, without a human in the loop in between:

1. every admitted task closed **and judged acceptable**, or
2. a short list of things that genuinely need the user: an escalated task with the judge's
   findings, or an irreversible task waiting for approval.

Everything else — worker choice, quota outages, one or two quality iterations — is handled by
the runner.

## 1. Roles and model selection

| role | who | model selection |
|---|---|---|
| host | interactive CLI | its current model |
| judge | headless read-only review | configured chain: fable, opus, codex |
| worker | headless task process | CLI configuration unless explicitly pinned in worker_cmds |

Claude workers omit `--model` and inherit effective CLI settings and environment at launch
in the project directory. This does not copy a transient model selection from another
interactive session. The runner neither injects Opus nor forbids a model family.
Explicit project model arguments remain authoritative. Roles are separated by permissions
and review lifecycle. The judge commands and their explicit model pins remain unchanged.

## 2. Usage-aware dispatch (probe before spend)

Today benching is reactive: a worker is benched only after an attempt died on a quota error, and
that costs a dispatch plus a queue-row restore. New: **probe once per run, before the first
dispatch**, every candidate worker (run-wide list ∪ row pins ∪ judge chain CLIs).

- Probe = one tiny real request (`Reply with the single word OK`) per CLI, `probe.timeout_s` (90).
- Output matched against `QUOTA_PATTERNS` → bench for `quota_cooldown_s` (same as reactive).
- Timeout / not installed / other non-zero exit → bench for `probe.retry_s` (600) only. A probe
  that never reached the provider must not occupy the quota window.
- Benched-by-probe state lives in `.gate/tool-status.json` with `reason: probe:<why>`; the run
  report lists it. `gate.py usage --repo` prints the same table on demand.
- Never probe per task. A worker whose cooldown expires mid-run is re-probed once before it is
  dispatched again.

Worker **choice by task kind** stays with the host (it writes the `worker` column). Rule table
in the `flow-run` skill; the runner only honours pins and falls through.

## 3. Judge after every task (not at the end)

Why per task: later tasks build on earlier ones; a defect found at run end means the whole chain
is suspect. Evidence for bounded iteration: first correction gives the bulk of the gain
(~62 → 70 %), the third and later attempts add < 2 % each and mostly recycle the previous diff
(self-correcting agent studies, 2026; Socratic-SWE plateaus by iteration 4–5).

Sequence after `task_done`:

1. `judge_start` — spawn the chain with `judge_prompt` (task id, commit, declared files, verify
   tail). The judge reads the owning spec's acceptance, the diff, the tests, and returns strict
   JSON `{score 0–100, verdict pass|revise|escalate, findings[], revision_brief}`.
   Rubric in the prompt: acceptance met 40 · tests real + mutation-checked 20 · scope discipline
   15 · conventions/quality 15 · regression risk 10.
2. `judge_verdict` journaled with score + verdict + chain member used.
3. `pass` or `score ≥ judge.pass_score` (85) → next task. **The judge never touches the queue
   row; DONE was earned by the exit-code gate and stays.**
4. `revise` → a **revision attempt** if all hold:
   - revisions so far `< judge.max_revisions` (2 → at most 3 rounds total: initial + 2),
   - not the second revision with score gain `< judge.min_gain` (5) — no-progress early stop,
   - a usable worker exists; one **other than** the last committer is preferred (rotation;
     the same model repeats its own blind spot). When the last committer is the only usable
     worker it is reused, and the journal (`revision_start.rotated: false`) and the report
     (`(not rotated)`) say so. No usable worker at all → escalate `no_workers_for_revision`.
     Projects should therefore list at least two CLIs in `workers`.
   Revision prompt: fix exactly these findings, code only, never edit the queue row, run the
   acceptance, commit. After it returns the runner re-runs the acceptance itself
   (`cmd_verify` internals): HEAD moved + FAIL → stop `revision_broke_verify:<id>` (no auto
   reset); HEAD unchanged → failed revision, counts against the cap; then re-judge.
   Same `non_gate_worktree_state` / `queue_status_changes` guards as a normal attempt.
5. `escalate`, cap reached, or no-progress → stop the run `judge_escalated:<id>`; the report
   carries score history and the last findings. The task stays DONE.
6. Judge chain entirely down or every member returns unparsable JSON → `judge_skipped`, the
   run continues, the report says so in its own section. A judge outage is not a task failure,
   and must not silently switch quality off — visibility is the substitute.

Cost caps: claude judge/worker entries carry `--max-budget-usd`; codex has no equivalent, so
`judge.timeout_s` (1200) is its cap. Total per task ≤ 3 rounds.

## 4. Reversibility tiers

| tier | examples | runner behaviour |
|---|---|---|
| read-only | tsc, vitest, git diff, code-graph queries | free |
| reversible | src edits, local commits, test.db | free; every commit is logged in the journal |
| external | `git push`, broadcasts | worker prompt forbids; the **host** does it after the run if CONTEXT/user allowed, with `rev-list 0/0` re-verification |
| **irreversible** | `irreversible_globs`: migrations, `data/**`, hooks, `.claude/settings*`, `--force` | needs `<!-- task:ID approved: <date or who> -->`; `admit` reports `needs-approval`; `run` stops with `needs_approval:<id>` **even in advisory mode** |

Approval is per task id and single-use by construction. The host places such tasks at the queue
tail so the run finishes everything else first and the user approves once, not mid-run.

## 5. Config keys (defaults)

```json
"probe": { "enabled": true, "timeout_s": 90, "retry_s": 600 },
"judge": { "enabled": false, "chain": ["fable", "opus", "codex"], "pass_score": 85,
           "max_revisions": 2, "min_gain": 5, "timeout_s": 1200 },
"judge_cmds": { "fable": [...], "opus": [...], "codex": [...] },
"judge_prompt": "<generic template; {task} {commit} {files} {verify_tail}>",
"revision_prompt": "<generic template; {task} {findings} {brief}>",
"irreversible_globs": ["drizzle/**", "data/**", ".git/hooks/**", ".claude/settings*", "scripts/apply-*"]
```

`judge.enabled` defaults to **false** so existing projects are unchanged until they opt in;
projects opt in per `.gate/config.json`. `workers` / `max_task_files` / admission thresholds
are untouched.

## 6. Journal events and stop reasons added

Events: `probe`, `judge_start`, `judge_verdict`, `judge_skipped`, `revision_start`,
`revision_end`, `judge_escalated`, `needs_approval`.
Stop reasons: `judge_escalated:<id>`, `revision_broke_verify:<id>`, `needs_approval:<id>`.
Both skills' watcher regex must list the new events, or the narrator goes silent on exactly the
new behaviour.

## 7. What the user sees at the end

`RUN-REPORT.md` gains a **Judge** section (per task: rounds, scores, final verdict, worker per
round) and a **Waiting on you** section that is empty on a clean run. That last section is the
only thing the user has to read.
