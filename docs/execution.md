# Queue-bound models and reasoning

Execution selection belongs to the task, not to the host conversation. While
queueing, resolve real model IDs and supported reasoning settings once from
the user's instruction, existing project policy, and local CLI discovery.
Never infer selection again from a later session or global configuration.
Keep the host conversation's model separate from worker and judge selections.

## Declare, validate, freeze

`.gate/config.json` holds `execution.defaults`. The existing `workers` list and
queue `worker` column still choose harness order; the judge chain still chooses
judge order. Every eligible primary and fallback member needs an explicit profile.
The example model IDs below must be replaced with available concrete model IDs:

```json
{
  "workers": ["codex", "claude"],
  "judge": {"enabled": true, "chain": ["codex"]},
  "execution": {
    "defaults": {
      "workers": {
        "codex": {"model": "concrete-codex-model-id", "reasoning_effort": "medium"},
        "claude": {"model": "concrete-claude-model-id", "reasoning_effort": "medium"}
      },
      "judges": {
        "codex": {"model": "concrete-codex-model-id", "reasoning_effort": "medium"}
      }
    }
  }
}
```

Keep overrides next to the task in the only queue, as JSON in one HTML comment:

```text
<!-- task:WP-7 execution: {"workers":{"codex":{"reasoning_effort":"high"}}} -->
```

Roles are `workers`, `judges`, and `revisions`; each maps a harness/member ID to
`model` and `reasoning_effort`. Missing task fields inherit the declared queue
defaults, never the environment. If revisions are not separately configured,
they inherit the fully resolved task worker profiles. The lock materializes all
three roles, including fallbacks, so this inheritance cannot drift later.
Do not set a higher effort when the user capped the task/package at medium.

Run `flow admit --repo <project>`, then `flow lock-execution --repo <project>`.
`flow usage` probes only explicit profiles, after configuration, and accounts
for distinct profiles on the same CLI. Resolve unavailable selections within
existing replacement authority; do not silently change model IDs or effort.

`lock-execution` saves `.gate/execution-lock.json` and immutable receipts under
`.gate/execution-locks/`. `run` creates the first lock if absent, but refuses a
changed existing selection without an explicit lock update. Amendments also
append to `.gate/execution-changes.ndjson`, including a return to an earlier profile.
Missing profiles
block inference even with `--force` or advisory admission. Old queue files need
explicit defaults before their next run; inspection and acceptance commands
remain usable. No migration reads the host's current model silently.

## Dispatch and recovery

Each run saves `execution.json`; each task saves `<task>.execution.json` before
dispatch. All attempts, revisions, judges and probes use that task's frozen
profiles. New tasks read the current lock at their boundary; a task already in
progress keeps its snapshot through review. Session reuse still needs its usual
gate evidence, and a changed task profile forces a fresh session.

For an explicit user instruction to change future execution, edit the declared
profiles, then run `flow lock-execution --repo <project> --reason "<instruction>"`.
The command preserves completed/started entries and records the prior receipt.
If the changed default also affects a currently started TODO/IN_PROGRESS row,
use task overrides for pending rows instead. A chat model/effort switch alone
is not an instruction to run this command. Do not kill a worker to apply a setting.
Do not edit the lock by hand or clear it on resume, quota exhaustion or restart.

Codex uses explicit `--model` and `-c model_reasoning_effort=...`; Claude uses
`--model` and `--effort`. Old conflicting selection flags are replaced while
permissions, working directory and output-schema flags are retained. Hidden
fallback/profile flags are refused. Custom wrappers need a verified adapter;
they cannot claim enforcement from prompt text alone. Grok/Kimi currently have
model selection only: `not_applicable` plus `reasoning_note` is accepted only
when the host has verified there is no applicable effort control for that model.
If an effort setting is required, add/test an adapter or choose an authorized
supported executor; never translate it into a silently ignored flag.

`execution_requested` records the explicit selection. `execution_observed`
records exposed runtime metadata separately, with `unknown` when absent.
A reported mismatch stops acceptance of that invocation. Explicit launch flags
prevent configuration inheritance but do not prove an opaque provider's backend
snapshot. Do not report requested values as observed values.

## Planning handoff

Flow and flow-run share this schema. Plan Arena freezes its own planning-call
roster (contestants, chief, winner synthesis, final reviewer and replacements)
before any inference. That roster is not the implementation queue's roster.
When a final plan becomes a queue, flow records its implementation profiles
under the schema above; an arena winner does not become the worker by accident.
