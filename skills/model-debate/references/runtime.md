# CLI adaptation, isolation and execution

Read before calls. These are capability requirements and discovery hints, not
promises about installed builds. Exact versions, availability and errors belong
in run records.

Read [quota.md](quota.md) before capability probes and model calls. Its
`quota_guard.py` performs read-only native allowance checks and whole-remaining-
run budget admission. Exclusions apply before probes; an account limit is not a
reason to spend another inference on the same exhausted account.

## Discover, resolve, lock

1. Read current CLI/subcommand help, version and non-secret effective model
   configuration. Prefer supported model-list commands/provider metadata; caches
   are leads and can be stale. Never dump credentials or whole config/env files.
2. Resolve selectors through the adapter. Skill-level `default` means inspect
   configuration, not a CLI model ID. Use `latest` only when that exact selector
   is documented. Never guess a version from a marketing name.
3. Preflight each distinct model/adapter configuration, not merely each binary.
   Capture runtime events. A model field merely echoing the request is not
   resolution evidence; model prose identifying itself proves nothing.
   Compare primary response-model fields with the request as well as usage
   records. If an exact requested ID conflicts with the reported serving ID,
   fail preflight pending an authoritative alias mapping or corrected runtime;
   do not relabel the requested seat to make the mismatch disappear. Track
   primary and auxiliary usage separately, preserving ambiguous reports.
4. Record selector, argument array, executable/version, observed serving
   provider/family/model, metadata origin, assurance (`exact`, `family-only`,
   `unknown`) and web/isolation probes. Redact auth material. Each seat links to
   its applicable preflight.
5. Pin exact IDs on every call when supported. If only a family/alias is exposed,
   lock it and record missing snapshot reproducibility. Two distinct underlying
   models still require evidence; unknown identities cannot supply the second.
   Two evidenced different families may qualify without exact backend snapshots.
6. Compare later runtime identity against the lock. A conflicting model or
   CLI/config change invalidates the attempt; pause affected seats. Never
   silently fall back or mix old/new identities in a completed stage. Exposed
   metadata cannot detect hidden provider changes; disclose that limit.

Auxiliary suggestions, automatic subagents and helper models are not seats. For
an exact model-only call set, disable auxiliary activity using supported process-
local settings and verify usage metadata. Report inability before formal calls.
Do not modify global defaults, upgrade CLIs or change subscriptions implicitly.

## Adapter discovery hints

Freeze reasoning effort alongside each seat's model selector before the first
probe. Include backups, synthesis and review calls; use explicit supported CLI
flags or native-agent fields every time. Record requested and observed effort
separately, using unknown for missing runtime metadata. A host session/global
configuration change does not amend the seat lock. Only an explicit instruction
about future execution permits a recorded amendment and fresh affected contexts.
If the selected model has no applicable effort control, record not_applicable
and the capability evidence; never invent or silently ignore an effort flag.

| Adapter | Inspect locally | Establish before calls |
|---|---|---|
| Claude Code | `claude --help`, `claude --version` | Print mode, model selector, JSON/events, exact tool set, restricted file roots, nonpersistent sessions, MCP/permissions. |
| Codex | `codex --help`, `codex exec --help`, `codex --version` | Exec mode, model pin, final-message capture/events, ephemeral sessions, sandbox/permission profile, web configuration. |
| Kimi | `kimi --help`, model/provider subcommand help | Prompt mode, qualified model IDs, diagnostics separation, actual read-only and web controls. |
| Grok | `grok --help`, `grok --version` | One-shot mode, model pin, sandbox/web/subagent controls, final-message extraction. |

Build argument arrays from **this installation's** help and capability tests.
Record commands in preflight, not version tables in the core skill. New adapters
fulfill the same requirements without changing the procedure. Flags can change.

Check these non-obvious pitfalls:

- Variadic tool flags may swallow trailing prompts. Use documented positions
  and argument arrays; never construct shell code from user text.
- An auto-allow list may not remove other tools. Inspect the actual tool set,
  including MCP, shell, browser-local-file and subagent routes.
- Codex `read-only` describes write restrictions, not proof of denied reads
  outside the seat root. A working directory is not a read boundary.
- Final-message files may appear only on success. Capture stdout/events and
  stderr separately; missing output on failure is possible.
- Some minimal modes disable subscription auth with customizations. Do not
  choose API-only modes or copy credentials to make a sandbox work.
- Without enforceable write restrictions, use an actually protected snapshot
  or another verified boundary. A prompt saying "do not edit" is insufficient.
- The working directory of an agent CLI is part of the prompt. Launched at a
  repository root it auto-loads that project's instruction files (`AGENTS.md`,
  `CLAUDE.md`, project skills), which can replace the seat's role. Launch from
  the seat's own input directory and hand over project evidence as inputs.
- Windows `.cmd` package-manager shims can cut a prompt at its first newline.
  Resolve the native executable, or pass the prompt by file or stdin, and
  confirm in preflight that a multi-line prompt arrives whole.
- A planning or approval-only permission mode may return narration instead of
  a review, and may auto-activate unrelated skills. Probe that the chosen mode
  yields a complete final answer before using it for a seat.
- A seat started inside the host's own tool shell dies with that shell and
  inherits its tool timeout, which truncates long reviews. Launch seats as
  detached processes and poll their completion records.
- Non-empty stderr is not a failure: some CLIs echo the whole transcript there.
  Judge the attempt by its completion record and the validity of the result.

