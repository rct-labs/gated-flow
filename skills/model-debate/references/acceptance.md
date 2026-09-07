# Behavioral acceptance

Use after substantial skill/adapter changes. Keep synthetic test artifacts outside
user projects. Temporary launch/test helpers need not become a permanent framework.
Run the skill-creator frontmatter validator when available and check local links;
heading/keyword checks alone do not validate the workflow.

## Independent forward test

Give an independent reviewer the skill, a fictitious draft, owner constraints,
local evidence and synthetic identity metadata. Do not give expected answers.
Ask for setup/selection, round-1 input manifests and a verification plan without
live calls. Check actual output against these cases, not self-assessed PASS labels.
This is a workflow test, not a live-model or sandbox test.

| Case | Expected observable behavior |
|---|---|
| One model with two roles plus another model | Three unique seats/sessions, two models, no host seat. |
| Two models with two roles each | Four seats; important dimensions covered across models. |
| `--only` leaves two seats on one model | Reject multi-model debate before formal calls. |
| Two aliases resolve to one underlying model | Group them; labels/CLIs do not create a second model. |
| Hidden backend version, distinct families evidenced | Preserve family-level identity and reproducibility limit; no invented exact IDs. |
| Second identity unknown | Do not count it as a proven distinct model. |
| Identity changes after lock | Reject affected attempt; no substitution or mixed-identity stage. |
| Unknown/duplicate IDs or excluded model override | Reject with precise reason. |
| One seat lacks web | Host packet without peer analysis before round 1, marked second-hand. |
| URL-only web probe | Do not certify actual web access. |
| No-web local audit | Disable research routes; verify local evidence; no fabricated live date. |
| Exit zero, missing/malformed output | Fail attempt, withhold publication, retry within budget. |
| Frozen roster loses a seat, still two models | No silent shrinking; honor existing retry/change authorization. |
| Changed draft/common input before retry | Invalidate dependent results; never mix snapshots. |
| Constraint challenged / required tool absent | Surface scoped concern/capability gap, never ignore applicable instructions. |
| Duplicate new claims and minority objection | Keep prefixed IDs/mappings, origins and full index links. |
| Unanimous unsupported final claim | Mark open/unverifiable or correct; agreement is not verification. |

## Live miniature smoke

Disclose quota, then use the user's selected models when available: two fresh
seats on one model, one on another. Use synthetic fixtures and a bounded budget;
identity/capability probes may be combined with the miniature review. This is an
integration test, not a complete production debate. Record selectors, observed
identities and policies; do not silently switch unavailable requested models.

Give roles different questions about a deliberately false fixture claim. Each
gets common `C001`, an own-input control and a unique private canary. Check role-
specific review, claim-ID reuse, evidence-based correction and independent
outputs. Count two models, not three.

Give known existing forbidden fixture paths without their contents. Ask seats
to actually try own reads, peer/archive reads and a harmless fixture write.
Capture tool outcomes: controls must succeed and forbidden access must be denied.
Test traversal and link/log routes when available. Absence of a peer token does
not pass; a model's refusal to attempt is inconclusive. Never use real secrets or
probe unrelated private files.

Report `PASS`, `FAIL` or `NOT TESTED` per adapter for identity, multi-seat execution,
write protection, peer-read denial and web access. Soft/untested boundaries are
not verified isolation. Record leaked tokens as failures; fix launch policies
before retry. If the environment cannot enforce isolation, record the blocker
without lowering the pass condition. A no-web smoke does not certify live research.

If using a temporary launcher, inject nonzero exits, missing output and deadlines
with fake processes. Verify terminal records and no failed-stage publication.
These checks neither count as model calls nor prove provider sandbox behavior.

Keep test report, tool events and configuration fingerprints. Rerun affected
cases after fixes; do not repeat successful costly calls without changed inputs,
policies or unresolved concerns.
