---
name: model-debate
description: Resolve difficult or materially disputed design decisions with one independent blind model round and host primary-evidence verification/synthesis by default. Standalone invocation is discussion-only; multi-round adversarial debate runs only when explicitly requested or required by an identified project hard gate. Not for routine fixes, documentation, or automatic post-implementation review.
---

# Model Debate

Review a decision using independent models and host-verified evidence. This is a
bounded workflow, not an approval framework for ordinary edits. Honor user scope,
project quorum, excluded providers and existing authorization. Use existing
authenticated subscription CLIs only; no implicit paid fallback, inference quota
sweeps, upgrades or global configuration changes.

Standalone `model-debate` is discussion-only: it critiques, verifies primary
evidence, reports the synthesis or unresolved disagreement, and makes no code
edits or task dispatches. When `flow-run` invokes `model-debate` on an in-scope
fork for already-authorized project work, `flow-run` resumes that work after
resolution without reopening routine permissions—while never overriding core
project quorum, per-task approval rules, or an unresolved human-level blocker.
Keep inter-model packets, seat prompts, claim IDs and run records in English;
deliver user-facing reports in the user's language (for example Chinese when the
user communicates in Chinese).

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

## Default: one independent round, then host verification (`focused`)

Default `model-debate` is **one independent blind round** followed by **host
primary-evidence verification and synthesis** (`profile: "focused"`). There are
no automatic delta questions, cross-rebuttals, or judge/helper/synthesis model
calls.

- **Default ordinary team**: Codex, Gemini Carbon (native `agy`) and Claude
  Fable 5.2 (`claude`) when available, pinned to `medium` effort unless the user
  or an explicit project rule overrides it.
- **Host vs blind seats**: an already-present host session may contribute its own
  labeled host analysis alongside external calls and owns verification/synthesis,
  **but is NOT an independent blind seat**. Do not make 3 fresh external vendor
  calls mandatory for an ordinary bounded review when 1–2 blind external peer
  calls from the available team suffice.
- **Formal project 3-independent-seat gate**: when an applicable project hard
  gate requires 3 independent seats (`project_min_models: 3`), three fresh blind
  sessions on provably distinct model families among the seats are mandatory.
  A fresh isolated seat may use the host's model family if project policy permits;
  the already-present host session, aliases and repeated roles never add a seat.

1. **Prepare once.** Read the actual sources and settle cheaply checkable facts.
   Freeze the question, alternatives, constraints, source hashes and open claims
   in one compact English packet. Prefer the relevant functions, contracts and
   direct consumers over complete files, repository histories or previous
   transcripts. Target roughly 4–8k input tokens per seat as a sizing target, not
   a truncation limit; include essential context even if larger and record why.
2. **Admit once per launch batch.** Read [quota.md](references/quota.md) for
   quota handling and [runtime.md](references/runtime.md) for adapter boundaries.
   When a formal project quota gate applies, require a fresh `READY` receipt from
   `quota_guard.py`. For ordinary bounded reviews on existing subscriptions
   without a mandated `READY` gate, check available read-only metadata without
   inference sweeps, treat `UNKNOWN` as unmeasured (never `0`, exhausted, a
   numeric rank across units, or a fabricated `READY`), and follow documented
   project preference within explicit call caps. Budget the single blind round
   (plus promised implementation/review inside `flow-run`), with no automatic
   follow-up reserve or default inference ping.
3. **Launch one independent blind round** across distinct pools; serialize
   shared-pool starts. Each fresh session receives the identical factual packet
   plus its own role, with no peer output or host conversation. Pin verified
   selector and `medium` effort (unless overridden). For Gemini Carbon via native
   `agy`, ensure `PATH` resolves `agy` to the verified executable (including any
   existing TLS wrapper) before launch and use flags verified from `agy --help`;
   no built-in repo adapter is promised, and discussion-only success never
   certifies coding capability. External changing claims need live primary
   sources; a verified host packet is marked `research_origin=host-packet`, not
   independent research.
4. **Verify and synthesize locally.** The host checks load-bearing claims and
   every blocker against primary evidence. Agreement is not proof. Close factual
   objections locally and never send verified facts back to models for another
   acknowledgement. Do not issue automatic delta questions, rebuttals or helper
   inference calls. If material design disagreement survives primary-evidence
   verification, **report the disagreement as unresolved** with each side's
   argument, verified facts and the open decision rather than manufacturing
   consensus.
5. **Freeze and deliver.** Keep model/runtime identities, quota freshness or
   `unknown` rationale, exact prompts, raw results, completion records,
   verification, synthesis and unresolved dissent ([artifacts.md](references/artifacts.md),
   and persist routing/quota notes in `CONTEXT.md` when invoked from `flow-run`).
   Deliver the report in the user's language, stating applied/not-applied status:
   standalone `model-debate` may save its discussion records but does not edit
   product implementation or dispatch tasks, while `flow-run`
   continues already-authorized work once an in-scope fork resolves without
   violating core quorum or approval rules.

## Cost and scheduling discipline

Default reply contract: verdict, material blockers with evidence and minimal
fixes, unresolved questions, and decisive tests. Aim for 600–1000 words, avoid
restating the packet and enumerating already-settled claims. Record actual token
usage when exposed; otherwise `unknown`, not a fabricated estimate. Include calls,
failures, retries and elapsed time, distinguishing cached input and output usage.

Use one task-local launcher with per-seat completion records, deadlines and
owned-process cleanup. Poll a batch together, typically every 30–60 seconds; do
useful local verification between polls. No persistent scheduler, task queue,
worktree or new runner framework solely for a short review. Prefer existing
verified adapters. Do not repeatedly rediscover help, parse the same transcript,
or reread unchanged instructions within a run.

During authorized failure recovery or an explicitly enabled multi-round run, send
only changed evidence, exact objections and directly affected context. Preserve
valid independent answers for unchanged inputs; invalidate only dependent results.
Never restart successful independent seats just because another seat failed.
Retry only reconciled failures within existing authorization and limits.

## Explicit or hard-gate multi-round debate (`debate`)

Use multi-round / adversarial cross-examination (`profile: "debate"`, or any
post-round-1 follow-up call) **only** when explicitly requested by the user OR
when an actual applicable project hard gate requires it—and **identify that exact
project requirement** in `roster.md` / `decision-log.md` before launching extra
calls. By default an explicit adversarial request permits the initial independent round
plus one focused cross-examination round; more rounds need an explicit request or
a named hard requirement. Report remaining disputes rather than looping.
Disclose extra calls before launch, and never label a single-round review
a completed two-round debate. For changes to this skill or an adapter, use
[acceptance.md](references/acceptance.md); offline checks are the default, and
live smoke requires a concrete unresolved runtime risk and call budget.
