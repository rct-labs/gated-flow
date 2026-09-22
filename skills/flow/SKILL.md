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
  work package per feature. `evidence.md` holds what was verified and the
  package's candidate lessons. Templates: `<home>/flow/templates/`.
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
shows `APPROVAL` and the run stops there for them. Plan a usable vertical
delivery, not a separate reviewed package for every module. Ordinary small
reversible tools default to acceptance without model review. For sensitive
behavior, keep one focused review at the delivery boundary when enabling
`judge.enabled`; honor explicit project review requirements. Write concrete
acceptance, supported inputs, non-goals and a delivery time budget in the spec.
Repair acceptance preserves original findings and their evidence; repair
verification is targeted, not a new audit. Plan development/module/integration/
delivery checkpoints in the existing spec, along with test impact and defect
due dates. Declare `<!-- task:ID impact: local|shared|unknown -->` (choose one).
Shared changes include affected callers; an unknown impact expands verification.
Group ordinary defects at checkpoints instead of creating a task per finding.
One repair batch is the default; further rounds require explicit user
authorization. Follow `flow-run`'s continuation policy.

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

**Hand off at a phase boundary** — three moments are natural handoff points:
research is done and implementation starts, a work package closed, the user
switches to another task. At one of them, and only there, consider whether a
fresh session would serve the user better. No extra model call, no check
after every tool use.

- Context pressure is advice, never a command. Use a real usage figure only
  when this host states one reliably (Host notes below). No figure means
  "unknown": never zero, never a guessed percentage. A count of tool calls
  says nothing about the window and never forces a handoff.
- Before a handoff or a compaction, make the state recoverable: rebuild
  CONTEXT.md from the template's five sections, keeping the goal and
  constraints, the user's decisions, what is uncommitted, any unresolved
  failure with its exact command, the next action, and the paths of the
  evidence. Refer to durable files; do not copy logs.
- Never hand off or compact in the middle of an unresolved edit, a failing
  test or a debugging chain because a number was crossed. Reach a
  recoverable state first. An interrupted task is written as interrupted,
  never as done: only the gate closes a task.
- Compaction itself belongs to the host. Do not imitate it, call an
  unsupported API for it, or prune a transcript.
- Say it once. Repeat only when the state or the pressure really changed.

**Capture a lesson** — only when something concrete happened in the current
work package: the user corrected the work, a failure was reproduced and its
fix verified, or the same observation recurred and would prevent a future
mistake. Not after every session, and not from one choice the user made.
Add or update one row in that package's `evidence.md` (template: Lessons),
with the observed fact kept apart from the proposed generalization, and a
commit, file and line, test or durable log as evidence.

- A candidate is not an instruction. It changes no prompt, no worker policy
  and no config until the user approves it. Then write the rule once, in the
  project's conventions (`AGENTS.md`) or an ADR, record that destination in
  the row and mark it `accepted`. Never keep the same active rule in several
  places.
- Scope is this repository. A lesson never becomes a global skill or another
  project's convention unless the user says so explicitly; seeing it in two
  projects is evidence, not permission.
- A standing user instruction always wins. A lesson that contradicts one is
  shown to the user, not applied. A superseded lesson keeps its row and its
  evidence.
- No confidence scores: evidence and status decide. No transcript, credential
  or runtime artifact goes into a lesson.

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

## Host notes: context usage

Kept apart from the rules above because it differs per CLI. As known on
2026-09-20; when in doubt the answer is "unknown".

- Claude Code compacts on its own and says when it did; `/compact` and
  `/context` are the user's commands. Use a usage figure only when the
  session itself states one; otherwise it is unknown. This workflow installs
  no hook to compute one from the transcript.
- Codex CLI, Kimi CLI, Grok Build: no documented usage source the host model
  can read. Unknown.
- A figure, if a host ever provides one, needs its window size from that
  host's own metadata, and cached tokens counted once. Never assume a window
  size from a model name.


## Repair continuation

Automatic continuation is OFF by default, including when blockers decrease.
Allow one consolidated high-risk/core-flow repair batch and targeted verification;
then stop if still blocked. Ordinary findings are recorded for later maintenance.
Further repair requires explicit user authorization, named tasks and an expiry;
spare budget or a generic continuation request is not authorization. Follow the
continuation contract in `flow-run` and `docs/autonomy.md` before dispatch.
