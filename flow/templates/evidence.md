# Evidence — <work-id>: <title>

> Written <YYYY-MM-DD>. What was verified and where the proof is. Point at
> commits, tests and logs; do not paste them.

## What shipped
<!-- Commits and the files they changed, against the spec's acceptance. -->

## Verification
<!-- Each command as run, its result, and what it proves. What was not
     verified, and why. Canonical rule: <flow home>/docs/autonomy.md,
     section `3. Review and defect records`.
     Lookup: `cat "$(flow home)/docs/autonomy.md"`
     Use the installed flow home; this project need not contain flow docs.
     For review evidence include:
     trigger, safe repro command if run, observed/expected result, invariant;
     otherwise source proof and explicit unverified/unsafe/unavailable limits. -->

## Remaining limits
<!-- What this package does not cover; known gaps. -->

## Lessons
<!-- Candidates only. Write a row when one of these happened in this
     package, and not otherwise: the user corrected the work; a failure was
     reproduced and its fix verified; the same observation recurred and
     would prevent a future mistake. One row per lesson: a repeat updates
     the existing row. Never a row per session, never a preference guessed
     from one choice.

     Existing status values candidate | accepted | rejected | superseded stay
     readable. New fields below are optional; do not backfill invented evidence.
     Missing evaluation, usage or activation evidence is unknown.
     Canonical rule: <flow home>/docs/autonomy.md,
     section `Evidence learning and bounded experiments`.
     Lookup: `cat "$(flow home)/docs/autonomy.md"`
     Use the installed flow home for the single promotion rule:
     observed failure -> proposed scoped change -> regression / evaluation -> authorized activation -> evidence + rollback. A candidate
     changes no policy or config. Existing explicit authorization may cover a
     scoped activation; otherwise keep it pending. Preserve rejected/superseded
     rows and their evidence. Link owning rule/enforcement in destination and
     evaluation; include the guard failing on the observed bad case before
     promotion. No global propagation without explicit scope. -->

| id | date | trigger (observed fact) | proposed action (generalization) | scope | evidence | status | destination | evaluation | activation authority / revision | rollback |
|---|---|---|---|---|---|---|---|---|---|---|

## Bounded experiment (only when needed)
<!-- For an external idea or proposed lesson change, reuse this work package;
     do not create a learning database or plugin registry. -->

- Source and observed failure:
- Owning rule, consumers, authorized scope and non-goals:
- Frozen baseline / candidate revisions, representative supported tasks/inputs:
- Frozen quality/safety floors and end-to-end success criteria:
- Regression / evaluation command, result and evidence path:
- End-to-end successful results, failures and retries for each candidate:
- Evidence kind: offline fixture/replay or live model behavior; limits:
- Actual provider usage, cache coverage/semantics (missing = unknown):
- Subjective comparison only if needed: blind labels, one scoped judge, authority:
- Decision: keep with meaningful evidence / revert / inconclusive; reason:
- Cumulative budget, stop condition and remaining limits:
- Activation authority, destination and revision (pending until authorized):
- Rollback target, procedure and verification:
