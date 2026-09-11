# flow-run autonomy — judge chain, usage-aware dispatch, bounded iteration, reversibility tiers

> Design contract for the 2026-09-03 upgrade of `gate/gate.py` + the `flow-run` / `run-queue`
> skills (`skills/flow-run`, `skills/run-queue`). Config keys below are the SSOT; `gate/README.md`
> documents mechanics, `docs/usage.md` the human view. Design decision (2026-09-03): `$flow`
> settles the requirement interactively; `$flow-run` runs it unattended. This document only
> covers the unattended half.

## 0. Goal

One `$flow-run` should end in one of two states, without a human in the loop in between:

1. every admitted task closed **and judged acceptable**, or
2. a concrete unresolved condition: missing authorization/information, an unavailable external
   dependency, or a cause with no supported correction after bounded diagnosis.

The runner owns deterministic attempts, verification, review caps and stop evidence. The host
owns diagnosis and admission of in-scope repairs between stopped runs. A request to finish
authorizes such repairs without another confirmation; it does not authorize new external or
irreversible effects. The shell supervisor does not interpret findings or write repair tasks.

## 1. Roles and model selection

| role | who | model selection |
|---|---|---|
| host | interactive CLI | its current model |
| judge | headless read-only review | configured chain: fable, opus, codex |
| worker | headless task process | frozen queue model and reasoning profile |

Workers, judges, revisions and fallbacks use explicit model and reasoning profiles
resolved while queueing. See [execution.md](execution.md) for declarations, admission,
persistent locks and controlled future-task updates. The host's current session and
later global CLI settings do not select queued execution. Codex and Claude receive
explicit model/effort flags; roles retain their permissions and review lifecycle.

## 2. Usage-aware dispatch (probe before spend)

Today benching is reactive: a worker is benched only after an attempt died on a quota error, and
that costs a dispatch plus a queue-row restore. New: **probe once per run, before the first
dispatch**, every candidate worker (run-wide list ∪ row pins ∪ judge chain CLIs).

- Probe = one tiny real request (`Reply with the single word OK`) per distinct profile, `probe.timeout_s` (90).
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
   carries score history and the last findings. The task stays DONE. This ends the runner,
   not the host's existing completion authorization. The host preserves that evidence, reads
   the failure and workspace state, and admits one supported in-scope follow-up before
   dependent tasks. A new run must pass normal admission, verification and independent
   review under unchanged thresholds/caps. No hand-run judge loop or blind retry.
   Repeated same-cause failure without progress gets one bounded diagnostic pass; another
   repair requires a materially different evidence-backed correction. Otherwise report the
   exact unresolved condition, asking only for actually missing information or authorization.
   See `skills/run-queue/SKILL.md` for the host recovery procedure.
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

Approval evidence is recorded per task id. An already-approved concrete implementation scope
can cover its necessary repair; the host records that original authorization and scope mapping
for the new ID, rather than copying a marker or asking again. New effects require authorization.
The host places independent irreversible work at the tail, preserving required dependencies.

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
round) and a **Waiting on you** section that is empty on a clean run. That heading is raw runner
output; the host first diagnoses whether it can repair the issue within existing scope. Report
what passed, what failed, the automatic recovery taken, and only decisions truly left to the user.
