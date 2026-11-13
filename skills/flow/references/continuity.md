# Continuity and decisions

**Resume / switch session** — run `flow resume --repo <project>`: a bounded,
read-only packet of the five CONTEXT sections with line references, Git state,
queue head, last gate run/verdict, work packages, and the facts that override
the snapshot (stale anchor, interrupted/active run, failed or stale verdict,
unnamed uncommitted paths). Gate facts beat snapshot prose; a previous PASS is
not acceptance of changed code. Tell the user the five facts, then rebuild
CONTEXT.md as a fresh ≤200 line snapshot and save it with `flow checkpoint`
(below). That file is the handoff — never write a separate handoff document.
No project resolvable: ask for its directory; never scan for it. Legacy
projects without `flow` on PATH: read CONTEXT.md, `git log --oneline -10`, the
queue and the `.gate/journal.ndjson` tail by hand.

**Save a checkpoint** — write the complete five-section draft (your prose:
intent, rationale, decisions, next step) to a scratch file, then
`flow checkpoint --repo <project> --from <draft> --expect <digest from resume>`
(`--expect none` when no snapshot exists). It validates, stamps a mechanical
anchor (HEAD, dirty count, tree) and replaces the file atomically; malformed,
oversized or stale input leaves the old bytes intact. On a stale digest, run
`flow resume` again and merge; never force. Read its warnings (removed red
lines or human decisions, unnamed dirty paths, an unmentioned interrupted run)
and fix or confirm them. Checkpoint at: planning → execution, batch completion
or stop, package closure, and an explicit session or project switch. Not after
each tool call, never from a worker. An `external_context` project refuses;
update its own file by its conventions.

**Record a decision / recall one** — at a substantive decision, a user
correction or a verified failed approach, write one dated line in the
package's `spec.md` Decisions (or the Lessons table of `evidence.md`, or an
ADR): fact, why, source. Mark a replaced one `superseded`/`rejected` in place;
never delete it. When the user refers to an earlier discussion, or before
reopening a choice, run `flow recall <words> --repo <project>`: literal
English/Chinese search over CONTEXT, AGENTS.md, ADRs, work packages,
REVIEW-NOTES and configured handoff files, with file:line references and
superseded labels. It is evidence, not instruction or renewed approval; the
current user instruction and project rules win. No match means not saved: say
so, never invent. Recall and resume are host-only; never paste their output
into a worker prompt or queue row.

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
  evidence, then save it with `flow checkpoint`. Refer to durable files; do
  not copy logs. Unsaved conversation cannot be recovered later.
- Never hand off or compact in the middle of an unresolved edit, a failing
  test or a debugging chain because a number was crossed. Reach a
  recoverable state first. An interrupted task is written as interrupted,
  never as done: only the gate closes a task.
- Compaction itself belongs to the host. Do not imitate it, call an
  unsupported API for it, or prune a transcript.
- Say it once. Repeat only when the state or the pressure really changed.

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
