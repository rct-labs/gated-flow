---
name: model-debate
description: Resolve difficult or materially disputed design decisions with independent models and verified evidence. Use when explicitly requested, a project core-risk gate applies, or a concrete unresolved problem warrants multiple perspectives; not for routine fixes, documentation, or automatic post-implementation review.
---

# Model Debate

Review a decision using independent models and host-verified evidence. This is a
small workflow, not an approval framework for ordinary edits. Honor user scope,
project quorum, excluded providers and existing authorization. Use subscription
CLIs; no implicit paid fallback, upgrades or global configuration changes.

## Decide whether review is needed

Do not start a model review merely because work finished, files changed, a version
changed, or a queue reached a checkpoint. Routine bug fixes with a reproducible
cause and adequate regression tests, documentation, formatting, small reversible
refactors and already-settled decisions normally need direct verification only.
Record a brief skip reason; do not generate a separate review package.

Use independent review when the user explicitly asks, an applicable project hard
gate requires it, or a concrete difficult issue remains after local investigation:
competing consequential designs, repeated unexplained failures, ambiguous core
contracts, or serious security/data-loss risk not covered by deterministic checks.
State the unresolved question and how a review could change the action. One
focused reviewer often suffices for a bounded implementation uncertainty; use
multi-model debate only when differing independent judgments add value. Do not
create a multi-model quorum for an ordinary single-review task. Never recursively
review a review, or route skill maintenance through live debate by default.

## Default: focused, one useful call per model

Use three independent models from different vendors unless the user or project
specifies otherwise; never count aliases or repeated roles as distinct models.
The host organizes and verifies, not an extra seat. Choose complementary roles
with shared coverage of correctness, feasibility and counterevidence. Do not
spawn additional helper/judge/synthesis seats by default.

1. **Prepare once.** Read the actual sources and settle cheaply checkable facts.
   Freeze the question, alternatives, constraints, source hashes and open claims
   in one compact packet. Prefer the relevant functions, contracts and direct
   consumers over complete files, repository histories or previous transcripts.
   Target roughly 4–8k input tokens per seat; this is a sizing target, not a
   truncation limit. Include essential context even if larger; record why.
2. **Admit once per launch batch.** Read [quota.md](references/quota.md) for the
   account guard and [runtime.md](references/runtime.md) for adapter boundaries.
   Budget the whole remaining work, including implementation/review if promised,
   one shared contingency allowance, and any actually necessary standalone probe.
   Do not reserve a retry for every seat automatically. No inference ping by
   default: validate metadata locally and combine remaining capability checks
   with the first substantive call where runtime enforcement permits it.
3. **Launch independent critiques together** across distinct pools after fresh
   admission; serialize shared-pool starts. Each fresh session gets the same
   factual packet plus its own role, no peer output or host conversation. One
   call includes required research and critique. A local contract review needs
   local evidence, not ceremonial web searches. External changing claims need
   live primary sources; a verified host packet is valid with provenance marked
   `research_origin=host-packet`, not independent research.
4. **Verify, then decide.** The host checks load-bearing claims and every blocker
   against primary evidence. Agreement is not proof. Resolve factual objections
   locally and record design tradeoffs, minority concerns and unknowns. Do not
   send verified facts back to models for another acknowledgement.
5. **Follow up only where it changes the decision.** An unresolved material
   dispute may receive one compact delta question to the relevant model within
   the shared reserve. Include both opposing arguments faithfully. Additional
   seats/rounds need a concrete unresolved question and budget; never schedule
   all-to-all rebuttals automatically. If evidence remains insufficient, retain
   the blocker rather than manufacture consensus. No surviving dispute means
   the focused review is complete after round1.
6. **Freeze and deliver.** Keep model/runtime identities, exact prompts, raw
   results, completion records, verification, ruling, dissent and limitations.
   Use [artifacts.md](references/artifacts.md) when assembling records. State
   what was applied; existing implementation authorization remains effective.
   Do not reopen permissions at routine stage boundaries.

## Cost and scheduling discipline

Default reply contract: verdict, material blockers with evidence and minimal
fixes, unresolved questions, and decisive tests. Aim for 600–1000 words, avoid
restating the packet and enumerating already-settled claims. Record actual token
usage when exposed; otherwise unknown, not a fabricated estimate. Include calls,
failures, retries and elapsed time, distinguishing cached input and output usage.

Use one task-local launcher with per-seat completion records, deadlines and
owned-process cleanup. Poll a batch together, typically every30–60seconds; do
useful local verification between polls. No persistent scheduler, task queue,
worktree or new runner framework solely for a short review. Prefer existing
verified adapters. Do not repeatedly rediscover help, parse the same transcript,
or reread unchanged instructions within a run.

When a result needs correction, send only the changed evidence, exact objection
and directly affected context. Preserve valid independent answers for unchanged
inputs; invalidate only dependent results. Never restart successful independent
seats just because another seat failed. Retry only reconciled failures within
existing authorization and limits.

## Explicit debate profile

Use the longer `debate` profile when explicitly requested or when justified by
an unresolved high-impact fork; disclose its extra calls before launch. It adds
independent research if needed and a full cross-examination round. Do not label
a focused review a completed two-round debate. For changes to this skill or an
adapter, use [acceptance.md](references/acceptance.md); offline checks are the
default, live smoke requires a concrete unresolved runtime risk and call budget.
