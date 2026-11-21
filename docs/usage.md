# flow / run — user manual

Bare `$flow-run` resumes authorized work and selects a suitable implementation
worker using trustworthy capacity evidence and project preference. No parameters
are needed; natural language such as "use Gemini this time" overrides routing
within policy-allowed roles. Unknown allowance is recorded honestly, not ranked
numerically. Existing verified native CLI wrappers are preserved. Coding workers
use the coding pool configured in your central
`~/.config/mini-model-routing/policy.json` (Sol / Hydrogen, with Claude fallback
only when primary coding routes are unavailable); design and required reviews
use the separate design pool and remain independent of the implementer.

`$model-debate` alone discusses without implementing: one independent blind
round, then host evidence verification and synthesis. Unresolved disagreement is
reported without automatic rebuttal calls. Adversarial rounds require an
explicit request or an identified project hard requirement. Model packets use
English, reports use the user's language, and model/effort pins follow the
central policy by role (design vs coding) unless explicitly overridden within
policy. Project quorum and financial/ownership gates still apply.


> The manual for people. Design rationale: [design.md](design.md). Machine
> details: [../gate/README.md](../gate/README.md). Unattended-run behaviour:
> [autonomy.md](autonomy.md).
>
> One-sentence mental model: **want the work to move forward → `$flow-run`. It
> loads the other two as needed. Only shaping an idea, handing off or tidying up,
> with nothing run → `$flow`; only running a queue that is already written →
> `$run`.**

Each agent CLI has its own sigil for invoking a skill: Claude Code `/flow`,
Codex `$flow`, Kimi Code `/skill:flow`, Grok Build `/flow`. This manual writes
`$flow` and `$run` as shorthand for whichever form your CLI uses.

---

## 0. What the installer puts in place

| Thing | Where |
|---|---|
| `flow` command | `~/.local/bin/flow` (bash shim) plus `flow.cmd` for PowerShell/cmd on Windows; both on PATH |
| Execution engine | `gate/gate.py` in the checkout (admission + acceptance + the unattended loop) |
| Tree auditor | `flow/audit.py` in the checkout (read-only) |
| Session continuity | `flow/continuity.py`: `flow resume`, `flow progress`, `flow checkpoint`, `flow recall` (section 4) |
| Publication verifier | `flow/publication.py`: `flow publish-verify` (section 4.2; read-only exact remote branch check + append-only `.gate/publication.ndjson` receipts) |
| Skills | `flow`, `flow-run`, `run-queue`, `model-debate`, installed into each CLI's global skills directory |

`flow home` prints the checkout directory, so every engine script can be
addressed as `"$(flow home)"/gate/<script>` without remembering where the
repository was cloned.

**Naming note:** the unattended skill's real name is `run-queue`, because `run`
collides with a built-in Claude Code skill. Saying `$run`, "unattended", or
"run the queue" routes to it correctly.

---

## Verified local maintenance release (Linux)

For an existing compatible Linux installation, the maintenance checkout is the
source authority. `flow/release.py` stages a deployment copy under
`~/.local/share/flow/<version>/runtime`; it does not update source, project
`.gate` configs/hooks, prior releases, historical receipts or machine policy.
Use this path instead of the legacy installer when updating this installation.
The legacy installer replaces directories and does not provide this recovery
contract. This tool requires Python 3.10+, the existing versioned `python3` shim,
four real canonical directories under `~/.agents/skills`, and their existing
symlink aliases under both `~/.codex/skills` and `~/.claude/skills`. Other layouts
are refused; it does not adopt or delete unrelated destinations.

The host first completes independent final review and full verification.
Implementation by itself does not authorize activation. After that delivery
boundary, run these commands from the maintenance checkout:

```bash
python3 flow/release.py inspect
# Review the printed target, hashes, modes and alias paths. Copy its fingerprint.
python3 flow/release.py stage --source "$PWD" --version <unique-version> --expected-install <reviewed-fingerprint>
python3 flow/release.py activate --version <unique-version>
python3 flow/release.py inspect
flow home
```

`inspect` is read-only. The explicit fingerprint acknowledges the exact existing
installation, including intentional local skill edits that differ from the old
runtime. Review those edits before staging; they are preserved in the backups.
Staging writes only a new release directory and the release lock. It records
per-file SHA-256 hashes and modes in `manifest.json` (plus `prior_release`,
`source_revision`, compatibility metadata, and evaluation/review references),
keeps original canonical skills and shim bytes in `backup/`, and leaves the live
installation alone. The payload is `flow/`, `gate/`, `learner/`, `skills/`,
`docs/`, README and LICENSE when present; caches, Git metadata, `.gate`,
`.learner-data`, SQLite/DB files and untracked files (such as untracked provider
logs) are excluded. The source and staged payload must still match at
activation. By default, `gate/model_policy.py` must match the prior installed
runtime byte-for-byte; only an explicit, reviewed `--policy-transition <json>`
descriptor permits a verified policy change when exact prior/candidate policy
SHA-256 digests, clean git `HEAD`, canonical repository path, prior release,
target version, and review/evaluation references all match.

Activation checks the original destination snapshot again and runs isolated
`flow.py home`, `gate.py --help`, and (when present) standalone `learner` and
`flow learn status` smoke commands with a temporary home and working directory.
It performs no inference, scheduler installation or business work. It then
replaces only the four canonical skill trees and atomically replaces the `flow`
shim with a shim targeting the new runtime. Skill directory swaps are
sequential; keep agents idle during activation/recovery. Alias link text stays
untouched. Release operations serialize through a nonblocking Linux file lock.
External edits must remain paused as well; this lock coordinates release
commands, not arbitrary editors.

Recover from an ordinary activation error (automatic rollback is attempted),
an interrupted activation, or an active release using the maintenance tool:

```bash
python3 flow/release.py rollback --version <same-version>
python3 flow/release.py inspect
flow home
```

Rollback restores prior canonical bytes/modes and the exact prior shim bytes,
mode and target (returning active `gate/model_policy.py` resolution to the prior
runtime). It also handles interruption between directory renames; rerun
rollback if recovery itself was interrupted. `state.json` records `prepared`,
`activating`, `active`, `rolling_back` or `rolled_back`. A prepared release has
not touched live files. A rolled-back release cannot be activated again; stage
a fresh version. Keep the manifest, backups and prior runtime until recovery is
no longer needed; the tool never prunes releases.

Activation refuses source, `HEAD`, or staged payload drift. Rollback tolerates
damaged code/docs in the abandoned runtime and changed maintenance source; it
still requires intact staged skill trees matching the manifest, backups and
prior runtime. Both operations refuse unexpected shim/aliases or live skill
bytes outside the recorded old/new states. On refusal, preserve the release and
inspect the named path; do not overwrite unrelated edits, change manifest hashes
to bypass the check, or run the legacy installer. Resolve the drift deliberately
and retry recovery, or stage a newly reviewed baseline.

### Directed learning release, verification, and rollback

All automated checks run offline with temporary roots before any host activation:

```bash
# Focused release, skill-lifecycle, and learning integration checks:
python3 -m unittest discover -s gate -p 'test_release.py'
python3 -m unittest discover -s gate -p 'test_skill_lifecycle.py'
python3 -m unittest discover -s gate -p 'test_learning.py'

# Full offline verification suites:
python3 scripts/verify_repository.py
python3 -m unittest discover -s gate -p 'test_*.py' -q
python3 -m unittest discover -s learner/tests -p 'test_*.py' -q
python3 -m unittest discover -s skills/flow/tests -p 'test_*.py' -q
python3 -m unittest discover -s scripts -p 'test_debate_quota.py' -q
bash gate/selftest.sh
```

When an authorized `gate/model_policy.py` transition is reviewed for the
canonical repository checkout, prepare a transition descriptor (replacing
placeholders with the committed `HEAD` SHA, chosen target version, final review
ID, and evaluation reference):

```bash
cat > /tmp/policy-transition.json <<'EOF'
{
  "package": "<authorized-package-name>",
  "repository": "<path-to-canonical-checkout>",
  "reviewed_revision": "<40-char-reviewed-HEAD-sha>",
  "target_version": "<target-version>",
  "prior_release": "<prior-version>",
  "prior_policy_sha256": "<64-char-prior-sha256>",
  "candidate_policy_sha256": "<64-char-candidate-sha256>",
  "review_ref": "<final-review-id>",
  "evaluation_ref": "<full-offline-check-summary>"
}
EOF

python3 flow/release.py inspect
python3 flow/release.py stage \
  --source "$PWD" \
  --version <target-version> \
  --expected-install <reviewed-fingerprint> \
  --policy-transition /tmp/policy-transition.json
python3 flow/release.py activate --version <target-version>
python3 flow/release.py inspect
flow home
```

Verify standalone `learner` operation outside the checkout with an explicit
`--storage-root` and no `PYTHONPATH` (plus optional isolated venv usage via a
`.pth` pointer to the active runtime):

```bash
# 1. Clean-environment import smoke outside repo (proves zero flow/gate imports):
SMOKE_DIR=$(mktemp -d)
RUNTIME=$(flow home)
(cd "$SMOKE_DIR" && env -u PYTHONPATH python3 -I -B -c "import sys; sys.path.insert(0, '$RUNTIME'); import learner, learner.cli; assert 'flow' not in sys.modules and 'gate' not in sys.modules; raise SystemExit(learner.cli.main(['--storage-root', '$SMOKE_DIR/store', 'status', '--json']))")
rm -rf "$SMOKE_DIR"

# 2. Direct invocation from actual runtime cwd without maintenance checkout or PYTHONPATH:
SMOKE_DIR=$(mktemp -d)
(cd "$(flow home)" && env -u PYTHONPATH python3 -B -m learner --storage-root "$SMOKE_DIR/store" status --json)
rm -rf "$SMOKE_DIR"

# 3. Optional standalone venv installation without PYTHONPATH from any directory:
python3 -m venv /tmp/learner-venv
echo "$(flow home)" > "$(/tmp/learner-venv/bin/python -c 'import site; print(site.getsitepackages()[0])')/learner-runtime.pth"
/tmp/learner-venv/bin/python -m learner --storage-root /tmp/learner-smoke status --json
```

Onboard a single project explicitly (keeping paused or `BLOCKED` tasks untouched)
or register a curated profile JSON, then grant a finite trial ceiling and expiry
before enabling `--live` model calls:

```bash
# Goal-level project onboarding (or register a curated profile JSON directly):
flow learn onboard --repo <project-dir> --json
# (cd "$(flow home)" && python3 -B -m learner --storage-root ~/.local/share/flow/learner register --profile <path/to/profile.json> --json)

# Grant finite cumulative model-call ceiling and UTC expiry (required before `--live` model calls; `--revoke` blocks dispatch without resetting prior usage/expiry):
(cd "$(flow home)" && python3 -B -m learner --storage-root ~/.local/share/flow/learner trial --grant --max-calls <max-calls> --expires-at <ISO-8601-UTC-timestamp> --grant-id <unique-grant-id> --scope project_pilot --json)
# Or explicitly opt into uncapped local model dispatch for this store (removes trial model-count/expiry and local daily/weekly model/deep-candidate quotas; preserves grant history, cumulative usage, daily metadata/fetch bounds, wall timeout, overlap/unknown/privacy safeguards, and paused task states):
(cd "$(flow home)" && python3 -B -m learner --storage-root ~/.local/share/flow/learner trial --unlimited --grant-id <unique-grant-id> --authority "<owner authorization>" --json)
(cd "$(flow home)" && python3 -B -m learner --storage-root ~/.local/share/flow/learner trial --json)
```

Active task pin and deterministic skill-selection inspection (reads immutable task-pinned skill bytes or policy snapshot when `--task` or an unambiguous `IN_PROGRESS` head task is pinned, and returns a deterministic `reasons` map recording the actual matched keywords, constraints, routing branch, reference triggers, and pin source without fabricating file or stage evidence):

```bash
flow skills select "<request>" --repo <project-dir> [--task <TASK-ID>] [--task-type <TYPE>] [--paused] [--read-only] --load --ref references/inspect.md
flow skills read-pin --repo <project-dir> [--task <TASK-ID>] --rel-path references/inspect.md
flow skills read-pin --repo <project-dir> [--task <TASK-ID>] --policy
```

Run scheduled `due` or on-demand checks for both modes (`project-led` and
`opportunity-led`), and inspect coverage, recommendations, model receipts, and
unknown dispatches:

```bash
# Scheduled due run (handles daily opportunity-led and weekly project-led cadence):
(cd "$(flow home)" && python3 -B -m learner --storage-root ~/.local/share/flow/learner due --live --plan-queries --json)

# On-demand checks for both modes (or individually: --mode project-led / --mode opportunity-led):
flow learn --repo <project-dir> --storage-root ~/.local/share/flow/learner check --mode both --live --json
(cd "$(flow home)" && python3 -B -m learner --storage-root ~/.local/share/flow/learner review --project-id <project-id> --mode both --live --plan-queries --json)

# Refresh and reassess evidence-deferred/assessed-deferred candidates without running new feed sweeps:
(cd "$(flow home)" && python3 -B -m learner --storage-root ~/.local/share/flow/learner review --project-id <project-id> --refresh-deferred --live --json)

# Recommendations (quiet when empty) and continuity surfacing:
flow learn --repo <project-dir> --storage-root ~/.local/share/flow/learner results --coverage
flow resume --repo <project-dir> --learning-status

# Coverage, trial, and unknown-dispatch inspection:
flow learn --repo <project-dir> --storage-root ~/.local/share/flow/learner status --json
(cd "$(flow home)" && python3 -B -m learner --storage-root ~/.local/share/flow/learner status --project-id <project-id> --json)
(cd "$(flow home)" && python3 -B -m learner --storage-root ~/.local/share/flow/learner results --project-id <project-id> --coverage --json)

# Inspect persisted dispatch reservation receipts read-only:
python3 -c "import sqlite3, pathlib; c=sqlite3.connect(pathlib.Path.home() / '.local/share/flow/learner/learner.sqlite3'); print('\n'.join(str(r) for r in c.execute('SELECT reservation_id, scope_type, role, state, units, actual_units, receipt_json FROM dispatch_reservations ORDER BY created_at DESC LIMIT 10')))"

# Reconcile an interrupted/unknown dispatch explicitly (settled or failed; never blind retry):
(cd "$(flow home)" && python3 -B -m learner --storage-root ~/.local/share/flow/learner reconcile --reservation-id <reservation-id> --outcome settled --actual-units <0|1> --note "<observed evidence>" --json)
```

Primary evidence retrieval bounds and limitations:
- Bounded download & section extraction: actual GitHub Trending HTML fetches use a dedicated finite `1 MiB` (`1048576` bytes) default through `fetch_github_trending` and `collect_opportunity_led` while honoring explicit smaller caller bounds and keeping `524288` bytes (`512 KiB`) for other feeds (including the labelled GitHub Search fallback, arXiv, and GitHub Releases). Primary text fetches read at most `max_primary_evidence_bytes` (default `262144` bytes; larger docs truncate safely with `document_truncated=true`), skip header/badge/TOC boilerplate, select up to 3 relevant sections (<=2000 chars each, <=5500 chars total) with line/char offsets and SHA-256 digests, and pack blind/synthesis prompts strictly within `max_packet_bytes` (`24000`, prioritizing assessor-cited evidence IDs/provenance and concrete `followup_*` evidence ahead of extra uncited sections, deduplicating matching citation metadata when tight, marking packet-size-omitted known evidence separately from unknown IDs, and deferring cleanly before dispatch as `synthesis_packet_deferred` / `packet_capacity_deferred` if minimum required evidence and context cannot fit without rerunning completed blind assessors). Exact citation verification remains strict without normalizing mismatched/full-digest IDs.
- Safe follow-up & metering: shallow READMEs (and `--refresh-deferred` on already-assessed deferred GitHub README-only items lacking concrete follow-ups, even when README prose met the broad mechanics heuristic) may follow at most 2 same-repository HTTPS text/doc/skill/source links (pinned ref where known; cross-repo, external, traversal, directory, and executable/installer links rejected; initial discovery stays bounded). `daily_fetches` (`10`/day) meters intake sweeps, post-intake `github_releases`, primary fetches (skipped when primary metadata and README are already cached), and follow-up fetches; `weekly_deep_calls` is checked before primary fetches.
- Transient feeds, model timeouts & limits: arXiv uses bounded retries (`max_retries=2`, `max_retry_after_s=5.0`), honors longer upstream `429 Retry-After` cooldowns without bypassing them, and falls back to cached XML only as `degraded`/`incomplete`. Deep assessment and synthesis calls use up to `300s` bounded by the remaining `20min` run wall deadline (global `ModelRunner` default stays `120s`); subprocess timeouts normalize mixed bytes/text output without duplicating buffered prefixes and persist partial/terminal evidence plus `unknown` usage as `unknown` dispatches requiring explicit reconciliation. `--refresh-deferred` reuses stored raw blobs first, reopens inference only when new/follow-up material is obtained, preserves prior assessments/receipts when material evidence or follow-up availability is unchanged, never retries `unknown` dispatches, and does not spend model calls when primary evidence remains insufficient. `status --json` includes `assessment_summary` explaining any remaining `seen/pending` or deferred rows.

Record project-owned skill experiment outcomes (`accepted`, `rejected`, or
`inconclusive`; negative/inconclusive outcomes feed future matching preferences
while keeping history intact):

```bash
flow learn --repo <project-dir> --storage-root ~/.local/share/flow/learner feedback \
  --project-id <project-id> \
  --assessment-id <assessment-id> \
  --experiment-ref <experiment-ref> \
  --status accepted \
  --baseline "<baseline summary>" \
  --outcome "<outcome summary>" \
  --evaluator "<evaluator>" \
  --release-ref <release-version> \
  --note "<optional note>" \
  --json
# For rejected or inconclusive experiments, use --status rejected or --status inconclusive (omit --release-ref).
```

Configure one `systemd --user` service and timer (invoking `due` with a
20-minute wall limit via `TimeoutStartSec=20min` and an `flock` overlap guard;
never install services during tests or alter linger, and do not register or
migrate other projects). One timer drives both daily opportunity-led sweeps and
weekly project-led reviews via `due`. Timer lifetime depends on the existing
`systemd --user` manager remaining alive (governed by `loginctl show-user "$USER" -p Linger`):

```ini
# ~/.config/systemd/user/flow-learner-due.service
[Unit]
Description=Project-scoped directed learner due sweep

[Service]
Type=oneshot
TimeoutStartSec=20min
ExecStart=/bin/sh -lc 'RUNTIME="$(%h/.local/bin/flow home)" && cd "$RUNTIME" && exec /usr/bin/flock -n "%t/flow-learner-due.lock" /usr/bin/python3 -B -m learner --storage-root "%h/.local/share/flow/learner" due --live --plan-queries --json'
```

```ini
# ~/.config/systemd/user/flow-learner-due.timer
[Unit]
Description=Daily timer for directed learner due checks

[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=5m

[Install]
WantedBy=timers.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now flow-learner-due.timer
systemctl --user list-timers flow-learner-due.timer
journalctl --user -u flow-learner-due.service -n 50 --no-pager
```

Pause the trial (retaining learning DB and evidence) or perform a full rollback
(stopping the timer and restoring prior Flow runtime, `gate/model_policy.py`,
canonical skills, and shim while preserving `~/.local/share/flow/learner`):

