---
name: model-debate
description: Review a draft, requirement, RFC, or idea through independent role-based seats on multiple subscription CLI models, live research, cross-examination, and evidence verification. Use for multi-model reviews and adversarial design audits; choose roles per project and resolve model identities at runtime.
---

# Model Debate

Output language follows the draft and the user's request; default to English when neither indicates otherwise. Preserve source quotations and multilingual input when needed.

Turn a draft into an evidence-checked final document. This is a Markdown skill,
not an orchestration framework. Use installed headless subscription CLIs; do not
switch to paid APIs or install infrastructure implicitly.

Before any inference/probe/retry, read [quota.md](references/quota.md) and run
its fresh account/whole-run budget check. Apply owner exclusions before calls;
record authorized backups and the project quorum. Read
[artifacts.md](references/artifacts.md) at setup for roster, claim IDs,
workspace and prompts. Read [runtime.md](references/runtime.md) before calls for
model discovery, permissions, isolation and process handling. When changing this
skill or a CLI adapter, use [acceptance.md](references/acceptance.md).

## Provider, model, seat

- A **provider/adapter** identifies the serving provider, CLI transport and
  subscription pool. A CLI name is neither a model identity nor evidence of a
  separate provider. Several models can share a CLI or subscription.
- A **model** is a requested selector resolved from current CLI/provider
  metadata. Versions belong in run artifacts or optional local configuration,
  never in this core workflow. Do not invent universal `latest` aliases.
- A **seat** has a stable run-local `seat_id`, `model_ref` and project-specific
  role. One model may fill several seats, each in a fresh independent session.
  Key files by seat ID, not model names or changing role titles.
- The **host defaults to a non-seated facilitator**: organize, verify, synthesize.
  It writes no seat critique and supplies no extra vote even when its model is
  selected. Only an explicit user request changes this default; disclose the
  context asymmetry if the already-informed host also debates.

Require at least **two provably distinct underlying models** after selection and
preflight. Multiple roles, aliases, sessions, CLI binaries or routing providers
for one model do not satisfy this rule. Different models provide diversity, not
statistical independence. Group corroboration by underlying model and source
origin; truth never follows seat count, majority vote or model prestige.

## Setup and selection

Infer the target, constraints and requested models from the conversation. User
choices override optional configuration. Without an explicit roster, use all
enabled entries in a user-supplied model catalog; otherwise discover installed
adapters' configured defaults and deduplicate actual models. Do not call every
model an account can serve. Disclose the proposed roster and budget before calls.
If two distinct models cannot be established, report the missing identity or
capability; never fabricate a second model.

Generate complementary roles from project goals, decisions and risks. Cover
benefits, feasibility, risk and counterevidence as relevant; do not mandate fixed
professions or opposition. Roles specify questions, not conclusions. All seats
use the same output format. Distribute important review dimensions across models
too, so a difference of opinion is not completely confounded with role assignment.

Prefer two seats per model and four to six total for substantial reviews; these
are defaults, not hard limits. Small reviews may use one seat per model. Honor
explicit counts and enabled catalog models; disclose excess size instead of
silently trimming them. Freeze roles in `roster.md` before research. Ordinary role
generation needs no separate approval when the run is already authorized.

Natural language is sufficient. Optional skill-level arguments (not CLI flags)
have these meanings, with no legacy participant-ID semantics:

| Argument | Meaning |
|---|---|
| `--roster PATH` | Load model requests and seat assignments. |
| `--roles PATH` | Use supplied role briefs when generating seats. |
| `--models ref=selector,...` | Override selectors for known model references. |
| `--only seat-a,seat-b` | Select exactly these seats from the proposed roster. |
| `--exclude seat-c` | Remove these seats from the proposed roster. |
| `--no-web` | Disable research and all later web access for this run. |

Reject unknown IDs, duplicate mappings/seat IDs, overrides for unselected model
refs and contradictory `--only`/`--exclude` use. Multiple seats may intentionally
share a model ref. Select before calls, then recheck the distinct-model minimum
against runtime identity. Group aliases resolving to the same underlying model.

## Procedure

Setup → per-seat research → independent critique → cross-examination → host
verification → freeze. Research is not a debate round. There are two debate
rounds; extra rounds require authorization outside the original budget.

At stage boundaries report material findings and let the user intervene. Existing
authorization to complete the run covers these checkpoints: report and continue
without repeatedly asking. Changes to the authorized roster, budget, constraints
or destination need new authorization unless already covered. Silence never
grants it; routine execution and corrections within scope need no new gate.

### 0. Prepare and preflight

1. Copy the input into `draft.md`, or structure the spoken idea. Record its
   revision/hash and project snapshot identity. Extract accepted owner decisions
   and explicit exclusions into `constraints.md`, with provenance.
2. Before first-round analysis, extract atomic factual claims into `claims.md`
   as `C001`, `C002`, etc., linked to draft locations. Distinguish facts,
   assumptions, proposals and constraints. The inventory is not an endorsement
   or a restriction on what seats can challenge or add.
3. Define shared and role-specific research questions derived from the draft,
   including current external dependencies. Prepare identical common inputs
   and each seat's role file; avoid arbitrary question counts.
