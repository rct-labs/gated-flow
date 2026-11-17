# Shape and queue authorized work

**New idea / requirement** — shaping is proportional host reasoning, not a
mandatory form or questionnaire:
1. **Survey first**: in an existing codebase, read the code, callers, tests and
   past decisions the request touches (failure and rollback surfaces included);
   for greenfield work without an existing codebase, ground in stated
   requirements, exemplars and constraints rather than presuming existing code
   or an imagined system. Weigh only the engineering lenses relevant to the
   slice: concrete outcome and non-goals, applicable data ownership and
   interface contracts, existing reuse or greenfield baseline, and quality
   constraints that materially affect design or acceptance.
2. **Match depth to actual risk and ambiguity**: when existing code and a clear
   reproduction settle a small local fix, state the scoped change and decisive
   check directly without ceremonial questions. Sensitive repairs (data,
   permissions, money, destructive or hard-to-reverse paths) keep full risk
   judgment and clarify any ambiguous safety invariant before editing.
3. **Restate and ask only decisive questions**: when requirements or shared
   boundaries are genuinely open, state the core invariants (marking user
   examples as examples) and what the current system or baseline already owns or
   conflicts with. Discuss in small rounds — one focused question at a time,
   paired with your grounded recommendation and pushback when a simpler option
   fits; record relevant conclusions and routine implementation choices in
   `spec.md` Decisions.
4. **Stabilize before queueing**: capture exploratory intent in
   `docs/work/<id>/brief.md`, then write `spec.md` when the goal is testable
   (edit in place and bump `revision:` on change — never a `spec-v2` file). Use
   the cheapest decisive proof for remaining uncertainty; for uncertain
   greenfield work, resolve decisive product and critical-risk unknowns rather
   than waterfall-specifying every detail upfront, then shape an early runnable
   vertical slice. Never turn unresolved fuzziness into falsely precise tasks or
   skip critical risks.

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

For genuine docs-only deliverables, opt in with `<!-- task:ID kind: document -->`
(default `code`): declare substantive non-status `files:` and an explicit real
task-specific `verify:` command (content validation is task-owned; see
[document task rules](../../../docs/autonomy.md#2-test-selection)). Never
reclassify code tasks as `document` or weaken checks to bypass Gate 1.

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
