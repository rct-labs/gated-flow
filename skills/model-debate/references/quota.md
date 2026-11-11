# Allowance admission and recovery

This reference governs account capacity checks, fallback routing and quota
recovery across `model-debate` and `flow-run`. It separates two operating modes
while keeping `scripts/quota_guard.py` strict and unchanged:

- **Formal quota gate (explicit project hard gate, strict `quota-plan.json`
  admission, or multi-round `debate` requiring verified headroom)**: run
  `quota_guard.py check` / `recover`. **Only an actual `READY` receipt permits
  launch.** `UNKNOWN`, stale, near-exhausted (`98%+` used / `<=2%` remaining) or
  unverified cross-unit observations fail closed (`BLOCKED`). Never fabricate a
  `READY` receipt, forge synthetic `KNOWN` samples in production, or bypass an
  applicable project-required guard.
- **Ordinary bounded selection (default single-round review or `flow-run` worker
  routing on existing authenticated subscriptions when no formal `READY` gate
  applies)**: honor owner exclusions and observed exhaustion/near-exhaustion
  before any probe or call, read available native read-only metadata without
  inference sweeps, and never use implicit paid fallback or overage. Where a
  provider has no read-only quota reader (for example Gemini `agy`, Claude/Fable
  or native `kimi`) or exposes incompatible units, treat `UNKNOWN` as unmeasured—
  **not** `0`, **not** exhausted, and **not** a comparable numeric rank across
  percent/currency/token/request units. Within explicit call caps (such as one
  blind call per seat in default single-round review or bounded `max_model_calls`),
  select by trustworthy same-unit capacity when comparable and otherwise follow
  documented project preference (or the available current host) while persisting
  `quota: unknown (<reason>)` and freshness timestamps in `CONTEXT.md` or run
  records instead of claiming `READY`.

This boundary never intercepts unrelated CLI commands, reserves provider
allowance, runs the debate, or makes a business workflow safe to replay.

## Before launch

1. Extract owner-excluded models AND account pools before discovery calls.
   A reported exhausted/excluded model is not tested by inference. Exclusions
   cover aliases, auxiliary models and fallback. Keep task exclusions in the run,
   not a permanent global blacklist. Separate CLI transport from paid account.
2. Resolve native executables, actual model families, account pools and owner
   authority. Multiple roles/models on one account share its remaining capacity.
   Read provider metadata/help without starting an inference turn (for Gemini
   `agy`, ensure `PATH` resolves the verified binary or existing TLS wrapper and
   check `agy --help`). The guard's quota readers do not establish full model
   identity, coding worker capability or sandbox isolation.
3. For formal quota-gated runs, write `quota-plan.json` (set `profile: "focused"`
   for single-round review with `remaining_calls: 1` per blind seat and zero
   automatic follow-up reserve; omitted profile preserves legacy two-round
   `debate` recovery) beside the run's `roster.md`. Include all remaining
   scheduled calls, inference probes and authorized retries/replacement work,
   plus previously used calls and in-flight calls. Reserve in-flight calls BEFORE
   starting them, subtract from remaining work, and retain the attempt record
   even after a crash. One host owns a run's plan. Serialize starts sharing an
   account; other sessions remain unreserved external usage.
4. Estimate whole remaining work per account, never per seat independently.
   Use recent comparable observed usage with sample count/provenance, or an
   explicitly disclosed conservative bootstrap estimate. Missing estimates are
   `UNKNOWN`. Subscription percentages, money, tokens and request counts are
   distinct units; never convert using invented prices, rank cross-unit numbers
   against each other, or assume a provider's hidden limits.
5. In formal quota-gated mode, run the helper below. For a batch of independent
   pools, one `READY` receipt may admit the reserved batch if launches occur
   within its freshness window (at most 60 seconds), bindings and budget are
   unchanged, and no failure occurs. Reserve each call before starting; do not
   reuse receipts for retries or later batches. In formal mode, only `READY`
   permits the listed roster to proceed. Save a NEW receipt for each check.
   Freeze the selected roster, backup order, replacement authorization, minimum
   model count and total call/wall budget. Persist the `READY` assignments as
   `frozen_assignments` in the next plan revision so normal checks stay on that
   exact roster without silently switching to a backup. Recheck just before
   every inference start; budget any inference probe itself. A successful tiny
   inference is not evidence of full-round allowance.

