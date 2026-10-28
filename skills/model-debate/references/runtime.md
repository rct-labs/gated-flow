# Native adapters and proportional validation

## Resolve once, verify before accepting results

Inspect native executable/version, supported flags and non-secret effective
configuration without inference. Resolve model selectors from provider metadata;
model self-identification is not evidence. Pin selector and supported effort on
each invocation. Record primary serving identity separately from auxiliary usage,
including family-only or unknown reproducibility. Unknown identities cannot count
toward distinct-model quorum; conflicts quarantine the affected answer.

Exclude paused providers before discovery or quota reads. Do not install, change
global defaults, copy credentials merely to make isolation work, or switch to paid
API routes. Existing authenticated subscription transport may remain available.

## Cheap checks first; no default ping

Version/hash changes trigger **local compatibility inspection**, not an automatic
inference-only probe. Compare supported arguments, effective tool/permission
configuration, metadata schema and launch policy. A different version does not
make an old live probe evidence for the new binary: record that limitation.
Unchanged fingerprints may reuse previous runtime evidence with its date; no
calendar-only expiry requires a paid inference. Quota freshness is separate.

Prefer tools-off inline review. When local configuration and native handshake
show the actual callable tool set is empty, the first substantive response can
also validate model identity, complete multiline input delivery and the empty
runtime tool set. Include a tiny input control if delivery is uncertain. Save
raw runtime evidence; do not trust a model saying it has no tools. Reject the
answer if tools unexpectedly appear, identity conflicts, required input is lost,
or the response is incomplete. Verify the callable surface **before submitting
sensitive inputs**; if the adapter only reports it later, first use a nonsensitive
synthetic fixture or an already enforced boundary. Do not expose live private
peer material to an uncertain policy just to save a call.

For tool-enabled seats, enforce permitted roots using actual supported access
controls or an existing sandbox. Test affected read/write/traversal/session/log
routes with harmless fixtures when enforcement is new, changed or uncertain.
These checks may share the substantive call only if the boundary is independently
enforced before access and violations cannot disclose peer artifacts. Otherwise
use one narrow standalone capability probe for that affected adapter, not three
provider-wide smoke tests. Count it in the shared reserve before launch. A cwd,
read-only flag, prompt refusal or missing canary is not proof of read isolation.
If isolation cannot be enforced, block the affected route; soft isolation needs
explicit authorization. Never claim a local flag inspection is a live access test.

## Adapter pitfalls

| Adapter | Inspect and constrain |
|---|---|
| Codex | Native app-server/exec metadata, ephemeral sessions, model/effort, tools, MCP, plugins, hooks, memory and environment discovery; read-only is not a read boundary. |
| Kimi | Native subscription route, effective custom agent tools/subagents, skill discovery, model metadata and output mode. Never infer paid fallback authorization. |
| Grok | Native initialization tool set, web/subagent controls, model/effort and primary vs auxiliary model usage. A tool allowlist alone may leave other tools exposed. |

Launch outside the project to avoid auto-loading unrelated project instructions;
include applicable constraints in the packet. Keep peer outputs, archive paths
and session stores inaccessible to tools. Never hand a reviewer the host history.
Use native executables and argument arrays; Windows shell shims can truncate
multiline prompts. Pass prompt by file/stdin through supported interfaces.

## Research

No standalone web smoke for a local contract review. When a seat researches an
external fact, retain its actual retrieval URL/content/events; a plausible URL
or answer is not proof. A seat without working web can receive the same host-
verified factual packet, explicitly marked second-hand. Do not claim web-grounded
verification without actual retrieval. Explicit `--no-web` disables research
routes, not authenticated inference, and applies to host verification as well.

## Launch and completion

Use fresh independent sessions, immutable prompts, explicit cwd and model/effort.
One task-local launcher may run different subscription pools concurrently; reserve
all starts before launching, with whole-run admission as described in quota.md.
Keep windows hidden. Capture stdout/events and stderr separately, final answer,
prompt hash, model/config fingerprint, PID, start/end, OS exit and terminal status.
Zero exit or a file's existence is insufficient: require valid nonempty output,
identity and boundary evidence. Failed/partial output is never a completed vote.

Bound each attempt with a deadline. Kill only its owned process tree on timeout;
reconcile process identity before retry to avoid duplicate still-running calls.
Retain failed attempts. Batch status checks rather than repeated per-seat polling.
No inference retry for explicit exhaustion; unknown failures require diagnosis,
not an inferred provider switch. See quota.md for authorized recovery.
