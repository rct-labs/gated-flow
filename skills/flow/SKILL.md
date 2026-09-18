---
name: flow
description: >
  $flow — the single interactive entry for the personal development workflow:
  capture and shape a fuzzy idea into brief/spec, scaffold a work package,
  queue admission-ready tasks, insert an urgent task, resume or hand off a
  session via CONTEXT.md, and audit a project tree for drift (read-only).
  Use when the user types $flow or /flow, or asks to tidy the project, switch
  or hand off a session, continue, insert a task, or turn a new idea into
  tasks in a project governed by this workflow; also responds to equivalent
  requests in other languages. Unattended execution is NOT this skill — that
  is $run (the run-queue skill).
---

# $flow — interactive workflow entry

One entry, few fixed actions. You are the router; every action lands on a
deterministic file convention or a read-only script. The `flow` command is on
PATH (`flow init|home|audit|admit|doctor|run|verify|usage`, dispatching to
`gate/gate.py` and `flow/audit.py`). `flow home` prints the checkout
directory; the design contract is `<home>/docs/design.md` and the user manual
is `<home>/docs/usage.md`. Never expose internal stage names to the user; just
do the thing.

**Onboard a project**: `flow init --repo <project> --verify-cmd "<cmd>"` —
create-if-missing only, never overwrites. If the user gives no verify
command, leave it empty and tell them admission will refuse everything with
no-oracle until they set one; never invent an oracle for them.

## Files this skill owns (per project)

- `CONTEXT.md` — ≤200 lines, answers exactly five questions: goal & red
  lines / what is being worked on now / last stable checkpoint / next step /
  decisions waiting on a human. Rebuild, never append.
- `docs/work/<work-id>/brief.md` + `spec.md` (+ `evidence.md`; `prototype/`
  only when a repository-held prototype is explicitly needed) — one vertical
  work package per feature. Templates: `<home>/flow/templates/`.
- `TASK_QUEUE.md` — the ONLY task list (gate.py format). Never create
  tasks.yaml / tasks.json / a second queue.

## Route by intent

**New idea / requirement** — shaping is a conversation grounded in the
current system, not a form to fill. In order:
1. **Survey first**: read the parts of the codebase and docs the idea
   touches (data model, existing screens/flows, related past decisions).
   Never shape against an imagined system.
2. **Restate**: say back what you understood — the invariants of the idea,
   with the user's examples explicitly marked as examples, plus what the
   current system already has or conflicts with.
3. **Discuss in small rounds**: one focused question at a time, each paired
   with your recommendation and why (drawn from the survey). Offer better
   alternatives when you see one; the user wants pushback, not dictation.
4. Only when the shape is stable, write `docs/work/<id>/brief.md` from the
   template, then `spec.md` when the goal is testable. Spec changed later?
   Edit the same file, bump its `revision:` line — never a spec-v2 file.
Never rush to tasks: a fuzzy idea that skips discussion becomes falsely
precise tasks — the exact failure this workflow exists to prevent.

**Show me options / prototype** — use the lowest-cost proof that matches the
question:

- Static structure, flow, or state → a Mermaid diagram in the conversation.
- Interactive behavior or a UI mockup → an in-conversation prototype when the
  host offers one (for example a `visualize` capability). Keep the first
  version focused; if the proof grows into a multi-page or persistent
  application, move it to a hosted-site capability if the host has one,
  instead of stretching the inline mockup.
- A hosted or durable multi-page prototype → a hosted-site capability;
  publish only when the user authorizes hosting.
- Write a self-contained HTML file under `docs/work/<id>/prototype/` only when
  the user explicitly wants the prototype saved/versioned with the repository,
  or when no in-conversation visualization capability is available. Delete
  that directory when the package closes.

For visual alternatives, show 2–3 materially different variants only when the
user asked to compare options. Record the chosen behavior, important states,
and conclusions in `spec.md`; do not rely on the prototype as the requirements
source of truth.

**Compete on a plan** — when the user requests independent perspectives,
competing designs or winner-led synthesis, and a `plan-arena` skill is
installed, invoke it before shaping the final brief/spec. Pass the resolved
project, the user's objective, the source draft and the constraints. Let it
derive roles from the actual problem; do not prescribe domain personas or map
roles to fixed models. Return its final plan, evidence and review status to
this workflow. An arena verdict does not admit tasks or replace a required
project debate. If no such skill is installed, say so rather than simulate
independent participants inside one context. This is a skill route, not a
`flow --arena` CLI flag.

**Queue it** — append rows to `TASK_QUEUE.md` in the project's queue format,
each with a scope line so admission can judge it:

```
<!-- task:WP-7 files: src/foo.py, tests/test_foo.py -->
```

Optionally give the table a `worker` column (`claude` / `codex` / `kimi` /
`grok`, empty = no pin) so each task goes to the CLI that suits it; gate
falls through to the run-wide list when the pin is benched.

A task whose declared files hit the project's `irreversible_globs`
(migrations, `data/**`, hooks, settings) goes to the **tail** of the queue and
is dispatched only with its own `<!-- task:ID approved: <who/date> -->` line.
Write that line only when the user said yes to that task; otherwise `admit`
shows `APPROVAL` and the run stops there for them. Every closed task is then
scored by the read-only judge chain when the project enables `judge.enabled`;
its acceptance in `spec.md` is what the judge reads, so write acceptance the
judge can check.

Then run `flow admit --repo <project>` and show the report. A task refused
admission is re-shaped now (split it, narrow it), not argued with later.

**Insert an urgent task** — add the row (with scope line) ABOVE the current
head, note the preempted task id in CONTEXT.md's checkpoint section. Small
fix inside the current package: fold it into that package instead. Still
fuzzy: it goes to a brief, not the queue.

**Resume / switch session** — read CONTEXT.md, `git log --oneline -10`, the
queue, and `.gate/journal.ndjson` tail. Rebuild CONTEXT.md as a fresh ≤200
line snapshot answering the five questions. That file is the handoff — never
write a separate handoff document.

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
- Unattended execution requests hand off to the run-queue skill ($run), not
  to a loop you run.
