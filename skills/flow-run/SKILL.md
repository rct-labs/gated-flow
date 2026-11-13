---
name: flow-run
description: >
  The default entry for this workflow: inspect and resume already-authorized
  project work as the host CLI, automatically choose a suitable available
  implementation worker (using the owner machine model policy and trustworthy
  capacity and suitability evidence, overridden by an explicit later
  natural-language worker choice), write an admission-ready TASK_QUEUE, then
  unattended-run it. It loads $flow and $run for their steps, so the user never
  has to pick between them. Known worker ids: codex, agy (custom
  worker_cmds.agy or an existing verified alias), claude, kimi, grok, pi. Use when the user types $flow-run
  or /flow-run, or says check progress, continue, keep going, keep executing,
  run unattended, continue until blocked, flow run, or continue and finish;
  also responds to equivalent requests in other languages. Analysis-only,
  plan-only, standalone debate and paused-business requests do not authorize
  execution.
---

# $flow-run — host router

For separately installed skills, `flow home` locates repository documents:
resolve shared rules at `<home>/docs/autonomy.md` and other repository links
against that home. Within-skill references stay relative to this skill.

Inspect and resume only already-authorized work. A status question alone is
read-only; analysis-only, plan-only, standalone debate and paused-business
requests never launch execution. Follow current user scope and limits.

Read [shared workflow rules](../../docs/autonomy.md), the authority for role
policy, adaptive risk, approvals, review, repair, cumulative limits and evidence.
Load each operational reference only when that phase is needed:

1. [Inspect and decide](references/inspect.md): resume facts, select an eligible
   worker from real available evidence, choose the next milestone.
2. [Queue](references/queue.md): use [flow](../flow/SKILL.md) planning only for missing work;
   preserve an existing admitted queue when the user asks to run it as written.
3. [Configure and launch](references/launch.md): reconcile a stopped package,
   then use [run-queue](../run-queue/SKILL.md) for engine-owned execution.
4. [Outcome](references/outcome.md): interpret review/acceptance, repair within
   existing authority and limits, checkpoint and report.

The host decides; `gate.py run` owns the loop, task verdicts and settlement.
Workers receive only the bounded task packet, optional verified handoff and
embedded acceptance, never host history or resume/recall output. No recursive
host workflow inside a task worker. Host never marks tasks DONE or bypasses the
hook. Queue exhaustion is a batch boundary, not evidence of product delivery.

Narrate task completion, blockers and review/acceptance changes. Use the journal
cursor and saved reports, with no repeated transcript scans or empty-poll updates.
For efficiency analysis use `flow metrics --last 5 --repo <project>`; missing
usage is unknown, and call counts alone do not establish token savings.
