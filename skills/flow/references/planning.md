# Shape and queue authorized work

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
delivery, not a separate reviewed package for every module. Use the [shared risk and review rules](../../../docs/autonomy.md#1-stage-impact-and-risk); preserve explicit project review requirements. Write concrete
acceptance, supported inputs, non-goals and a delivery time budget in the spec.
Repair acceptance preserves original findings and their evidence; repair
verification is targeted, not a new audit. Plan development/module/integration/
delivery checkpoints in the existing spec, along with test impact and defect
due dates. Declare `<!-- task:ID impact: local|shared|unknown -->` (choose one).
Shared changes include affected callers; an unknown impact expands verification.
Group ordinary defects at checkpoints instead of creating a task per finding.
One repair batch is the default; further rounds require explicit user
authorization. Follow [shared repair and limits rules](../../../docs/autonomy.md#repair-continuation).

Before admitting, inspect implementation, callers and tests together so the
scope contains a complete verifiable behaviour. Reconcile earlier scope requests
against the spec instead of relaunching unchanged work. Choose task checks from
actual impact; keep full delivery acceptance. Then run
`flow admit --plan --repo <project>`. It previews declared checks without running
them and flags malformed rows, oversized packets and missing/broad checks.
Fix errors before launch. Warnings need engineering judgment, not automatic
command rewriting.

**Insert an urgent task** — add the row (with scope line) ABOVE the current
head, note the preempted task id in CONTEXT.md's checkpoint section. Small
fix inside the current package: fold it into that package instead. Still
fuzzy: it goes to a brief, not the queue.
