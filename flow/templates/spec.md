# Spec — <work-id>: <title>

revision: 1
<!-- Edit THIS file on change and bump revision. Never create spec-v2. -->

## Behaviour
<!-- The approved behaviour, precise enough to fail a test against. -->

## Acceptance
<!-- Commands or observable checks. These become verify_cmd / queue
     baselines; a spec whose acceptance can't fail is not finished. -->

## Critical assumptions (only when needed)
<!-- Canonical rule: <flow home>/docs/autonomy.md,
     section `Critical safety assumptions`.
     Lookup: `cat "$(flow home)/docs/autonomy.md"`
     Use the installed flow home; this project need not contain flow docs.
     List only the few decisive assumptions for changed behavior: invariant,
     affected callers/surface, strongest inexpensive evidence, unresolved risk.
     Record evidence and unavailable/unsafe checks in the package evidence. -->

## Handoff TASK-ID
<!-- Optional task-specific section; omit when there is no verified handoff.
     Replace BASE_COMMIT with the full Git commit verified against the listed
     project-relative related files/directories. Use explicit paths, not globs.
     Include callers even when outside edit scope; this grants no scope.
     Gate checks committed, staged, working and untracked related-file drift.
     Stale/unverifiable anchors require source rereading and replanning.
     Keep this short: oversized packets are refused, never truncated. -->
<!-- handoff: {"base": "BASE_COMMIT", "files": ["src/entry.py", "tests/test_entry.py"]} -->
Entry/callers: `entry.handle` is called by `service.run`; checked at the base above.
Reuse/exemplar: extend `entry.validate`; follow its existing error contract.
Decisions: preserve the public signature and existing approval boundary.
Stop conditions: request scope before changing callers or the acceptance contract.

## Checkpoints and impact
<!-- Current stage: development / module / integration / delivery.
     Name the observable completion condition for each relevant checkpoint.
     Declare local/shared/unknown impact, affected callers and commands.
     State when full acceptance is needed; unknown impact is conservative.
     Record the delivery budget and ordinary-defect handling checkpoints.
     Survey implementation, callers and tests before declaring files. Resolve
     prior scope requests; use flow admit --plan to preview errors and checks.
     Prefer affected task checks where coverage permits; delivery stays full.
     Keep supported inputs and non-goals explicit; review does not expand them. -->

## Decisions
<!-- Choices made and why, one line each: date, current|superseded|rejected,
     the decision, why, source (user correction, test, commit). Mark a
     replaced choice superseded in place; never delete it. `flow recall`
     finds these lines and labels superseded ones. Long-lived ones graduate
     to docs/adr/. -->
