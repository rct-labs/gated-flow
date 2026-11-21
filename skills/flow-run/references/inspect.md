# Host inspection and decisions

## 0. Worker selection and pin

No new skill or parameter syntax is used. Bare `$flow-run` inspects and resumes
authorized work and selects a suitable available worker automatically; an inline
or natural-language worker request overrides automatic selection:

| user said (inline or natural language) | worker id |
|---|---|
| codex, codex cli | `codex` |
| gemini, agy, antigravity | the existing verified Gemini worker key (for example `agy` or `agy-flow`) |
| claude, claude code | `claude` |
| kimi, kimi cli | `kimi` |
| grok, grok build | `grok` |
| pi, openrouter, or an OpenRouter model id | `pi` (set `worker_models.pi`) |

Unknown requested worker name: list the supported ids and stop; never substitute
silently. Distinguish explicit user intent:

- **One-off exact pin** (e.g. "use X this time" or an exact owner model/effort demand): bind strictly for that run with no fallback, constraining `workers` and reconciling only conflicting queued row pins while preserving other rows without broadening; do not persist as project default, and do not feed route-order learning. An ordinary queued row worker pin is not a strict owner demand—`gate/gate.py` preserves its fall-through to the run-wide `workers` list.
- **Tolerant preference** (e.g. "prefer X"): place X first with verified policy-eligible fallbacks (preserving conditionally eligible fallbacks); do not persist as project default unless asked.
- **Explicitly persisted scoped project default** (durable preference, e.g. "default this repo/class to X"): save the scoped default in `CONTEXT.md` (and apply to `.gate/config.json`) only when explicitly requested; never treat historic `.gate/config.json` state or the current host CLI as a durable default.

Filter candidates by live policy eligibility and availability first, then order eligible choices by **user override > explicit durable preference > learned/default class rank** among installed, non-benched subscription CLIs:

