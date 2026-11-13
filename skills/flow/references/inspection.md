# Read-only inspection and help

**Is the project getting messy?** — run the read-only auditor:

```
flow audit <project> --out <scratchpad>/audit-<name>.md
```

Report the category counts, the oversize list, and the top duplicate/stale
findings. It proposes only — v1 has no write mode. Actual moves/deletes are a
human-approved batch, smallest categories first (generated/runtime), never in
the same breath as the audit.

**High-stakes uncertainty** — only when uncertainty, impact, and
irreversibility are ALL high, suggest the `model-debate` skill. Never by
default.

**How do I… / which command was it** — read `<home>/docs/usage.md` and
answer plainly. Then OFFER TO RUN the command yourself instead of making the
user type it — the user's contract is zero command memorisation: they say
what they want in their own language, you translate it into flow/gate
invocations.

## Hard rules

- English for state files, queue fields, code; any language is fine for
  prose docs.
- No new state homes: `.gate/` is machine truth, CONTEXT.md is the human
  snapshot, work packages hold the rest. Never create `.flow/`, `.pipeline/`.
- No file moves or deletes from audit results without explicit per-batch
  human approval.
- Never edit acceptance/verify commands to make a queued task admissible.
- Execution requests hand off to $flow-run (or $run when the queue is already
  admitted), not to a loop you run.
