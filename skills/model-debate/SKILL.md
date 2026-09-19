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

Prefer models from **different vendors and account pools** when the roster
allows it. Two models of one vendor meet the minimum but share training lineage
and usually one allowance: disclose that as reduced diversity in `roster.md` and
the final report, and never present their agreement as cross-vendor support.

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

Default to three seats on three models from different vendors, one seat per
model. Use two seats per model and four to six total for a `debate` or when the
review dimensions do not fit three roles; these are defaults, not hard limits. Honor
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

Choose the profile at setup and record it in `roster.md`:

| Profile | Use for | Shape |
|---|---|---|
| `focused` (default) | most reviews | host pre-verification → round 1 with research inside the same call → host verification → cross-examination of surviving disputes only → freeze |
| `debate` | an irreversible, high-impact fork, or an explicit request | host pre-verification → per-seat research → round 1 → full round 2 → host verification → freeze |

Spend calls where they change the outcome. Past runs show host verification
overturns about a third of claims while a full second round rarely moves a
conclusion, so `focused` checks facts first and sends to cross-examination only
what is still in dispute afterwards. The numbered stages below describe
`debate`; each states what `focused` does differently. Research is not a debate
round. Rounds beyond the profile need authorization outside the original budget.

The profile is chosen and disclosed before calls. An instruction to hurry,
finish or run unattended (including from `$flow-run`) removes neither host
verification nor the surviving-dispute round from `focused`, nor round 2 from a
`debate`. If a required stage cannot run, say so, keep the run open or change
profile with the user's agreement, and name the profile that actually ran in
every report. Host verification is mandatory in both profiles.

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
3. Pre-verify: settle every claim the host can check cheaply against a primary
   artifact (run the query, open the file, read the actual error, inspect the
   recorded snapshot) before any seat call. Mark each `SETTLED-TRUE` or
   `SETTLED-FALSE` in `claims.md` with its evidence and checked date. Seats
   receive these as established facts and spend their effort on judgment; a
   seat may still challenge one, but only with contrary evidence. Do not
   pre-verify preferences, designs or anything needing interpretation.
4. Define shared and role-specific research questions derived from the draft,
   including current external dependencies. Prepare identical common inputs
   and each seat's role file; avoid arbitrary question counts.
5. Disclose seats, model requests, shared subscriptions and call budget. In
   `focused`: one call per seat plus a stated reserve for surviving disputes
   (normally up to one further call for each seat). In `debate`: three calls per
   seat (research and two rounds), two with `--no-web`. Add model preflights,
   capability probes not covered by a valid cached preflight (`runtime.md`) and
   a stated retry allowance. Set total calls, concurrency by subscription and
   wall-clock deadlines per seat.
6. Run the quota guard for the complete remaining budget, including inference
   probes and retries. Missing allowance/estimate or insufficient project quorum
   blocks the affected launch. Resolve/lock models and probe actual read/write
   boundaries and web capability
   per `runtime.md`. Smoke tests consume quota too; existing authorization for
   this disclosed run covers them. Freeze `roster.md` after preflight. Preserve
   requests/failures; never silently replace a requested model with a default.

### 1. Research independently

In `focused` there is no separate research call: each seat researches inside
its round-1 session under the rules below and returns sources and critique in
one result. The barrier, privacy and host-packet rules apply unchanged.

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

In `focused` this stage runs **after** stage 4 and only on surviving disputes.
A dispute survives when seats on different models hold incompatible positions
on a point that changes the final document, and host verification could not
settle it: a preference, a design trade-off, or a fact marked `UNVERIFIABLE`.
Objections that verification settled, that no seat opposes, or that do not
change the outcome are recorded in `objections.md` as closed with the reason
and are not sent out. Send each surviving dispute to the seats that hold the
positions plus one seat on a different model where available; other seats are
not called. If nothing survives, record `no surviving disputes` and run no
second round: that is a complete `focused` run, not a shortened one. Verify any
new factual claim raised in these replies before it reaches `final.md`. The
rules below apply to whatever is sent.

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

In `focused` this runs as soon as the round-1 barrier closes, before any
cross-examination, and its verdicts decide which disputes survive.

Check factual claims from the draft and every proposed final change, including
inventory omissions. Prioritize disputed, unverified, stale and uncited-consensus
claims. Treat unanimity as a risk signal, not a shortcut: when every seat agrees
on a load-bearing premise, parameter or error signature, verify it first and
against the primary artifact (run the query, open the file, read the actual
error), because shared training produces shared invention. Output from a failed
or truncated seat attempt is not evidence. Inspect sources yourself: model citations are leads, not evidence. Tie
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

Before reporting, check the run directory holds `roster.md`, the quota receipts,
`verification.md`, `decision-log.md` and `final.md`. A run missing any of them
is reported as incomplete, naming the gap. A run that stops early still writes
`decision-log.md` with the stage reached and the reason; a run directory with
no decision log is a defect. Record total calls, retries and wall-clock per
stage in the decision log.

Report the profile that ran, seats versus distinct models and vendors, whether
the host also held a seat or authored the draft under review, rounds,
checked sources, verdict counts, material changes, limitations and artifact
paths in the user's output style. State on its own line whether `final.md` has
been applied to its destination; when it has not, say what would apply it.
Applying the final back to its original destination needs authorization; an
existing explicit instruction to revise that destination already satisfies it.
Do not commit, publish or change project configuration merely because a debate ran.
