---
name: flow
description: >
  $flow — interactive planning that executes nothing: capture and shape a
  fuzzy idea into brief/spec, scaffold a work package, queue admission-ready
  tasks, insert an urgent task, hand off a session via CONTEXT.md, and audit
  a project tree for drift (read-only). Use when the user types $flow or
  /flow, or asks to tidy the project, hand off or switch a session, insert a
  task, or turn a new idea into tasks without running them; also responds to
  equivalent requests in other languages. Any request to continue, keep going
  or finish the work is $flow-run, which loads this skill for its planning
  steps.
---

# $flow — plan without executing

For separately installed skills, `flow home` locates repository documents:
resolve shared rules at `<home>/docs/autonomy.md` and other repository links
against that home. Within-skill references stay relative to this skill.

Route the user's intent; no extra syntax or stage vocabulary is required.
This entry prepares work only. Execution requests go to `flow-run`, or to
`run-queue` for an already admitted queue. Loading a reference grants no action.

Read [shared workflow rules](../../docs/autonomy.md) for model-policy authority,
adaptive risk selection, approval, review, limits and lesson promotion. Read
only the conditional reference needed for the current intent:

| Intent | Reference / action |
|---|---|
| New idea, options, prototype, competing plans | [Planning](references/planning.md): survey, discuss, brief/spec |
| Queue work or insert an urgent task | [Planning](references/planning.md): scope and admission |
| Resume, switch session, save checkpoint, recall decisions | [Continuity](references/continuity.md) |
| Capture a verified lesson or evaluate an external idea | [Evidence learning](../../docs/autonomy.md#evidence-learning-and-bounded-experiments) and [template](../../flow/templates/evidence.md) |
| Audit project mess, high-stakes uncertainty, command help | [Inspection](references/inspection.md) |

`flow home` locates the installed checkout and its templates and docs.
`flow init --repo <project> --verify-cmd "<cmd>"` creates missing files only.
Without a real verify command, leave it empty and report the no-oracle refusal.

Keep one `TASK_QUEUE.md`, work packages in `docs/work/<id>/`, machine truth in
`.gate/`, and the human snapshot in `CONTEXT.md` (five sections, at most 200
lines, rebuilt rather than appended). An `external_context` project keeps its
own handoff conventions. English queue fields, code and machine state; reports
follow the user's language. Never alter acceptance to get an admission PASS.