- **Eligibility vs entitlement**: apply the [owner model-policy authority](../../../docs/autonomy.md#model-routing-authority) first; all overrides, preferences, and helper rankings are subordinate to live installed policy, and missing or invalid policy is never bypassed. Filter out policy-ineligible, unavailable, owner-excluded, or evidenced-exhausted choices while preserving conditionally eligible fallbacks; if no authorized eligible route remains, stop and report. Local CLI presence, `--help` model visibility, or a `route.py` candidate entry shows candidate visibility only—never subscription entitlement, role permission, or paid/overage allowance. An explicit CLI choice does not authorize a model outside its allowed role. A custom worker needs a verified headless edit/shell/git contract in `worker_cmds`; tools-off success is not coding capability. The first useful authorized implementation call establishes live capability; never run benchmarks, trial pings, or probes.
- **Deterministic class routing (`route.py recommend`)**: for a **new** package (unless an owner strict pin locks a single worker), classify the package as `routine` or `complex` and run `python3 "$(flow home)/skills/flow-run/scripts/route.py" recommend --repo . --class routine` (use `--class complex` when appropriate) to obtain learned/default exact model+effort pins and reason, ordered behind any tolerant user override or explicit durable preference. On resume of an active stopped package, keep its reconciled current pin while it remains live-policy eligible and available unless overridden; if the resumed pin is blocked/missing or `route.py` is unavailable, apply any explicit durable preference in `CONTEXT.md`, otherwise validate the same class defaults (`routine` to Gemini and `complex` to Codex with mutual backup) independently against live owner policy—never use historic `.gate/config.json` or the current host CLI as a durable default, and report if no authorized eligible route remains. Vendor docs state model tier positioning as facts ([Antigravity models](https://antigravity.google/docs/models), [OpenAI model selection](https://developers.openai.com/api/docs/guides/model-selection)), whereas Flow's chosen defaults route `routine` to Gemini and `complex` to Codex with mutual backup. Qualifying backup rescues adjust subsequent class order; a policy-confirmed same-role successor takes its predecessor's slot with zeroed scores, and an unsuccessful trial is demoted after two attributable implementation failures. Empty history is neutral/untested—never report empty state as "good".
- **Local availability**: recommendations and admission read bounded local policy/account state only. They never refresh catalogs, fetch release sources, wait for learner, or infer entitlement from a listing. `flow inventory status --json` reports the machine cache. Existing learner `due --live` refreshes discovery out of band on an independent seven-day cadence; explicit `flow inventory refresh --live` uses the same cadence, including failed attempts. Keep quota, owner exclusions and infrastructure outages out of quality/rescue learning. See [inventory commands](../../../docs/usage.md#local-model-inventory).
- **Honest quota handling**: read only local/read-only capacity evidence that is
  already available; never run an inference quota sweep (`flow usage` or
  candidate pings) and never use an implicit paid API or overage fallback.
  `UNKNOWN` metadata and cross-unit allowances (percent vs currency vs tokens vs
  requests) are never treated as `0`/exhausted and must not be ranked as
  comparable numeric capacity (same units alone are also insufficient without
  account scope, window/reset time and task-cost context). Evidenced lack of
  capacity filters choices first while preserving conditionally eligible
  fallbacks; `flow-run` does no capacity-first re-ranking among eligible choices,
  keeping **user override > explicit durable preference > learned/default rank**
  and stating observed freshness or the explicit unknown-quota rationale.
- **Persistence**: keep inter-model packets, worker prompts and machine state
  in English; report in the user's language. Say `host: <CLI> / worker: <id>
  (<pin or auto rationale>)`. Save the role/model/effort choice, route reason, and capacity
  freshness or `unknown` in existing CONTEXT and stopped-run configuration.
  Reconcile prompts, models and any conflicting queue pins (preserving other rows
  without broadening) before admission; do not modify a running configuration or
  closed receipts.

## 1. Inspect (always)

Start with `flow resume --repo .`: one bounded read-only packet of CONTEXT
sections, the configured queue, `git status`/recent commits, the last gate
run and verdict, `.gate/RUN-REPORT.md` and work packages, plus the facts that
override the snapshot. Do not re-read those sources one by one unless it
reports a gap. Then `flow doctor --repo .` and `flow admit --repo .`. Tell the
user five facts in their language: goal, now, last checkpoint, next step,
anything waiting on a human. Then continue. Gate facts win over CONTEXT prose:
an interrupted run, a failed or stale verdict or an IN_PROGRESS head is
reported as such, never as done. When the user refers to an earlier decision,
`flow recall <words>` before reopening it (see `flow`).

Do not launch when the pre-commit hook is missing, the head task is BLOCKED
or IN_PROGRESS, or the worktree is dirty with work you did not make. For a
recorded scope stop, apply [scope request recovery](../../../docs/autonomy.md#scope-request-recovery)
while stopped before another dispatch; this preserves partial work and reconciles
state rather than overriding this launch check. No
`verify_cmd`: say admission will refuse code tasks until the user sets one.

### Bounded learning evidence

- `flow resume --repo .` surfaces at most three bounded learning recommendations
  (benefit, fit, evidence, uncertainty, effort/risk and next action) when
  available, and stays quiet when none exist. Do not fabricate or repeat empty
  learning sections during normal authorized execution.
- Learning recommendations and user-curated sources/URLs are advisory only and
  never grant adoption or execution authority: paused or `BLOCKED` tasks (such
  as `CR-1`) remain paused; evaluate them under
  [evidence learning and bounded experiments](../../../docs/autonomy.md#evidence-learning-and-bounded-experiments).
- For natural-language project-improvement, preference-feedback, language, or
  learning-pause requests, route via `flow learn` (`onboard`, `check`,
  `results`, `feedback`, `pause`, `status`) using goal-level context without
  asking users for daily keywords, models, or skill files.

## 2. Decide

Before selecting executable work, apply the shared
[budget continuation decision](../../../docs/autonomy.md#budget-continuation-decision):
distinguish owner-set caps (actual grant required), host-created operational
batches (replenishable with a finite evidenced task-sized increment under
explicit owner instruction to finish feasible existing work), real provider
exhaustion (no paid fallback), and ambiguous authority (stop and confirm).
Carry remaining allowance across sessions without resetting cumulative counts.

Use [shared adaptive workflow, stage and budget rules](../../../docs/autonomy.md).
Select simple, scoped or sensitive from actual behavior without asking the user
for a mode. Set the next milestone's stage before dispatch, retaining any
existing mandatory review. Record observable acceptance, supported inputs,
non-goals and cumulative limits in the existing spec and CONTEXT. Record only
[decisive safety assumptions](../../../docs/autonomy.md#critical-safety-assumptions)
for the changed behavior and the evidence needed to resolve them.

Prefer vertical tasks that produce a runnable user flow early. Necessary
implementation slices share a delivery acceptance; do not split each module
into a separately reviewed package merely to satisfy a file cap. Cleanup and
future extensibility stay outside the current delivery.

Survey only the code and docs the next step touches. If CONTEXT section 4 is
already a testable slice, use it; otherwise write or update
`docs/work/<id>/brief.md` and `spec.md` from the flow templates and rebuild
`CONTEXT.md` (at most 200 lines, five questions, never append). Do not ask
the user to pick among options this skill exists to settle; record the choice
under Decisions in the spec.

The survey ending here is a phase boundary: rebuild `CONTEXT.md` and save it
with `flow checkpoint --from <draft> --expect <digest>` (`flow`: Save a
checkpoint) before launch, so the run can start from a new session if this one
is heavy. `flow` has the rule (Hand off at a phase boundary); it is advice,
never a required step.