From the installed skill directory (use its actual resolved location):

```sh
python scripts/quota_guard.py check /path/to/run/quota-plan.json --out /path/to/run/quota-001.json
python scripts/quota_guard.py recover /path/to/run/quota-plan.json /path/to/run/failure.json --out /path/to/run/quota-002.json
```

No top-up, reset-credit redemption, paid overage, subscription purchase or global
CLI configuration change. Eligible known headroom is an estimate, not a promise
of completion. Inside `quota_guard.py`, unsupported or missing native schemas/routes
refuse formal `READY` admission (`QUOTA_UNKNOWN` / `QUOTA_SCHEMA_UNKNOWN`); never
replace an observed `UNKNOWN` with a guessed percentage in either mode.

## Plan contract

The helper is Python3.11+ standard library. `check` performs fresh bounded native
allowance reads. Its reusable pure `evaluate` function accepts synthetic
observations for offline tests; production CLI checks do not accept fake quota
files or a bypass switch. It emits a decision only; the skill host retains the
existing bounded, read-only launcher and completion records.

```json
{
  "version": 1,
  "min_models": 3,
  "project_min_models": 3,
  "max_calls": 15,
  "used_calls": 0,
  "max_age_seconds": 60,
  "safety_factor": 1.5,
  "excluded_models": [],
  "excluded_pools": [],
  "pools": {
    "account-a": {
      "observer": {"adapter": "codex", "executable": "/path/to/native/codex", "limit_id": "codex"},
      "per_call": {"percent": 1},
      "reserve": {"percent": 20},
      "estimate_basis": "declared_bootstrap",
      "estimate_evidence": "Owner-disclosed conservative estimate; no measured sample yet",
      "inflight_calls": 0
    }
  },
  "models": {
    "model-a": {"model": "RESOLVED_SELECTOR", "family": "RESOLVED_DISTINCT_MODEL", "pool": "account-a", "identity_evidence": "preflight/model-a.json"}
  },
  "seats": [{"id": "review-a", "candidates": ["model-a"], "remaining_calls": 3}]
}
```

This deliberately incomplete illustration cannot meet three-model quorum. Add
the real other accounts/models/seats; do not lower the minimum to make it pass.
The host sets `project_min_models` from applicable project policy, even if it
exceeds the generic skill minimum. Candidate lists are explicit ordered allowed
alternatives, not every model a provider sells. Pin a specifically requested
model to a singleton unless substitution is already authorized.

An account needs `reserve + safety_factor * per_call * (remaining + in-flight
calls)` in EVERY reported required window. Usage98%+ is always skipped for
percent windows. There is no anticipated reset credit. The example20pp and1.5
factor are conservative starting choices, not measured universal task costs.
For money, declare the balance currency, per-call estimate and cash reserve;
the included DeepSeek reader currently verifies CNY, not converted currencies.
A native Codex response can explicitly report `secondary: null`; that supplies
one observed window, not proof that every possible undisclosed limit is absent.
Missing fields or a missing primary still fail closed.

Included native readers (no model session or inference turn is started):

| Adapter | Observer fields and boundary |
|---|---|
| `codex` | Native executable, exact `limit_id`; ephemeral app-server `account/rateLimits/read`. Checks primary, any explicitly reported secondary, and reported individual window, refuses reached spend controls; never redeems reset credits. |
| `grok` | Native executable and resolved `model`; ephemeral ACP initialize/model availability and `_x.ai/billing`. Included weekly allowance; no inference or paid-overage enablement. |
| `deepseek` | Existing Kimi `config_path`, `model_alias`, actual `model`, `paid_access_authorized: true`. Checks the alias against the independent official DeepSeek route and literal configured key, then reads balance using that same account. Never invokes Kimi inference or falls back to its subscription. |

