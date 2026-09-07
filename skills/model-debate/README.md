# model-debate — Requirements

Requirements for auditing this skill. Source: `skills/model-debate` in this
repository; installed CLI copies are distributions. The skill must work on any
project without depending on that source path.

## Boundary

Audit a draft, requirement, RFC, design or idea using multiple models, verify the
evidence, and produce a revised final document. Do not make this skill a mandatory
approval process for unrelated work. Keep it a small Markdown workflow with
focused references, not a framework, service, API client or execution engine.
Temporary launch/test helpers are acceptable for safe process handling. Use
headless subscription CLIs; no API keys or paid search dependency.

## Accepted design

1. Separate provider/CLI transport, model identity and role-bearing seat. Multiple
   seats may share a model; require two provably distinct underlying models for
   multi-model debate. Seats/aliases do not multiply evidence weight.
2. Host defaults to a non-seated organizer and verifier. All debating seats start
   independently, including those that use the host's model.
3. Generate roles per project. Roles specify questions, not forced opinions.
   Preserve common output formats and cross-model coverage. Prefer two seats per
   model and four to six total without imposing hard limits.
4. Explicit user choices win. Otherwise use a supplied model catalog or discovered
   configured CLI defaults. Select before calls, disclose cost and freeze the
   roster. Remove the fixed version roster and legacy participant semantics.
5. Discover capabilities from current CLI help/runtime metadata. Resolve selectors
   at setup and lock identities as far as providers expose them. Record unknown
   versions honestly. Never invent aliases, trust self-identification, silently
   substitute models or mix upgrades within a run. Upgrades normally affect
   configuration or focused adapter guidance, not the debate procedure.
6. Research per seat. Initial research/critiques cannot read peer artifacts or
   peer summaries. Enforce readable scope through tools or existing isolation;
   hidden folders, working directories and write protection alone do not suffice.
   Probe real access and disclose enforcement limits.
7. Missing web capability can use host-created factual evidence without peer
   analysis, labeled second-hand. Only explicit no-web instructions disable live
   research. Preserve local-evidence verification.
8. Number claims before round 1 with draft locations. Prefix new claims by seat;
   preserve merge mappings. Index objections by claim and retain originals,
   minority evidence and source origins.
9. Record owner decisions/exclusions separately. Surface constraint questions
   without reopening decisions automatically. Respect applicable repository
   instructions; read-only review is not a blanket exemption.
10. Two debate rounds, mandatory host verification and visible freeze. Evidence
    decides facts. Unresolved assertions remain assumptions/questions. Existing
    authorization covers in-scope stage checkpoints.
11. Bound calls, concurrency, retries and elapsed time. Record process completion
    separately from outputs. Retry failed seats only with unchanged inputs and
    identities; invalidate results affected by changed inputs.
12. Output follows the draft/user language and user formatting preferences.
    Quotes retain their form subject to higher-priority language/quotation rules.

## Acceptance

[references/acceptance.md](references/acceptance.md) covers multiple seats,
distinct-model selection, identity drift, real access denial, claim traceability,
failure recovery and instruction conflicts. Static validation does not establish
live runtime isolation.