4. Disclose seats, model requests, shared subscriptions and call budget: normally
   three calls per seat (research and two rounds), two with `--no-web`, plus
   model preflights, capability probes and a stated retry allowance. Set total
   calls, concurrency by subscription and wall-clock deadlines per seat.
5. Run the quota guard for the complete remaining budget, including inference
   probes and retries. Missing allowance/estimate or insufficient project quorum
   blocks the affected launch. Resolve/lock models and probe actual read/write
   boundaries and web capability
   per `runtime.md`. Smoke tests consume quota too; existing authorization for
   this disclosed run covers them. Freeze `roster.md` after preflight. Preserve
   requests/failures; never silently replace a requested model with a default.

### 1. Research independently

Research is on by default and runs **per seat**, guided by its role. Each gets
only common inputs and its role. Use fresh sessions; do not fork the host's
conversation or resume another seat. Research outputs, prompts and logs remain
private until the independent-critique barrier closes. The host may report
issues to the user without exposing peer summaries to seats. Do not distribute
merged research before round 1 completes.

Use primary sources, dates/access times, short load-bearing quotes within
applicable quotation limits, and claim/question IDs. Prefer authoritative current
editions over mere recency; undated sources have unknown freshness. Label model
memory `MEMORY`, record source contradictions and unsuccessful searches as gaps.

A seat without verified web access receives a **host-prepared factual packet**
without peer analysis. Freeze the same packet for seats needing it and record
`research_origin=host-packet`: second-hand research, not independent searching.
Working web seats still research. The host must actually open packet sources.
If no live access exists anywhere, surface the failure and obtain an explicit
no-web decision before claiming a web-grounded run.

With `--no-web`, skip this stage and enforce no web access at tool/transport
boundaries for all later stages, including host verification. Local file evidence
remains valid; do not describe a local audit as based only on model memory.

### 2. Independent critique

Each seat receives the frozen draft, claims, constraints, own role, permitted
project evidence and **only its own research** (or declared host packet). It
checks applicable claims, discovers omissions and may research new questions
when web is enabled. Use the common output format in `artifacts.md`.

No peer outputs or merged findings are readable through any enabled tool.
Isolation includes prompts, session stores, logs, caches and links, not merely
`round-1/`. All seats remain read-only reviewers. Keep owner constraints in force;
reasoned objections to them go in `CONSTRAINT QUESTIONS` for the host to surface,
not automatic scope changes. Repository instructions retain their applicable
scope and priority; read-only review does not make them irrelevant.

Close the barrier only after every selected seat has a valid completed result
or an authorized roster revision handles failures. Publish research and round-1
results as a logically complete immutable stage. Then merge sources into
`research/brief.md`, retaining ID mappings and original provenance.

### 3. Cross-examine by claim

Build `objections.md` indexed by claim/objection ID: concern, evidence and
counterevidence, originating seat/model, and links to full passages. Include
minority objections and source conflicts. Preserve the force of objections;
the index must not substitute the host's preferred interpretation. All completed
research and round-1 originals become accessible now.

Seats read the index and open originals for disputed or compressed points. Reply
`CONCEDE`, `REBUT` or `DEFER` to objections, citing evidence for rebuttals. For large
rosters assign coverage so every objection receives scrutiny from a different
model where available; every seat may raise an omitted issue. Avoid all-to-all
copying of full texts as a requirement.

Require blind-spot lists: accepted claims without evidence, repeated `MEMORY`
claims, and repeated citations to one source misrepresented as independent.
Keep round-2 results private until its completion barrier. Report disputes and
constraint questions. A new design fork is not permission for an unbudgeted round.

### 4. Verify evidence (host, mandatory)

Check factual claims from the draft and every proposed final change, including
inventory omissions. Prioritize disputed, unverified, stale and uncited-consensus
claims. Inspect sources yourself: model citations are leads, not evidence. Tie
code evidence to the recorded snapshot and actually inspect line references.

For external facts, open cited URLs, confirm quotes in context, check dates and
preserve contrary evidence. Inaccessible/paywalled/unrelated pages do not verify
a claim: find accessible evidence or mark it `UNVERIFIABLE`. Under `--no-web`, do
not fetch URLs; local copies can support bounded dated claims with that limitation.

Write `verification.md` with claim ID, assertion/revision, provenance, verdict,
evidence, checked date and action. Factual verdicts: `CONFIRMED`, `REFUTED`,
`UNVERIFIABLE`. Preferences and designs are reasoned decisions, not experimentally
confirmed facts. Preserve merge mappings for new claim IDs.

### 5. Freeze and deliver

Produce `final.md` with corrected claims, explicit assumptions/open questions,
verified citations and a research-current-as-of date (or explicit no-web statement).
Correct/remove refuted assertions. A completed review may retain open questions;
never call an unverified assertion established or a business requirement accepted
when its owner has not accepted it.

Record decisions in `decision-log.md`: changes, reasons, claim/objection IDs,
originating seats/models, accepted/rejected fixes and unresolved constraints.
Include identity limitations, shared research, isolation assurance, failed/dropped
seats, retries/calls and evidence gaps. Preserve every attempt.

Report seats versus distinct models, rounds, checked sources, verdict counts,
material changes, limitations and artifact paths in the user's output style.
Applying the final back to its original destination needs authorization; an
existing explicit instruction to revise that destination already satisfies it.
Do not commit, publish or change project configuration merely because a debate ran.
