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
silently.

When no worker is specified, choose automatically among installed, non-benched
subscription CLIs:

- **Eligibility**: apply the [owner model-policy authority](../../../docs/autonomy.md#model-routing-authority).
  An explicit CLI choice does not authorize a model outside its allowed role.
  A custom worker needs a verified headless edit/shell/git contract in
  `worker_cmds`; tools-off success is not coding capability. The first useful
  authorized implementation call establishes live capability; no trial ping.
- **Honest quota handling**: read only local/read-only capacity evidence that is
  already available; never run an inference quota sweep (`flow usage` or
  candidate pings) and never use an implicit paid API or overage fallback.
  `UNKNOWN` metadata and cross-unit allowances (percent vs currency vs tokens vs
  requests) are never treated as `0`/exhausted and must not be ranked as
  comparable numeric capacity. When trustworthy comparable capacity cannot decide (same units alone are insufficient
  without account scope, window/reset time and task-cost context),
  follow the documented project preference in `.gate/config.json` / `CONTEXT.md`
  (otherwise the available current host CLI if coding-capable) and state the
  explicit unknown-quota rationale.
- **Persistence**: keep inter-model packets, worker prompts and machine state
  in English; report in the user's language. Say `host: <CLI> / worker: <id>
  (<pin or auto rationale>)`. Save the role/model/effort choice and capacity
  freshness or `unknown` in existing CONTEXT and stopped-run configuration.
  Reconcile prompts, models and queue pins before admission; do not modify a
  running configuration or closed receipts.

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

## 2. Decide

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