## Isolation and probes

Default to **verified tool-level isolation**: only seat inputs are readable via
every tool the model can invoke, and reviewed inputs are immutable. Use supported
restricted-root tools or an existing OS/container boundary. Do not install
containers, create accounts or alter broad ACLs implicitly. Tool-level isolation
limits agent actions, not a malicious CLI binary; report the actual mechanism
and tested scope rather than claiming an OS-security guarantee.

Disable/constrain alternate channels: shell/code execution, MCP filesystems,
local browser/file URLs, host-agent/session APIs, hooks, memory and symlink/
junction escapes. Keep peer and archive paths outside readable roots. Inspect
session/log access too. Check auto-loaded project instructions; preserve applicable
rules in input packets if a supported launch mode suppresses discovery. Resolve
requirements by scope/priority; never waive a required unavailable tool in a
prompt. Mark evidence gaps or surface an actual blocker.

Probe through actual enabled seat tools under the same launch policy:

- Successfully read an own-input control; attempt a harmless write to a
  disposable reviewed-input fixture and verify refusal.
- Attempt known existing absolute peer/archive paths, relative traversal and
  any available link/session-log route. Require denial by access enforcement,
  not a missing file or the model declining to try.
- Place unique private canaries in test seats. Retrieving a peer's token fails.
  Its absence in the answer is **not** proof: retain tool-level denied reads
  and the successful control read.

Reuse probes within a run only for identical boundary configuration; test each
differing policy. Across runs, a **cached preflight** may stand in for the
identity, isolation and web probes of an adapter when all of these still match
the cached record: executable path and version, resolved model selector and
effort, argument array, permission/sandbox policy, the non-secret effective
configuration fingerprint, and OS user. Keep the records outside any project
(for example `~/.model-debate/preflight/<fingerprint>.json`) with the probe
evidence and timestamp. A record is valid for 7 days; any fingerprint change, a
failed or surprising seat result, or a user request invalidates it at once and
the probes run again. State in `roster.md` and the report which assurances come
from a cached preflight and its age. The cache never covers quota: run the
quota guard fresh every time. Cached results do not upgrade an assurance level
and cannot supply the evidence for a second distinct model that the original
probe did not establish. If a channel is unrestricted or a read succeeds, do not
label the round independent. Try supported stricter launch modes first. If none
works, report the precise gap. Proceed with explicitly labeled **soft isolation**
only when the user authorizes that limitation; otherwise stop the affected launch.
Existing soft-isolation authorization persists. Never replace requested models
merely because their isolation capability is missing.

## Web and no-web

Probe a benign public fact using an actual search/fetch tool. Retain the event,
URL and retrieved content/title. A plausible answer/URL is insufficient. Without
observed retrieval evidence, mark web unverified and use a declared host packet
before round 1.

With `--no-web`, explicitly disable research tools and indirect network routes
(shell, MCP, browser, subagents). Omitting an enabling flag does not disable an
existing default. Preserve authenticated inference transport: no-web restricts
seat research, not model inference. Report a policy that cannot enforce the mode.

## Launch, complete, retry

Use temporary task-local process handling, not an installed runner. One call per
seat/stage, fresh session, isolated inputs, immutable prompt, explicit model,
known cwd, stdin closed unless deliberately used for the prompt, and separate
final answer/stdout/events/stderr. Prefer argument arrays/direct executables;
Windows wrappers need a supported launcher, not interpolated shell code. Hide
Windows helper windows; do not translate POSIX `&`/`wait` literally to PowerShell.

Parent launcher writes atomic `completion.json` after streams close, including
seat/stage/attempt, process identity, start/end time, terminal status (`SUCCEEDED`,
`FAILED`, `TIMED_OUT`, `CANCELLED`), nullable OS exit code, input hash, model-lock
fingerprint and result path. Use a finally path so failures also get records.
Zero exit still requires a nonempty valid final result, matching model identity
and required claim/objection coverage. Missing output is failed, not success.

Observe completion records plus process status, not result-file size. If the
launcher dies without a record, reconcile its recorded process identity before
retrying; never duplicate a still-running seat. Apply per-seat deadlines to the
task-owned process tree with supported OS mechanisms; retain partial outputs as
failed attempts, and never kill by generic process name. Individual waits stay
short so progress is visible even when a seat runs for minutes.

Distinguish exhausted account quota from a transient rate limit. Use the
quota recovery plan for authorized replacements; recheck quota and shared-pool
budget before every retry. Preserve historical originals, rebuild independent
replacement research/round1, and invalidate ALL round2 results when the model
roster changes. Existing explicit roster-change authority is sufficient; do
not ask again for the same authorized substitution. Unknown failures require
reconciliation/repair, not an inferred model switch.

Set a retry allowance (normally one transient retry per failed seat). Preserve
attempts and retry only failures, counting all probes/retries in the call budget.
Reuse successful outputs only with unchanged draft, claims, constraints, role,
visible evidence and model-lock fingerprints. Round-2 reuse also requires the
same index and full published round-1 snapshot. Changed inputs invalidate all
dependent results, not just the previously failed seat.

Availability filtering is allowed **before freezing an automatically discovered
roster**, with exclusions disclosed. Explicit requested seats/models cannot be
dropped automatically. After freeze, failure blocks stage completion until a
budgeted repair or authorized roster revision resolves it, even if two models
remain. Single-model review needs explicit separate authorization and is never
presented as a multi-model debate.