Windows readers require direct native executables, not shell command strings.
Each reader has an overall deadline; credentials stay out of argv and results.
There is no Claude/Fable, Gemini `agy` or native-Kimi quota reader in this
release. In formal `quota_guard.py` admission, pools without a verified native
reader evaluate to `UNKNOWN` / `BLOCKED` and cannot produce `READY`; in ordinary
bounded subscription selection (when no project `READY` gate applies), record
`unknown` explicitly and use documented project preference within explicit call
caps rather than inventing headroom. This is an explicit reader coverage boundary,
not a permanently excluded model list.

## When a call fails

Preserve prompt, stdout/events, stderr, completion and model lock. Do not turn
partial prose or exit0 without a valid answer into a completed vote. Reconcile
the exact owned process before retry; never launch a duplicate active seat.

| Observed failure | Response |
|---|---|
| Explicit account quota exhaustion | Quarantine the account for this run, including other aliases and seats. Do not retry the same exhausted pool. Re-plan using only authorized alternatives, fresh quota and budget. |
| Explicit transient rate limit | Honor Retry-After within the disclosed wait/retry budget; serialize shared-pool calls, then fresh preflight. A bare429 is not proof of daily/weekly exhaustion. |
| Timeout, ambiguous exit, auth or schema failure | Reconcile/repair first. Do not infer quota exhaustion or silently change model/account. |
| No eligible replacement or insufficient quorum/budget | Preserve all originals and write a resumable blocked checkpoint. Continue non-dependent local work; do not mark the debate complete or send unrelated tasks to another model. |

`recover` expects: `kind`, `model_ref`, `stage` (research/round-1/round-2), frozen
`assignments` by seat, `process_reconciled`, `read_only_verified`,
`context_unchanged`, `independence_verified`, `replacement_authorized`, `no_web`.
For transient rate limits add `retry_after_seconds`, `max_wait_seconds` and
`retry_budget_available`. Use booleans from actual evidence/owner authorization,
not defaults that manufacture permission. Update the plan's remaining work and
spent calls before recovery; keep `frozen_assignments` matching the failure record. After
a READY recovery, apply `required_plan_changes`, save the new frozen assignments
and record the roster revision before launch. In `debate`, a replacement must
budget its fresh research and both rounds, and all other seats need the repeated
round 2; in `focused`, default single-round recovery budgets only the replacement's
fresh round 1 (`repeat_seats: []`), repeating another seat only when an audited
explicit/hard-gate follow-up actually read the replaced seat's output. Reaching
the budget does not authorize extending it. Missing authorization is a scoped
roster/budget decision only; existing explicit substitution authority is reused,
not re-asked.

## What can survive a model change

Runtime quota quarantine may end only after a newly observed reset/recovery
and a fresh whole-run check, with that evidence recorded in a new plan revision.
Owner exclusions are separate and are never cleared automatically.

The replacement is a NEW model-bound seat attempt, not a continuation of the
failed model's thoughts. Give it only the frozen common packet and its role for
fresh independent research/round1. Do not expose the old seat's answer or peer
reviews while rebuilding independence. Original files remain immutable and
superseded results retain their model labels in the archive.

Other successful research/round1 answers can stay active only when their common
inputs, own role, evidence, model-lock and isolation fingerprints are unchanged.
If those changed, invalidate the dependent answers too. Rebuild the merged
index after a model change. In focused recovery, supply `repeat_seats` in the
failure record: the explicit dependency-audited list of follow-ups that consumed
the replaced answer, including an explicit empty list when none did. Each listed
seat needs one repeat call; a replaced seat needs one fresh round1 call plus its
listed repeat, if any. Missing dependency audit blocks recovery. Unrelated valid
round1 answers need zero extra calls. In debate profile (also the legacy omitted
profile), all round2 answers depend on the shared peer snapshot and are repeated.
Verify distinct models and project quorum from valid results in the final revision. Preserving originals is not the
same as counting them toward the new verdict.