```bash
# Pause trial or unlimited dispatch immediately without deleting DB/evidence (can re-enable later via finite `--grant` or `--unlimited` with a new `--grant-id`):
flow learn --repo <project-dir> --storage-root ~/.local/share/flow/learner pause --json
(cd "$(flow home)" && python3 -B -m learner --storage-root ~/.local/share/flow/learner trial --revoke --json)
systemctl --user stop flow-learner-due.timer

# Full rollback (stop timer, revoke trial, restore prior release/skills/policy/shim, preserve learner DB/evidence):
systemctl --user disable --now flow-learner-due.timer
systemctl --user stop flow-learner-due.service
(cd "$(flow home)" && python3 -B -m learner --storage-root ~/.local/share/flow/learner trial --revoke --json)
python3 flow/release.py rollback --version <target-version>
python3 flow/release.py inspect
flow home
```

## 1. One-time onboarding per project

```powershell
cd <your-project>
flow init --repo . --verify-cmd "<acceptance command>"
```

- Creates only the files that are missing (`TASK_QUEUE.md`, `CONTEXT.md`,
  `.gate/config.json`, the pre-commit hook). **Existing files are never
  overwritten**; re-running is side-effect free.
- `--verify-cmd` is the only thing you must decide yourself: one command that
  exits non-zero when the project is broken (for example `uv run pytest tests -q`).
  **If you do not give one, leave it empty. While it is empty, admission refuses
  every task with `no-oracle`** — loud beats a guessed oracle.
- Check: `flow doctor --repo .`

## 2. The daily loop

### 2.1 Have an idea → tell your agent `$flow ...`

Examples: `$flow I want to add employee growth records`, `$flow show me a few
clickable options first`, `$flow switch session`, `$flow insert an urgent fix`,
`$flow is this project getting messy`.

`$flow` will: ask until the goal is testable → write `docs/work/<id>/brief.md`
and `spec.md` → queue tasks into `TASK_QUEUE.md` with a scope declaration →
run admission and show you the result.

### 2.2 Queue row format (the same when you queue by hand)

```markdown
| 1 | WP-1 | add greeting function | `TODO` | 0 passed | 1 passed |

<!-- task:WP-1 files: src/greet.py, tests/test_greet.py -->
```

- The status must be wrapped in backticks (`` `TODO` ``); otherwise the row does
  not count.
- Below the table, one scope declaration per task (an HTML comment, invisible
  when rendered).

### 2.3 Admission check

```powershell
flow admit --plan --repo .
```

Three states:

| Result | Meaning | What you do |
|---|---|---|
| `ok` | In the sweet spot (≤6 files, no overlap with other TODO tasks, an oracle exists, no "design/decide"-style wording) | Nothing |
| `REFUSE` | Outside the sweet spot; the reason follows | **Split the task smaller; do not loosen the thresholds.** Something that genuinely needs a human decision goes back to `$flow` interactively |
| `UNDECLARED` | No scope line | Add one |

The default is **advisory mode**: a refusal warns but does not block. After one
full cycle on a real project, human-checked for false refusals, set
`"strict_admit": true` in that project's `.gate/config.json`; from then on a
refused task stops the run outright. A malformed table width always stops dispatch:
a shifted test-count cell must not become a worker name.

