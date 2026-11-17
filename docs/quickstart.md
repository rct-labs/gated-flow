# Quickstart

Concise guide to installing the runtime, initializing a project, and running
common prompts and terminal commands. For full details, see [usage.md](usage.md),
[autonomy.md](autonomy.md), and [design.md](design.md).

## 1. Prerequisites and runtime vs skills-only install

- **Plain skills install does not install the runtime**: `npx skills add rct-labs/gated-flow`
  or Claude Code's `/plugin install` copies skill markdown files only; it does
  **not** install the `flow` CLI shim, `gate/`, `flow/`, or `learner/`.
- **Install skills + `flow` runtime** from a checkout:
  - Linux / macOS / Git Bash: `bash install.sh`
  - Windows PowerShell: `pwsh -File install.ps1`
- **Model policy and CLI credentials are installed separately**: before
  dispatching implementation workers, configure your local model policy at
  `~/.config/mini-model-routing/policy.json` (and companion `POLICY.md`) and
  authenticate your subscription CLIs separately. See
  [required local configuration](autonomy.md#required-local-configuration).
- **Project test command cannot be guessed**: you must supply your project's
  real failing-when-broken acceptance command via `--verify-cmd`. Leaving it
  empty causes `flow admit` to refuse code tasks with `no-oracle`.
- **Publication and deployment need project authorization**: `flow` never
  pushes, merges, publishes, or deploys on its own; each repository's standing
  or explicit authorization governs release, commit/push, and deployment.

## 2. One-time project initialization and health check

```bash
flow init --repo <project-dir> --verify-cmd "<REAL_COMMAND>"
flow doctor --repo <project-dir>
```

`flow init` creates only missing files (`TASK_QUEUE.md`, `CONTEXT.md`,
`.gate/config.json`, and the git `pre-commit` hook) and never overwrites
existing files.

## 3. Practical agent prompts

Skill sigils differ by CLI (`/flow` in Claude Code and Grok Build, `$flow` in
Codex, `/skill:flow` in Kimi Code); this guide uses `$`.

| Goal | Example prompt | What it does |
|---|---|---|
| Implement / resume authorized work | `$flow-run` | Host inspects `flow resume` + `flow doctor` + `flow admit`, selects a policy-eligible coding worker, and runs admitted tasks unattended |
| Plan only (no execution) | `$flow plan-only: scope the next slice and write brief/spec/queue rows without running workers` | Surveys code, drafts `brief.md` / `spec.md` and queue rows, checks admission, and stops without launching workers |
| Status only (read-only) | `$flow-run status only — report current progress, blockers, and pending reviews without launching work` | Runs read-only inspection (`flow resume` / `flow progress`) and reports facts without dispatching workers or modifying state |
| Checkpoint / new session | `$flow checkpoint and hand off so I can continue in a fresh session` | Rebuilds `CONTEXT.md` (<= 200 lines) atomically via `flow checkpoint`; in the next session, say `continue` to run `flow resume` |
| Standalone design debate | `$model-debate <path/to/draft.md>` | Runs one blind multi-model review round by default, verifies claims against primary evidence, and synthesizes without editing code |

## 4. Essential terminal commands

All commands below accept `--repo <project-dir>` (use `.` inside the project):

```bash
# Read-only session recovery packet (CONTEXT, git, queue, gate, progress, publication, learning):
flow resume --repo <project-dir>

# Read-only bounded run progress, lock wait, and error snapshot:
flow progress --repo <project-dir>

# Read-only directed-learner inspection (never runs model calls or mutates queues):
flow learn status --repo <project-dir>
flow learn results --repo <project-dir>

# Pause learning for a registered project without altering task execution state:
flow learn pause --repo <project-dir>
```