`--plan` also previews selected stage commands without executing them, flags
invalid checks/spec links, invalid opted-in handoff schemas or oversized packets,
and warns about earlier scope requests and broad/repeated checks. Errors exit 2;
warnings do not rewrite acceptance. Optional task handoffs (`## Handoff <TASK>`)
may opt into `role_sections` (`worker` and/or `review` mapped to unique exact
`###` headings within that task's handoff) and `required_sections` inside
`<!-- handoff: {...} -->`: preamble and `required_sections` stay in every role
with verbatim citations and compact hash/byte/omission provenance, roles without
a selection receive the full handoff, `STALE/UNVERIFIED` anchors disable role
filtering with a reread warning (`CURRENT` verifies related-file Git freshness
only, never prose truth), legacy handoffs that do not opt into `role_sections`
or `required_sections` (including extra legacy keys, malformed/non-object
comments, duplicate legacy comments, or literal key mentions in values/prose)
preserve full legacy behavior without failing admission, invalid opted-in schemas
fail admission before dispatch regardless of advisory mode, mandatory packet
components (including full `Acceptance`, scope-request protocol and skill pins)
are never filtered, and review-selected handoff text is inlined through bounded
`review_packet` projection. Inspect implementation, callers and tests before narrowing a command.
Fresh code still needs fresh evidence. Use `verify --task` as the final task
check rather than running the same underlying command immediately before it.

For efficiency comparisons, `flow metrics --last 5 --repo .` reads recent runs;
add `--json` for structured output. New runs record probe/model/check durations
and lock waits. Older missing measures are unknown. Worker duration includes
its checks, so do not add the two. Compare similar batches and defect outcomes,
not just task or call counts. No extra monitoring service is needed.

### 2.4 Unattended → tell your agent `$run`

The agent will: run the doctor + admit preflight and show it to you → confirm
once (how many tasks) → launch detached through Task Scheduler (the run survives
the agent session ending) → poll the journal and narrate in real time → read
`RUN-REPORT.md` to you at the end.

The manual equivalent on Windows:

```powershell
pwsh -NoProfile -File "$(flow home)\gate\launch-detached.ps1" -Repo <project> -MaxTasks 3 -TaskName gate-<name>
```

Detached launch uses Windows Task Scheduler and `pwsh`. On macOS/Linux a
foreground `python "$(flow home)/gate/gate.py" run --repo <project> --max-tasks 3`
(or the same under `nohup`) runs the identical loop, but that path has not been
exercised by the authors.

Iron rules (enforced by mechanism, not by convention): a worker cannot mark
itself done — the pre-commit gate blocks a "DONE commit that only changes the
queue"; the verdict comes only from the acceptance command's exit code; two
identical failures stop the run; a CLI whose quota is exhausted is benched and
the next CLI takes over without burning an attempt.

### 2.4b Autonomy (design in [autonomy.md](autonomy.md))

- **Probe before dispatch.** Every candidate CLI gets one tiny real request at
  run start; a quota-shaped reply benches it for the cooldown.
- **Local checks while working.** A row may admit its own test command
  (`<!-- task:ID verify: {"cmd": ..., "timeout_s": N} -->`); the worker runs
  `flow verify --task ID` and the hook accepts the DONE commit while the
  declared files keep the tested bytes. Genuine docs-only tasks opt in with
  `<!-- task:ID kind: document -->` (default `code`), declaring substantive
  non-status deliverables and a real task-specific `verify` command (content
  validation is task-owned; see [autonomy.md](autonomy.md#2-test-selection)).
- **One review per batch.** With `judge.enabled`, a read-only reviewer reads
  the commits once, when the queue has no TODO left; tasks closed by earlier
  runs are included. Current-scope safety and due acceptance defects block;
  ordinary defects go to `REVIEW-NOTES.md` with stable BUG ids and checkpoints.
  They never automatically become rows. No automatic revision loop, no
  score threshold. Ordinary small reversible tools leave review off by default;
  sensitive behavior gets a focused review. Repairs verify the original
  blockers and direct regressions only, not another general audit.
- **Stage and impact select checks.** Set `delivery.stage` to development,
  module, integration or delivery. Declare each task's `impact: local`,
  `shared` or `unknown`; shared work needs caller checks in `delivery.checks`.
  Full acceptance is for delivery, broad or unknown impact; local batches use
  task checks and mapped callers. Integration also runs `integration_cmd`.
  Missing declarations conservatively fall back to full checks. Several projects
  can run at the same time on one machine; their full oracles take turns, so
  one never slows another into false failures.
- **Bounded spend.** `max_model_calls` and `run_timeout_s` cap a run. The host
  also carries the agreed delivery budget across restarts in CONTEXT; this
  cumulative planning limit is not enforced by an engine ledger.
- **Irreversible actions need prior approval.** A task whose declared files
  hit `irreversible_globs` runs only with `<!-- task:ID approved: <who/date> -->`.

### 2.5 Reading a stop

| Stop reason | Meaning |
|---|---|
| `queue_empty` | The batch closed at its configured stage; this is not automatically product delivery |
| `budget` / `budget:model_calls` / `run_timeout` | A configured limit with work left; start another run if you want more. The review and the full oracle wait for the package boundary |
| `blocked:<id>` / `admit_refused:<id>` | The task needs decisions or a smaller shape |
| `no_progress:<id>` / `not_done:<id>` | The system refused to record work it could not verify; read the log under `.gate/runs/<run-id>/` |
| `worker_left_changes:<id>` | Two attempts left the tree dirty without closing the task |
| `scope_request:<id>` / `prompt_too_large:<id>` | Widen or split the task |
| `no_workers` | Every CLI is benched; wait for the cooldown |
| `review_failed:<ids>` | Current-scope safety/due blockers or unclear acceptance; consolidate evidenced repairs tagged `origin: review` |
| `review_loop:<location-or-task>` | Recorded repair blockers show no progress; diagnose the cause before dispatching again |
| `stage_acceptance_failed:<ids>` | A selected check failed or a due defect is unresolved; read the named logs and defect records |
| `full_acceptance_failed:<ids>` | The full oracle fails after the run; its whole output is in `.gate/runs/<run-id>/full-acceptance.log`; admit one repair row |
| `needs_approval:<id>` | Add the task's `approved:` line after reading its scope |

**Stopping is good. The only unacceptable outcome is a green light that lies.**

At a planned checkpoint without new implementation tasks, use
`flow verify --stage module --tasks A,B` or `flow verify --stage delivery`.
New projects start in development; old configs keep full acceptance until an
explicit stage is set. Read [autonomy.md](autonomy.md) for the complete mapping,
defect-resolution and migration contract. A local PASS never substitutes for
the delivery candidate's complete check.

## 3. The project is getting messy

```powershell
flow audit <project-dir> --out <somewhere-else>/audit.md
```

Read-only: nine categories, oversize-active-document warnings, duplicate file
groups. **It only proposes; v1 has no write mode at all.** When files really
should move, a human approves one batch at a time, starting with the
`generated` and `runtime` categories. The report may not be written inside the
audited tree (the script refuses).

## 4. Switching sessions / handing off

Tell your agent `$flow switch session`. It rebuilds `CONTEXT.md` (≤200 lines,
five questions: goal and red lines / what is being worked on / last checkpoint /
next step / decisions waiting on a human). **CONTEXT.md is the handoff document;
there is no separate long handoff write-up.**

The same continuity commands work from every CLI (your agent runs them for you):

| Command | What it does |
|---|---|
| `flow resume --repo .` | Read-only recovery for a new session: the five CONTEXT sections with line references, Git state, queue head, last gate run and verdict, the bounded `progress` and `publication` projections, work packages, and anything that overrides the snapshot (stale snapshot, interrupted or active run, failed or outdated PASS, stale/unverified publication receipt, uncommitted paths). Prints the snapshot digest |
| `flow progress --repo . [--json]` | Read-only bounded progress snapshot reusing `journal_tail` and `gate/watch-journal.py` compact event vocabulary (no journal, cursor, lock or config writes; no daemon or inference): latest run/task, meaningful progress (excluding `heartbeat` noise), separate last sign of life, current acceptance-lock wait/holder (live waiting only when `runner_pid` is live; cleared on new run, `run_end` or acceptance; missing historical `waited_s` shown as `unknown`), integrity warnings (malformed records, truncated tail, missing `run_start`, bad timestamps), and last relevant error. Explicit `run` IDs must match; unscoped events are attributed positionally only within the latest run boundary and labelled accordingly |
| `flow checkpoint --from draft.md --expect <digest>` | Validates a complete five-section draft and replaces `CONTEXT.md` atomically, with an anchor line recording HEAD and the tree. Malformed, oversized or out-of-date input (someone else saved in between) changes nothing. Use `--expect none` for a first snapshot, `--dry-run` to check only |
| `flow recall <words> --repo .` | Literal search (English or Chinese words) over CONTEXT, AGENTS.md, ADRs, work packages and review notes of this project only, with file:line references; records marked superseded/rejected are labelled. "No match" means it was never saved |

Saying "continue" in a new session is enough: the agent runs `flow resume`.
Decisions and corrections reach later sessions only if they were saved — the
agent writes them as dated lines in the work package's spec Decisions or the
evidence Lessons table. A chat that was never saved cannot be recovered.

The agent also considers a handoff on its own at three phase boundaries:
research done and implementation about to start, a work package closed, a
switch to another task. It is advice. The agent uses a context-usage figure
only when its CLI states one; without one the usage is "unknown", never a
guessed percentage, and a count of tool calls never forces anything. It does
not hand off in the middle of a failing test or a half-made edit: it first
writes the recoverable state into `CONTEXT.md`. An interrupted task keeps its
failing command, its uncommitted paths and the path of its log under "Now",
and is never described as done. Compaction stays the CLI's own feature; this
workflow installs no hook for it and prunes no transcript.

### 4.1 Lessons

When you correct the agent, or a failure is reproduced and its fix verified,
the agent may add one row to the Lessons table of that work package's
`evidence.md`: the observed fact, the proposed rule, the evidence (commit,
file and line, test or log), a status and a date. A row is a candidate and
changes nothing. It becomes a rule only when you approve it; the agent then
writes it once into `AGENTS.md` or an ADR and records that destination in the
row. Lessons stay in the repository they came from; nothing is promoted to a
global skill or to another project unless you say so. Your standing
instructions always win over a lesson.

### 4.2 Exact remote publication verification

Verify that an already-configured Git remote's exact branch ref points to an
expected full 40- or 64-character commit SHA without pushing, prompting,
retrying, or mutating `.gate/verdict.json` or `.gate/journal.ndjson`:

```bash
flow publish-verify --repo <project-dir> --remote <remote-name> --branch <branch-name> --expect <full-40-or-64-sha> [--timeout <seconds>] [--json]
```

- **Target and transport safety**: `--remote` must name an existing configured
  Git remote (never a raw URL, path, or option). Before running `git ls-remote`,
  `flow/publication.py` resolves and validates the configured remote URL and
  local remote config: normal `https://`, `ssh://`, SCP (`[user@]host:path`),
  and local bare repository targets are supported; `ext::`/`fd::`/helper
  transports, plaintext `http://`/`git://`, custom `vcs`/`uploadpack` or local
  `core.sshCommand`/`core.gitProxy` overrides, option-shaped hosts/users/paths,
  query/fragment tokens, and embedded HTTP/SSH/SCP credentials are rejected
  before transport. Raw credentials and raw `git` stderr are never emitted or
  persisted. `--branch` is validated with `git check-ref-format` plus
  conservative non-option, non-control/whitespace, and non-glob rules.
  `git ls-remote` runs once (`argv` list only, bounded timeout,
  `GIT_TERMINAL_PROMPT=0`, askpass disabled, `protocol.ext.allow=never`, no
  retry, no push/PR/config/credential changes) and matches only a single exact
  `refs/heads/<branch>` row.
- **Statuses and exit codes**:

| Status | Exit code | Meaning |
|---|---|---|
| `match` | `0` | Exact `refs/heads/<branch>` resolved unambiguously to `--expect` |
| `invalid` | `2` | Invalid/missing CLI arguments, unconfigured remote, unsafe remote transport/options/embedded credentials, or symlink receipt path refused |
| `mismatch` | `3` | Exact `refs/heads/<branch>` resolved unambiguously to a different commit SHA |
| `absent` | `4` | Remote responded cleanly and exact `refs/heads/<branch>` does not exist (tags and branch-name suffixes never match) |
| `unavailable` | `5` | Remote unreachable, authentication unavailable/prompts disabled, timeout, or malformed/duplicate `ls-remote` output |

- **Receipts and `flow resume` projection**: each attempt appends one bounded
  JSON record to `.gate/publication.ndjson` (refusing symlinks on `.gate` and
  `.gate/publication.ndjson`) containing a UUID, UTC timestamp, target identity
  recording that `git ls-remote` observed the `fetch` endpoint, the SHA-256 hash
  of the sanitized fetch URL, and the read-only effective push URL binding
  (`git remote get-url --push --all`, expanding `pushurl` and `pushInsteadOf`),
  expected and observed SHAs, local `HEAD` and candidate cleanliness, verdict
  metadata, and explicit delivery-scope acceptance freshness/coverage (a
  task-local or partial-stage `PASS` never satisfies delivery coverage). `flow
  resume` reads the bounded receipt tail read-only and separates the observed
  remote fact at time `T` from current local acceptance: ordinary matching
  single-endpoint remotes (`push_binding=same`) verify normally when local
  delivery acceptance is fresh, whereas distinct (`push_binding=different`),
  multiple (`push_binding=multiple`), unverified, or legacy missing
  (`push_binding=unknown`) push bindings retain the fetch observation while
  disclosing the push endpoint status and never reporting
  `current_delivery_verified`; changing a remote's fetch or push URL binding
  triggers `remote_drift` so an earlier receipt's `match` cannot be reused, and
  stale `HEAD`, dirty candidates, or non-delivery local verdicts never imply
  current delivery. Receipts record read-only observations and never establish
  push or release authority.

## 5. Troubleshooting

| Symptom | Fix |
|---|---|
| `flow` command not found | Open a new terminal; confirm `~/.local/bin` is on PATH |
| The pre-commit hook blocked a commit | Read the reason it printed. **Do not use `--no-verify`** — the block is the product; a bypass is recorded as FAKE_COMPLETION by `flow audit-gate --repo . --last 30` |
| `admit` refuses everything with `no-oracle` | `verify_cmd` in `.gate/config.json` is empty, or the last acceptance run was VACUOUS; set it or fix it |
| `doctor` says the hook is not installed | `flow install-hook --repo .` |
| `flow audit .` refuses to write the report | Expected — the report may not land in the audited tree. Pass `--out` pointing outside the project (desktop, temp directory) |
| Regression self-test | `bash "$(flow home)/gate/selftest.sh"` (expect 10/10) |

## 6. Uninstall

Delete `.git/hooks/pre-commit`, `.gate/`, and `~/.local/bin/flow*`. For the
skills, delete `~/.agents/skills/{flow,flow-run,run-queue,model-debate}` and the
same-name junctions or copies under `~/.claude/skills` and `~/.codex/skills`.
Project documents (CONTEXT, queue, work packages) are ordinary files; keep or
discard them as you like.
## Worker models

Each worker CLI runs whatever model its own configuration selects, unless
`.gate/config.json` pins one in `worker_cmds` (the full command line) or, for
the `pi` worker, in `worker_models.pi` (an OpenRouter model id). The engine
never reads the host session's model; a queue runs the same way from any CLI.

## Local model inventory

`flow inventory status --json` reads bounded local state without discovery,
network, inference, or enrollment. When installed, optional role/model/effort policy remains in
`~/.config/mini-model-routing/policy.json`; inventory visibility never establishes
entitlement or changes a policy pin. Standalone installations use explicit project worker/reviewer contracts
without requiring that policy. Normal routing, planning, admission and dispatch
read local state only and never wait for a learner refresh.

Real-work quota observations are shared across projects by exact route, model
and account scope, ordered by event time. A successful exit alone never clears a
limit: native terminal metadata must report the actual matching model. Unknown
identity stays unknown, and success never clears an owner exclusion. Set an
opaque `FLOW_ACCOUNT_SCOPE_CODEX`, `FLOW_ACCOUNT_SCOPE_CLAUDE` or
`FLOW_ACCOUNT_SCOPE_AGY` label consistently in routing and runner environments
when using separate subscriptions. Known CLI profile/home selectors also split
scopes using a local hash; credential contents are never inspected. Without a
binding/profile, `default-login` explicitly assumes one login per CLI route.
Catalog discovery remains advisory; it does not clear account restrictions.

A terminal native refusal requiring extra usage credits holds that exact
subscription route/model/profile indefinitely. Rate limits, periodic quota and
network errors retain their ordinary behavior. The exact historical account
cooldown reason `Actual subscription CLI requires usage credits; no paid fallback.`
is treated as a credit hold even after expiry and migrated on the next inventory
write. Other historical cooldowns remain ordinary. Identified project credit
holds yield only to newer recheck evidence; ambiguous old project `quota` records
cannot be attributed to credits and retain their expiry. Historical project
credit records without an event timestamp use their expiry as the ordering bound.

Weekly live refresh also reads bounded `claude auth status --json`, without
inference. It records hashed identity/profile and subscriptionType/planTier as a
baseline. Only changed entitlement signals for the same identity/profile allow
the next authorized subscription call to revalidate. Missing auth, a changed
login, catalog/news updates and time alone do not release holds. If identity or
plan signals are unavailable, or a same-tier grant is invisible to auth status,
use `flow inventory recheck --model EXACT_MODEL` (defaults to current Fable;
`--route` defaults to `claude`). This local command permits useful revalidation;
it makes no provider call and does not claim success. Keep the same profile/scope
in the command environment as in dispatch. Owner exclusions, regular quota,
other profiles/models and task budgets remain in force. Another credit refusal
immediately reinstates the hold. No auth status command runs during selection,
ordinary dispatch, or fresh/idle inventory ticks.

Inventory is an optional Flow integration. The separately packaged learner keeps
working without Flow: ordinary status and due commands do not import gate modules;
due reports inventory unavailable when the shared Flow runtime is absent.

```sh
# Local catalog files only; an explicit cache keeps fixtures isolated.
flow inventory refresh --catalog codex=/path/to/models_cache.json --cache /path/to/inventory.json --json
# Out-of-band discovery through existing subscription/catalog routes; no inference.
flow inventory refresh --live --json
# Version-specific source review supplied by the owner/reviewer.
flow inventory refresh --live --reviewed-hints /path/to/reviewed-hints.json --json
# Record/release an owner report against the current exact central Fable model.
flow inventory exclude-fable --reason 'Owner reports this machine unavailable' --json
flow inventory release-fable --reason 'Owner confirms availability restored' --json
```

`flow inventory status` retains the existing raw inventory payload on stdout in
both human and JSON modes. Human mode adds a hint on stderr for effective
subscription credit holds for models in the current account/profile; no holds
means no added message. Explicit worker and review pins (including
Fable audit members) also explain a credit hold when skipped, even if another
route succeeds. The hint names the exact model, route, profile and last verified
tier when available, and provides a shell-quoted `flow inventory recheck` command.
For a different profile, switch to that profile and request status there before
rechecking. Default-cache guidance uses plain `flow inventory recheck --model
EXACT_MODEL --route ROUTE`; an explicit `--cache` is preserved exactly and safely
quoted in status guidance. `--json` adds no hint. These local diagnostics neither check access nor
release holds automatically; ordinary quota cooldowns do not produce this hint.

The machine cache is `~/.local/share/flow/model-inventory.json`. Existing learner
`due --live` refreshes it independently every seven days. Offline/API due calls
use a cache inside their explicit store root and never launch catalog commands.
Local refresh reads Codex's existing catalog and any explicit `ROUTE=PATH` files;
`--live` also runs native `agy models`, capped at ten seconds and one MiB. Listings
record route, exact identifier, CLI version when exposed, source and observation
time. Missing catalogs and unknown account state remain unknown.

Discovery uses a nonblocking writer lock, atomic replacement and boot/process
start identity where available. A failed refresh preserves the last-good snapshot;
a separate attempt timestamp prevents repeated discovery within seven days.
Readers keep using local stale-good evidence. Release/capability sources for
OpenAI, Google and Anthropic are advisory, fetched only during explicit live weekly
refresh through the existing bounded learner HTTPS/evidence facilities. Failed
hints preserve prior hints from each failed source and report errors separately.
Stable primary markdown sources provide bounded readable evidence slices,
literal model/family mentions, links and observation dates. Exact host protection
and the 64 KiB ceiling remain enforced. Discovery releases the account writer
lock before collection/network work and merges into the latest atomic state,
preserving account events and owner exclusions received during refresh.

A reviewed-hints file is an object with a `sources` list. Each version-specific
entry requires `url` (HTTPS), `provider`, `model` (exact version present in the
fetched evidence), `authority` (`primary` or `secondary`), `reviewed_by` and
`reviewed_at` (ISO date); secondary evidence also requires `as_of` (ISO date).
Review provenance is an explicit attestation supplied by the caller; discovery
does not invent review or authorize model adoption. Evidence is stored by digest
using the existing learner store (`--storage-root` can isolate it).

`learner.model_inventory.record_observation` records exact route/model account
success, cooldown or unknown state with event timestamps; permanent exclusions
and their releases require owner authority. Shared-pool limits need explicit
route/model membership evidence. Newer account success or weekly catalog data
cannot clear an owner exclusion; stale events cannot clear newer limits. Quota
and infrastructure failures are availability evidence, separate from learned
implementation quality. Recommendations expose filtered routes and may have no
backup; they fail closed if none remain eligible. Keep strict owner pins restricted
to their configured single worker; ordinary queue pins retain existing fallback.

With mini policy, implicit gate reviews use exact read-only Carbon/Astra adapters.
Explicit compatible custom review contracts remain eligible, including healthy
centrally authorized Fable contracts regardless of their display alias. Historical
explicit chains and templates are validated unchanged against current policy;
stale pins, missing pins and unsafe mode overrides fail closed at admission and
review. Independence is rechecked against workers that actually completed tasks
after fallback. Unavailable review preserves completed work as unreviewed, with
existing budgets and acceptance gates intact.

### Durable supervision of an authorized batch

For new authorized flow-run batches with a known origin session, select
`flow supervise` by default over the ordinary gate runner. Opt out explicitly
when session metadata or a durable host owner is unavailable. Bind
only the admitted TODO tasks, the package specification and the originating host
session. Use finite cumulative calls and wall-clock seconds under the existing
authorization. For example (replace every path and session identifier):

```sh
flow supervise launch --repo /absolute/project --session <exact-origin-UUID> \
  --package docs/work/package/spec.md --tasks A,B --max-calls 12 --seconds 7200 \
  --transport codex --watch
```

Launch this command as the existing user systemd transient service when available
(see the run-queue launch reference). `--watch` owns one watcher without requiring
another user turn. Without it, launch detaches the gate and exits; the host must
explicitly run `flow supervise watch --repo ...` under a supported owner. The
adapter neither installs a scheduler nor enables lingering. User-manager lifetime
still depends on the platform's existing login settings. Attached watch is
available where there is no detached owner; disclose that lifetime limitation.
Use `--transport codex` for an existing local Codex UUID. The adapter checks
local session metadata against both UUID and repository before launch. Generic
Unix IPC remains available when the host has an explicit compatible receiver;
otherwise delivery stays pending. Windows retains its existing launcher.

`.gate/supervision/state.json` binds the resolved repo, package and configuration
hashes, admitted task metadata (including scope, checks, pins and dependencies),
origin session, absolute deadline and cumulative model calls. Task status and
completion commits may advance normally. All new gate runners share a
process-lifetime writer lock; supervised worker, probe and reviewer dispatches
reserve calls under the same short transaction as user stop. Existing policy,
strict pins, clean fallback, required review and acceptance remain gate-owned.
`flow supervise stop --repo ...` persists the stop and prevents later dispatch;
an already-started call may finish. Stop never resets the envelope or budget.

The idle `tick` is a bounded no-model/no-network check, due about every 30 minutes and
persistently deduplicated; duplicate ticks return without reading the journal.
It uses the existing byte cursor, reading at most one bounded page per due tick.
Backlog pages are immediately due. Healthy idle checks do not scan history or
send empty updates. Boot ID and process-start identity distinguish reused Linux
PIDs; unavailable identity requires host reconciliation. Only clean interrupted
work with no unresolved call, claimed task or competing runner can resume, once
per progress signature. A failed or unchanged outcome, dirty/uncertain work,
journal integrity warning, configuration/scope change or exhausted cumulative
limit becomes a durable host hold. Clean `no_workers` endings instead enter
`waiting_routes`; later due ticks check local eligibility and known cooldown
expiry, with no network probe or inference. Recovery is attempted once per
changed eligible-route/progress signature within the same cumulative envelope.
Startup with no available routes also records a normal `no_workers` end; an
unsupervised run still refuses at startup. Terminal accepted/stopped watchers
exit after a delivery attempt even when a final message remains pending on disk.
Codex delivery binds only in the explicitly invoked originating session with
its exact trusted UUID; the repo may be nested under its persisted working
directory. Unrelated threads and `--last` remain unsupported.
Use `flow supervise resume --repo ...` only after same-owner reconciliation of
partial effects and task rows, from the bound session. The default session is
trusted `CODEX_THREAD_ID`; `--session <same-bound-ID>` is also supported. It preserves the token, counters, scope and deadline
and refuses live/uncertain calls, edits or unresolved delivery. A separately
authorized launch after settlement/stop archives the old envelope (including any
pending message); it never silently resets the old batch. No transcript text is
executed or replayed.
No automatic source discard or worker takeover occurs.

The native Codex adapter uses a bounded `codex exec resume --json` targeting
only the verified UUID, exact configured design pin and read-only sandbox.
With an installed policy, its design Codex pin is authoritative. Without one,
set `supervision.notification_pin` in `.gate/config.json` to an object containing
exact `model` and `effort` strings before binding the batch. Missing pins keep
notifications pending without a call. It sends a
fixed status prompt with supervisor phase/counts, acceptance boundary and event
ID, never transcript text or event
commands. The turn consumes the same cumulative call budget and deadline. It
waits while the runner or a detected exact-session resume is busy; stop prevents
new turns. A matching `thread.started` and successful `turn.completed` establish
`exact_session_turn_completed`, not proof the user read the message. Missing
CLI/metadata, busy/offline routes or exhausted limits retain pending. An
ambiguous started turn is not replayed automatically; the host must reconcile
it against that exact thread. Disposable real-session verification is a separate
host acceptance step; offline fixtures never call a vendor.

The optional generic transport is a host-owned Unix stream socket with owner UID
verification where supported. The host registers that socket for the exact
originating session. A receiver accepts a bounded newline JSON notification:
`version`, `session`, `id`, `repo`, `notification` (phase/reason/bounded event
summaries/accepted tasks), routes it only to that registered session and replies
`{"session":"exact-origin-thread","id":"received-id","ack":true}`.
Receiver deduplication uses session plus ID; delivery may repeat after a lost ACK.
A changed summary gets a new ID. Receivers must treat all fields as data and must
never create owner threads, run transcript commands or start competing writers.
This IPC contract requires an actual host adapter; a CLI shell alone does not
provide chat delivery. Missing transport or ACK remains visibly `pending` and
retries on later due ticks. Native Codex delivery requires explicit
`--transport codex`; `status` and read-only `flow resume --json`
expose the pending message on reconnect.

`run_end` is not acceptance. Only the gate's successful package settlement records
accepted task IDs; supervision reports `accepted` after the runner exits with
fresh, clean settlement evidence for every bound task. Report the selected stage
and any remaining delivery checks from RUN-REPORT; a development-stage settlement
does not establish delivery acceptance. Outages and switches are notifications,
never learned quality scores. Run the full offline suites and focused review
before activating this code. Sandbox tests cover CLI launch/hook/settlement,
restart/stop races, PID reuse, bounded idle, duplicate ticks and no-user-turn IPC.

Rollback: stop the supervisor service and persist `stop`, inspect and wait for any
existing runner/call to finish, then retain the supervision directory and journal
as evidence. Restore the prior runtime only after confirming no writer remains.
Do not delete locks/state to force a restart. A responsible host may archive a
held envelope only after reconciling side effects, ownership and remaining
authorization; a same-batch resume retains its counters/deadline.
No live project, installed runtime, scheduled job or active thread is migrated by
this feature or its tests.

The built-in Carbon reviewer stays in plan mode with session-only scoped
`read_file` access to the repository, `.gate/final-review-evidence` and owner
model policy files. A shared contract builder/validator upgrades legacy plan
commands while preserving env/TLS wrappers. Writes, shell commands and remote
tools are denied; no persistent settings are changed.


### Technical review consultation

Repeated mapped blockers or an explicit technical dispute arrange two blind
senior sessions through existing guarded read-only providers. The run report and
supervision notification identify pending/needs-host advice; it is never a pass.
Inspect `.gate/consult/state.json`, check the evidence, and use
`flow consult-settle --tasks <ids> --episode <id> --disposition <json-file> --repo .`
to record one repair and dissent disposition. Then hand the scoped repair to the
coding pool and obtain fresh independent review. Unchanged rejected candidates
are deduplicated across explicit review/restart. See
[consultation accounting and settlement](autonomy.md#senior-consultation-after-technical-rejection).
