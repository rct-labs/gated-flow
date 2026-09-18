#!/usr/bin/env python3
"""
gate.py — the acceptance gate that a lying commit cannot pass.

Closes one specific hole observed in an earlier unattended orchestrator:
a commit that flips a task's status to DONE while touching only the queue
and handoff documents. Measured fake-completion rate before the fix: ~60%.

Design rules (see docs/design.md):
  - The verdict comes from a command's exit code, never from a model's claim.
  - The structural check is derived from the diff itself, so it cannot be
    forged: there is no field to lie in.
  - Everything else is auditable after the fact with one command.

Stdlib only. Python 3.10+. Windows-safe (no python3 assumption, UTF-8 I/O).

Subcommands
  init          write .gate/config.json into a repo, with sane defaults
  install-hook  install .git/hooks/pre-commit that calls `check-commit`
  check-commit  fast structural gate, run by the pre-commit hook
  verify        slow gate: run the acceptance command (--task ID for a task's
                admitted local check, --queue for the full oracle), record a verdict
  audit         re-derive the structural verdict over the last N commits
  admit         sweet-spot admission report for every TODO task in the queue
  usage         probe every worker / judge CLI now; bench the quota-dead ones
  doctor        report what is configured and whether it is usable
  run           unattended loop: probe → dispatch → verify → judge → revise
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

GATE_DIR = ".gate"
CONFIG_NAME = "config.json"
VERDICT_NAME = "verdict.json"

# Windows consoles default to a legacy code page; without this the report is
# mojibake and a blocked commit's reason becomes unreadable.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

DEFAULT_CONFIG = {
    # Repo-relative directory this project owns. Empty means the whole repo.
    # When set, a commit that touches the queue may not touch anything outside
    # it — the "don't drag my other projects into this history" gate.
    "project_prefix": "",
    "queue_file": "TASK_QUEUE.md",
    "context_file": "CONTEXT.md",
    "external_context": False,
    "handoff_globs": [],
    # A commit that only touches these is documentation. Flipping a task to
    # DONE while touching nothing else is the fake-completion pattern.
    "doc_only_globs": [
        "TASK_QUEUE.md",
        "NEXT_SESSION.md",
        "NEXT_SESSION.*.archive.md",
        "SESSION.md",
        "docs/handoff/*",
    ],
    # The acceptance oracle. Must exit non-zero when the requirement is violated.
    "verify_cmd": "uv run pytest tests -q",
    "verify_timeout_s": 1800,
    # How the oracle reports its count, for the non-vacuity check.
    "count_regex": r"(\d+)\s+passed",
    # Paths whose git diff must be empty during a refactor package.
    # Empty list disables the check.
    "frozen_globs": [],
    # A verdict older than this is not accepted by check-commit.
    "verdict_max_age_s": 5400,
    # Set false to let check-commit warn instead of block on a missing verdict.
    "require_verdict_on_done": True,
    "done_markers": ["DONE"],
    "todo_markers": ["TODO"],
    "blocked_markers": ["BLOCKED"],
    "in_progress_markers": ["IN_PROGRESS"],
    # ---- unattended run -------------------------------------------------
    # Writers are tried in order; a quota-dead one is skipped for a cooldown.
    "workers": ["codex", "claude", "grok"],
    # What one worker is asked to do. It should execute exactly one task and
    # stop — the loop belongs to this script, not to the model.
    "worker_prompt": (
        "Invoke the {skill} skill. Execute exactly task {task} from the queue "
        "— claim {task}, implement it, verify it, commit it — then stop. "
        "Never return while a test, verification, build, or child process is "
        "still running. Run acceptance commands in the foreground with an "
        "adequate timeout; if any command is backgrounded, wait for it and "
        "check its exit code before responding. "
        "Never select a different TODO task, even if {task} is no longer TODO. "
        "Do not start a second task. Do not push. If {task} is BLOCKED, stop "
        "immediately and report why instead of guessing. Tool, sandbox, Git, "
        "quota, and filesystem failures are environment failures, not task "
        "blockers: do not mark the task BLOCKED for them."
    ),
    "worker_skill": "",
    "max_tasks": 3,
    "max_attempts_per_task": 2,
    "task_timeout_s": 5400,
    "run_timeout_s": 28800,
    "quota_cooldown_s": 21600,
    # Shell command run on every milestone (task start / task end / run end).
    # {title} and {body} are filled in. Empty disables notification.
    "notify_cmd": "",
    # How often a still-running worker writes a heartbeat, so a long task and a
    # hung one look different from the outside.
    "heartbeat_s": 300,
    # ---- admission ------------------------------------------------------
    # The third leg of the design: exit-code verdicts and the diff
    # guard catch a bad attempt, admission keeps out-of-sweet-spot tasks
    # (small / isolated / implicitly verifiable) out of the loop entirely.
    # Scope is declared per task in the queue file, on its own line:
    #   <!-- task:WP-7 files: src/a.py, tests/test_a.py -->
    "max_task_files": 6,
    # A queue row matching this is a decision, not a task; it needs a human.
    # The first three alternatives are the Chinese words for design / decide /
    # review, written as escapes so the source stays ASCII.
    "human_decision_regex": "\u8bbe\u8ba1|\u51b3\u5b9a|\u8bc4\u5ba1|design|decide|choose",
    # False: a refused head task is a warning. True: it stops the run.
    # Flip to true only after one real project's advisory report has been
    # human-checked for false refusals.
    "strict_admit": False,
    # ---- autonomy (docs/autonomy.md) ---------------------------------------
    # Probe every candidate CLI once per run, before the first dispatch, with a
    # tiny real request. A quota-shaped answer benches it for quota_cooldown_s;
    # a probe that never reached the provider (timeout, not installed, other
    # error) benches it for retry_s only — it must not occupy the quota window.
    "probe": {"enabled": True, "timeout_s": 90, "retry_s": 600},
    # Judge every closed task with a read-only headless model, then iterate at
    # most max_revisions times (initial + 2 = 3 rounds: the point where the
    # per-round gain of self-correction drops under 2%). The judge never edits
    # the queue: DONE is earned by the exit-code gate and stays.
    "judge": {
        "enabled": False,
        "chain": ["fable", "opus", "codex"],
        "pass_score": 85,
        "max_revisions": 2,
        "min_gain": 5,
        "timeout_s": 1200,
    },
    "judge_prompt": (
        "You are the acceptance judge for ONE finished task in this repository. "
        "Task {task} was just closed by commit {commit}. Declared scope: {files}. "
        "Commits of this task since {base}: {commits}. Last acceptance run tail:\n"
        "{verify_tail}\n\n"
        "Read CONTEXT.md and TASK_QUEUE.md (repo root); locate the work package under "
        "docs/work/ whose PLAN.md or spec.md names {task} and read its acceptance for "
        "this task. Inspect the diff (`git show <sha>`) and the tests it added or "
        "changed. Do not modify anything.\n\n"
        "Score 0-100 with this rubric: acceptance criteria met 40; tests are real and "
        "would fail without the change 20; scope discipline (only declared files, no "
        "unrelated edits, queue row untouched apart from status/counts) 15; project "
        "conventions and code quality 15; regression risk 10.\n"
        "verdict: 'pass' when score >= {pass_score} and no high-severity finding; "
        "'revise' when the shortfall is fixable inside the declared scope by one worker "
        "session; 'escalate' when the spec is ambiguous, the fix needs a human decision, "
        "or the scope must grow. findings: concrete, file-anchored, each with a fix. "
        "revision_brief: the exact instructions a worker needs, or empty on pass.\n"
        "Return ONLY JSON matching the schema."
    ),
    "revision_prompt": (
        "You are revising ONE task in this repository after an acceptance review. "
        "Task {task} is already DONE in the queue and must stay DONE: do not edit its "
        "queue row or any other row. Fix exactly the findings below, inside the declared "
        "scope {files}; do not widen scope, do not weaken or delete tests. Run the "
        "project's acceptance command in the foreground and make it pass, then commit "
        "only the paths you changed (never `git add -A`, never `--no-verify`, never "
        "push). If a finding cannot be fixed within scope, leave it and say so.\n\n"
        "Reviewer brief: {brief}\n\nFindings:\n{findings}\n"
    ),
    # Declared files matching these are irreversible for this project: the
    # task needs an explicit `<!-- task:ID approved: <who/date> -->` line or the
    # run stops with needs_approval, even in advisory admission mode.
    "irreversible_globs": [
        "drizzle/**",
        "data/**",
        ".git/hooks/**",
        ".claude/settings*",
        "scripts/apply-*",
    ],
}

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "verdict": {"type": "string", "enum": ["pass", "revise", "escalate"]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    "file": {"type": "string"},
                    "issue": {"type": "string"},
                    "fix": {"type": "string"},
                },
                # Codex strict structured output requires every property here.
                # Empty strings represent findings without a location or fix.
                "required": ["severity", "file", "issue", "fix"],
                "additionalProperties": False,
            },
        },
        "revision_brief": {"type": "string"},
    },
    "required": ["score", "verdict", "findings", "revision_brief"],
    "additionalProperties": False,
}

# Read-only judge contracts. The chain is fable → opus → codex; fable is allowed
# here because the judge only reads. {schema} is the inline JSON schema,
# {schema_file} / {out_file} are paths for CLIs that take files.
JUDGE_CMDS: dict[str, list[str]] = {
    "fable": [
        "claude", "-p", "{prompt}",
        "--model", "claude-fable-5-1",
        "--output-format", "json", "--json-schema", "{schema}",
        "--allowedTools",
        "Read,Grep,Glob,Bash(git diff:*),Bash(git show:*),Bash(git log:*),Bash(git status:*)",
        "--max-budget-usd", "5",
    ],
    "opus": [
        "claude", "-p", "{prompt}",
        "--model", "opus",
        "--output-format", "json", "--json-schema", "{schema}",
        "--allowedTools",
        "Read,Grep,Glob,Bash(git diff:*),Bash(git show:*),Bash(git log:*),Bash(git status:*)",
        "--max-budget-usd", "5",
    ],
    "codex": [
        "codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check",
        "-C", "{projdir}", "--output-schema", "{schema_file}", "-o", "{out_file}", "-",
    ],
}

# Which installed CLI a judge chain member runs on, for probing.
JUDGE_TOOL: dict[str, str] = {"fable": "claude", "opus": "claude", "codex": "codex"}

# A worker that cannot be flag-restricted to read-only is still safe to use as
# a writer here: every commit it makes passes through the pre-commit gate, and
# the scope gate keeps it inside the project prefix.
WORKER_CMDS: dict[str, list[str]] = {
    # The gate contract requires every worker to make queue-only claim and
    # completion commits. Codex workspace-write deliberately protects .git as
    # read-only, so it cannot satisfy that contract. The hook and scope gate
    # remain the enforcement boundary; Codex therefore needs full filesystem
    # access here, just like the other writer CLIs.
    "codex": [
        "codex", "exec", "--sandbox", "danger-full-access",
        "--skip-git-repo-check", "-C", "{projdir}", "-",
    ],
    # Queue workers never run on fable: fable is the host/judge model and a
    # headless `claude -p` would silently inherit whatever the user last set as
    # their CLI default. Pin opus here; ensure_worker_model() below enforces the
    # same rule on any config override.
    "claude": [
        "claude", "-p", "{prompt}",
        "--model", "opus",
        "--permission-mode", "acceptEdits",
        "--allowedTools", "Read,Edit,Write,Bash,Glob,Grep,TaskOutput",
        "--output-format", "text",
    ],
    "grok": [
        "grok", "--prompt-file", "{prompt_file}",
        "--permission-mode", "auto", "--max-turns", "120",
        "--output-format", "plain", "--cwd", "{projdir}",
    ],
    "kimi": ["kimi", "-p", "{prompt}", "--output-format", "text"],
}

# Models that may only host and judge, never execute queue tasks. Matched
# case-insensitively as a substring of the --model value.
FORBIDDEN_WORKER_MODELS = ("fable",)
DEFAULT_CLAUDE_WORKER_MODEL = "opus"


def ensure_worker_model(worker: str, template: list[str]) -> tuple[list[str], str | None]:
    """Enforce the worker-model policy on a resolved command template.

    For the claude worker: inject ``--model opus`` when no --model is given, and
    refuse outright when --model names a forbidden (host-only) model. Other
    workers are returned unchanged. Returns (template, error).
    """
    if worker != "claude":
        return template, None
    argv = list(template)
    for i, part in enumerate(argv):
        if part == "--model" and i + 1 < len(argv):
            value = argv[i + 1]
            if any(bad in value.lower() for bad in FORBIDDEN_WORKER_MODELS):
                return argv, (
                    f"claude worker --model {value!r} is a host-only model; "
                    f"queue tasks may run on opus/codex/kimi/grok only"
                )
            return argv, None
        if part.startswith("--model="):
            value = part.split("=", 1)[1]
            if any(bad in value.lower() for bad in FORBIDDEN_WORKER_MODELS):
                return argv, (
                    f"claude worker --model {value!r} is a host-only model; "
                    f"queue tasks may run on opus/codex/kimi/grok only"
                )
            return argv, None
    # No --model at all: never inherit the CLI default, pin the worker model.
    argv[1:1] = ["--model", DEFAULT_CLAUDE_WORKER_MODEL]
    return argv, None

# Signatures that mean "this tool is unusable right now", not "this task
# failed". Both classes get the same treatment — bench the tool, do not spend
# the task's attempt budget, fall through to the next worker — because both are
# outages of the tool rather than verdicts on the work.
#
# The overload half was added after a real run burned both attempts on
# `API Error: 529 Overloaded.` and reported not_done for work that was in
# fact complete. A server-side 5xx is not a rejected commit.
# Patterns are matched case-insensitively as substrings, so they are kept
# distinctive: a bare "529" would false-match an incidental test count.
QUOTA_PATTERNS = [
    "usage limit",
    "billing cycle",
    "quota",
    "rate_limit",
    "rate limit",
    "429",
    "insufficient_quota",
    # prepaid balance / billing exhaustion: a 402 is the same outage as a 429,
    # not a rejected commit. Observed once: grok returned
    # `API error (status 402 Payment Required): Grok Build usage balance
    # exhausted`, matched nothing here, burned a task's second attempt and
    # ended the run as not_done. A bare "402" would false-match a test count.
    "status 402",
    "error: 402",
    "error 402",
    "402 payment required",
    "balance exhausted",
    "insufficient balance",
    "insufficient credit",
    "credit balance is too low",
    "out of credits",
    # transient server-side outages
    "overloaded",
    "error: 529",
    "error 529",
    "status 529",
    "error: 503",
    "error 503",
    "service unavailable",
    "internal server error",
    "bad gateway",
    "gateway timeout",
]

# Codex can exit zero even when a model-issued Git command failed. Match the
# actual tool transcript, not only the CLI process return code. Deliberately do
# not match "File exists": a stale index.lock is repository state and must be
# inspected, never deleted automatically.
GIT_WRITE_DENIAL_RE = re.compile(
    r"fatal:\s+unable to create ['\"][^'\"]*[/\\]\.git[/\\]index\.lock['\"]:\s*"
    r"(?:permission denied|access is denied|read-only file system)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------- utilities


def die(msg: str, code: int = 1) -> None:
    print(f"GATE: {msg}", file=sys.stderr)
    raise SystemExit(code)


def ok(msg: str) -> None:
    print(f"GATE PASS: {msg}")


def fail(msg: str, detail: str = "") -> None:
    print(f"GATE BLOCK: {msg}", file=sys.stderr)
    if detail:
        print(detail, file=sys.stderr)
    raise SystemExit(1)


def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        die(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def repo_root(start: Path) -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(start),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if out.returncode != 0:
        die(f"{start} is not inside a git repository")
    return Path(out.stdout.strip())


def load_config(repo: Path) -> dict:
    # GATE_CONFIG lets you point the gate at a repo without writing into it —
    # useful for auditing a project before you decide to install the hook.
    env = os.environ.get("GATE_CONFIG")
    path = Path(env) if env else repo / GATE_DIR / CONFIG_NAME
    if not path.exists():
        die(f"no config at {path}. Run: gate.py init --repo {repo}")
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(json.loads(path.read_text(encoding="utf-8")))
    return cfg


def continuous_quality_script() -> Path | None:
    """Locate the optional provider-neutral continuous-quality runner."""
    explicit = os.environ.get("CONTINUOUS_QUALITY_SCRIPT")
    candidates = [Path(explicit)] if explicit else []
    home = Path.home()
    candidates.extend(
        [
            home / ".agents" / "skills" / "continuous-quality" / "scripts" / "continuous_quality.py",
            home / ".codex" / "skills" / "continuous-quality" / "scripts" / "continuous_quality.py",
            home / ".claude" / "skills" / "continuous-quality" / "scripts" / "continuous_quality.py",
            home / ".grok" / "skills" / "continuous-quality" / "scripts" / "continuous_quality.py",
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def quality_phase(repo: Path, cfg: dict, phase: str) -> dict | None:
    """Run the optional quality hook and return only process-level evidence.

    The shared runner owns policy and project semantics. Gate knows the phase,
    command exit code, and a bounded output tail only.
    """
    quality = cfg.get("quality")
    if quality is None:
        return None
    if not isinstance(quality, dict):
        return {
            "result": "FAIL",
            "exit_code": 1,
            "tail": "quality must be an object",
        }
    if quality.get("enabled") is False:
        return None
    script = continuous_quality_script()
    if script is None:
        return {
            "result": "FAIL",
            "exit_code": 127,
            "tail": "continuous-quality runner not found under a shared skill directory",
        }
    proc = subprocess.run(
        [sys.executable, str(script), "check", "--repo", str(repo), "--phase", phase],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    return {
        "result": "PASS" if proc.returncode == 0 else "FAIL",
        "exit_code": proc.returncode,
        "tail": "\n".join(output.strip().splitlines()[-20:])[-4000:],
    }


def matches_any(path: str, globs: list[str]) -> bool:
    norm = path.replace("\\", "/")
    return any(fnmatch.fnmatch(norm, g) for g in globs)


def prefixed(cfg: dict, rel: str) -> str:
    """Resolve a project-relative path to a repo-relative one."""
    pre = (cfg.get("project_prefix") or "").strip("/")
    return f"{pre}/{rel}" if pre else rel


def in_project(cfg: dict, path: str) -> bool:
    pre = (cfg.get("project_prefix") or "").strip("/")
    if not pre:
        return True
    return path.replace("\\", "/").startswith(pre + "/")


def project_rel(cfg: dict, path: str) -> str:
    pre = (cfg.get("project_prefix") or "").strip("/")
    norm = path.replace("\\", "/")
    return norm[len(pre) + 1 :] if pre and norm.startswith(pre + "/") else norm


# ------------------------------------------------------------ queue parsing

# | 5 | WP-3a | name | `DONE` | **484 passed** | **507 passed**（...） | ... |
ROW_RE = re.compile(r"^\s*\|\s*\d+\s*\|\s*([^|]+?)\s*\|.*$")
STATUS_RE = re.compile(r"`([A-Z_]+)`")


def parse_queue(text: str) -> dict[str, dict]:
    """Map task id -> {status, cells}. Tolerates prose around the table."""
    tasks: dict[str, dict] = {}
    for line in text.splitlines():
        m = ROW_RE.match(line)
        if not m:
            continue
        task_id = m.group(1).strip().strip("*` ")
        if not task_id or task_id in ("ID", "---"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        status = ""
        for cell in cells:
            sm = STATUS_RE.search(cell)
            if sm:
                status = sm.group(1)
                break
        if not status:
            continue
        tasks[task_id] = {"status": status, "cells": cells}
    return tasks


# <!-- task:WP-7 files: src/foo.py, tests/test_foo.py -->
TASK_FILES_RE = re.compile(r"<!--\s*task:(\S+)\s+files:\s*(.*?)\s*-->")


def parse_task_files(text: str) -> dict[str, list[str]]:
    """Map task id -> declared files, from scope-declaration comment lines."""
    out: dict[str, list[str]] = {}
    for m in TASK_FILES_RE.finditer(text):
        out[m.group(1)] = [f.strip() for f in m.group(2).split(",") if f.strip()]
    return out


# <!-- task:WP-8 after: WP-7 -->
TASK_AFTER_RE = re.compile(r"<!--\s*task:(\S+)\s+after:\s*(.*?)\s*-->")


def parse_task_after(text: str) -> dict[str, list[str]]:
    """Map task id -> ids it declares it runs after. A declared order lets two
    strictly sequential slices share a file (the second extends what the first
    created) without tripping the overlap refusal, which exists to stop
    *independent* tasks from stepping on each other. Added 2026-09-02."""
    out: dict[str, list[str]] = {}
    for m in TASK_AFTER_RE.finditer(text):
        out[m.group(1)] = [f.strip() for f in m.group(2).split(",") if f.strip()]
    return out


# <!-- task:WP-9 approved: 2026-09-03 user -->
TASK_APPROVED_RE = re.compile(r"<!--\s*task:(\S+)\s+approved:\s*(.*?)\s*-->")
# One task-local acceptance command per task, admitted in the queue itself:
#   <!-- task:WP-7 verify: {"cmd": "pytest tests/test_a.py -q", "timeout_s": 900} -->
# Workers run `gate.py verify --task WP-7`; the full verify_cmd runs once per
# run, after the package review. A row without a declaration uses verify_cmd.
TASK_VERIFY_RE = re.compile(r"<!--\s*task:([\w.-]+)\s+verify:\s*(\{.*?\})\s*-->", re.S)


def parse_task_approved(text: str) -> dict[str, str]:
    """Map task id -> approval note. An irreversible task (declared files match
    irreversible_globs) needs one of these before the runner will dispatch it.
    Per task id, so one approval never carries over to the next task."""
    out: dict[str, str] = {}
    for m in TASK_APPROVED_RE.finditer(text):
        note = m.group(2).strip()
        if note:
            out[m.group(1)] = note
    return out


def parse_task_verify(text: str) -> dict[str, dict]:
    """{task_id: {"cmd": str, "timeout_s": int}} for every declared local check."""
    out: dict[str, dict] = {}
    for tid, raw in TASK_VERIFY_RE.findall(text):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        cmd = value.get("cmd") if isinstance(value, dict) else None
        if not isinstance(cmd, str) or not cmd.strip():
            continue
        timeout = value.get("timeout_s", 900)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
            timeout = 900
        out[tid] = {"cmd": cmd.strip(), "timeout_s": timeout}
    return out


def sub_cfg(cfg: dict, key: str) -> dict:
    """Nested config block with defaults filled in. load_config() replaces a
    top-level key wholesale, so a project that sets only judge.enabled must
    still get the default chain and thresholds."""
    merged = dict(DEFAULT_CONFIG.get(key) or {})
    value = cfg.get(key)
    if isinstance(value, dict):
        merged.update(value)
    return merged


def irreversible_files(cfg: dict, files: list[str]) -> list[str]:
    globs = cfg.get("irreversible_globs") or []
    return [f for f in files if matches_any(f, globs)]


WORKER_COLUMN = "worker"
NO_PIN_CELLS = {"", "-", "—", "–"}


def _is_separator_row(cells: list[str]) -> bool:
    return all(set(c) <= set("-: ") for c in cells)


def parse_task_workers(text: str) -> dict[str, str]:
    """Map task id -> pinned worker id, from an optional `worker` column.

    The column index is per table, not per file: a header row that names the
    column sets it, a header row without it clears it, so the many older tables
    in a long queue file stay unpinned. An empty / dash cell means no pin.
    Added 2026-09-02 so one run can route each task to the CLI that suits it
    (docs to kimi, mechanical implementation to codex, cross-package design to
    claude) instead of one run per worker."""
    out: dict[str, str] = {}
    idx: int | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if _is_separator_row(cells):
            continue
        m = ROW_RE.match(line)
        if not m:
            # A header row. It either declares the column or resets it.
            lowered = [c.lower().strip("*` ") for c in cells]
            idx = lowered.index(WORKER_COLUMN) if WORKER_COLUMN in lowered else None
            continue
        if idx is None or idx >= len(cells):
            continue
        task_id = m.group(1).strip().strip("*` ")
        value = cells[idx].strip("*` ").lower()
        if not task_id or value in NO_PIN_CELLS:
            continue
        out[task_id] = value
    return out


def known_workers(cfg: dict) -> set[str]:
    return set(WORKER_CMDS) | set(cfg.get("worker_cmds") or {})


def blob(repo: Path, ref: str, path: str) -> str | None:
    """Content of `path` at `ref`. Use ':' for the staged (index) version."""
    spec = f":{path}" if ref == ":" else f"{ref}:{path}"
    proc = subprocess.run(
        ["git", "show", spec],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def newly_done(repo: Path, cfg: dict, before_ref: str, after_ref: str) -> list[str]:
    """Task ids whose status became a done-marker between two refs."""
    qf = prefixed(cfg, cfg["queue_file"])
    before = blob(repo, before_ref, qf)
    after = blob(repo, after_ref, qf)
    if after is None:
        return []
    old = parse_queue(before) if before else {}
    new = parse_queue(after)
    markers = set(cfg["done_markers"])
    flipped = []
    for tid, row in new.items():
        was = old.get(tid, {}).get("status", "")
        if row["status"] in markers and was not in markers:
            flipped.append(tid)
    return flipped


def end_baseline_for(repo: Path, cfg: dict, ref: str, task_id: str) -> int | None:
    text = blob(repo, ref, prefixed(cfg, cfg["queue_file"]))
    if not text:
        return None
    row = parse_queue(text).get(task_id)
    if not row:
        return None
    # The end baseline is the last cell containing a "<n> passed"-shaped number.
    found = None
    for cell in row["cells"]:
        m = re.search(cfg["count_regex"], cell)
        if m:
            found = int(m.group(1))
    return found


# ------------------------------------------------------------ verdict store


def verdict_path(repo: Path) -> Path:
    return repo / GATE_DIR / VERDICT_NAME


def write_verdict(repo: Path, payload: dict) -> None:
    p = verdict_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def read_verdict(repo: Path) -> dict | None:
    p = verdict_path(repo)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def files_fingerprint(repo: Path, cfg: dict, files: list[str]) -> str:
    """Hash of the working-tree bytes of the declared files, so a task verdict
    cannot outlive the code it measured. Staging, committing and edits to
    other files do not change it; editing a declared file does."""
    import hashlib

    h = hashlib.sha256()
    for rel in sorted(files):
        p = repo / prefixed(cfg, rel)
        h.update(rel.encode())
        h.update(p.read_bytes() if p.is_file() else b"<missing>")
    return h.hexdigest()[:16]


# -------------------------------------------------------------- subcommands


def cmd_init(args) -> None:
    repo = repo_root(Path(args.repo).resolve())
    d = repo / GATE_DIR
    d.mkdir(parents=True, exist_ok=True)
    cfgp = d / CONFIG_NAME
    if cfgp.exists() and not args.force:
        die(f"{cfgp} exists. Use --force to overwrite.")
    cfgp.write_text(
        json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    gi = d / ".gitignore"
    gi.write_text("verdict.json\n*.tmp\n", encoding="utf-8")
    print(f"wrote {cfgp}")
    print(f"wrote {gi}  (the verdict is local evidence, not repo content)")
    print("Review verify_cmd and frozen_globs before installing the hook.")


# The gate needs only the standard library, so it deliberately runs on a plain
# interpreter rather than the project's uv-managed venv — a venv that is being
# rebuilt must never be able to block a commit. Interpreters are tried in order;
# if none exists the hook warns and allows the commit, because a missing
# interpreter is not evidence of a fake completion, and bricking every commit in
# a repo that hosts several projects is the worse failure. `gate.py audit` still
# sees anything that slipped through.
HOOK_TEMPLATE = """#!/bin/sh
# installed by gate.py — blocks the fake-completion pattern.
# Bypass in an emergency with: git commit --no-verify   (and say so in the message)
GATE="{gate}"
for PY in "{python}" python py python3; do
    if [ "$PY" = "py" ]; then
        command -v py >/dev/null 2>&1 && exec py -3 "$GATE" check-commit --repo "{repo}"
    elif command -v "$PY" >/dev/null 2>&1 || [ -x "$PY" ]; then
        exec "$PY" "$GATE" check-commit --repo "{repo}"
    fi
done
echo "GATE WARNING: no Python interpreter found; the acceptance gate did not run." >&2
echo "GATE WARNING: re-check this commit later with: gate.py audit" >&2
exit 0
"""


def cmd_install_hook(args) -> None:
    repo = repo_root(Path(args.repo).resolve())
    hooks = Path(git(repo, "rev-parse", "--git-path", "hooks").strip())
    if not hooks.is_absolute():
        hooks = repo / hooks
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-commit"
    if hook.exists() and not args.force:
        existing = hook.read_text(encoding="utf-8", errors="replace")
        if "gate.py" in existing and "check-commit" in existing:
            print(f"already installed {hook}")
            return
        die(
            f"{hook} exists and is not a direct gate hook; refusing to overwrite it. "
            "If it is managed by pre-commit, add a local gate-check-commit entry "
            "to .pre-commit-config.yaml and re-run doctor. Use --force only when "
            "you explicitly intend to replace the existing hook."
        )
    body = HOOK_TEMPLATE.format(
        python=sys.executable.replace("\\", "/"),
        gate=str(Path(__file__).resolve()).replace("\\", "/"),
        repo=str(repo).replace("\\", "/"),
    )
    hook.write_text(body, encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    print(f"installed {hook}")


def hook_installation(repo: Path) -> tuple[bool, str]:
    """Return whether gate is enforced directly or through pre-commit.

    A pre-commit-generated shell hook intentionally does not mention gate.py;
    the project config is the auditable chain in that setup.  Requiring both a
    known local hook id and the gate command avoids treating an arbitrary
    pre-commit installation as gate enforcement.
    """
    hooks = Path(git(repo, "rev-parse", "--git-path", "hooks").strip())
    if not hooks.is_absolute():
        hooks = repo / hooks
    hook = hooks / "pre-commit"
    if not hook.exists():
        return False, f"{hook} missing"
    body = hook.read_text(encoding="utf-8", errors="replace")
    if "gate.py" in body and "check-commit" in body:
        return True, "direct"
    pcfg = repo / ".pre-commit-config.yaml"
    if "pre-commit" in body and pcfg.exists():
        cfg_text = pcfg.read_text(encoding="utf-8", errors="replace")
        chained = (
            "id: gate-check-commit" in cfg_text
            and "gate.py" in cfg_text
            and "check-commit" in cfg_text
        )
        if chained:
            return True, "pre-commit-chain"
        return False, "pre-commit installed, gate-check-commit chain missing"
    return False, "existing hook is not gate-managed"


def cmd_check_commit(args) -> None:
    """Fast, structural, and unforgeable: derived from the staged diff itself."""
    repo = repo_root(Path(args.repo).resolve())
    cfg = load_config(repo)

    staged = [
        p for p in git(repo, "diff", "--cached", "--name-only").splitlines() if p.strip()
    ]
    if not staged:
        ok("no staged changes")
        return

    quality = quality_phase(repo, cfg, "commit")
    if quality is not None and quality["result"] != "PASS":
        fail(
            "required continuous-quality commit checks failed.",
            quality["tail"],
        )

    has_head = git(repo, "rev-parse", "--verify", "HEAD", check=False).strip() != ""
    before = "HEAD" if has_head else None
    flipped = newly_done(repo, cfg, before or "HEAD", ":") if before else []

    queue_staged = prefixed(cfg, cfg["queue_file"]) in [
        p.replace("\\", "/") for p in staged
    ]

    # --- Gate 0: scope. A commit that touches this project's queue may not
    # carry paths from elsewhere in the repo. This is the "don't drag my other
    # projects into this history" rule, enforced instead of merely written down.
    if queue_staged and (cfg.get("project_prefix") or "").strip("/"):
        outside = [p for p in staged if not in_project(cfg, p)]
        if outside:
            fail(
                "commit touches the task queue and paths outside "
                f"'{cfg['project_prefix']}'.",
                "Outside paths:\n  "
                + "\n  ".join(outside)
                + "\n\nStage this project's paths only. Never `git add -A` in a "
                "repo that hosts several projects.",
            )

    if not flipped:
        ok(f"{len(staged)} staged path(s); no task flipped to DONE")
        return

    # --- Gate 1: fake completion. A DONE flip needs code, not just documents.
    code_paths = [
        p for p in staged if not matches_any(project_rel(cfg, p), cfg["doc_only_globs"])
    ]
    if not code_paths:
        fail(
            f"fake completion — task(s) {', '.join(flipped)} flipped to DONE "
            f"but the commit touches only documentation.",
            "Staged paths:\n  "
            + "\n  ".join(staged)
            + "\n\nThis is the fake-completion pattern (measured at ~60% of tasks in the\n"
            "orchestrator this gate replaced).\n"
            "A task is done when the code changed, not when the queue says so.",
        )

    # --- Gate 2: frozen paths must not move.
    frozen = cfg.get("frozen_globs") or []
    if frozen:
        violating = [p for p in staged if matches_any(project_rel(cfg, p), frozen)]
        if violating:
            fail(
                "frozen path modified in a DONE commit: " + ", ".join(violating),
                "frozen_globs in .gate/config.json forbids changing these while "
                "closing a task. Weakening the oracle is how a loop converges on "
                "rewriting its own tests.",
            )

    # --- Gate 3: a fresh, matching verdict from the real acceptance command.
    if cfg.get("require_verdict_on_done", True):
        v = read_verdict(repo)
        if not v:
            fail(
                f"no verdict recorded for {', '.join(flipped)}.",
                "Run the acceptance command through the gate first:\n"
                f"  python {Path(__file__).name} verify --repo {repo}",
            )
        age = time.time() - float(v.get("at_epoch", 0))
        if age > float(cfg["verdict_max_age_s"]):
            fail(
                f"verdict is {int(age)}s old (limit {cfg['verdict_max_age_s']}s).",
                "Re-run: gate.py verify",
            )
        if v.get("result") != "PASS":
            fail(
                f"last verdict is {v.get('result')}, not PASS.",
                json.dumps(v, indent=2, ensure_ascii=False),
            )
        if v.get("scope") == "task":
            # A task-local PASS closes exactly the task it measured, and only
            # while the declared files still hold the bytes that were tested.
            if v.get("task") not in flipped or len(flipped) != 1:
                fail(
                    f"verdict is for task {v.get('task')} but this commit flips "
                    f"{', '.join(flipped)}.",
                    "Run: gate.py verify --task <id> for the task being closed.",
                )
            files = list(v.get("files") or [])
            if files and files_fingerprint(repo, cfg, files) != v.get("fingerprint"):
                fail(
                    f"declared files of {v['task']} changed after its local check.",
                    "Re-run: gate.py verify --task " + str(v["task"]),
                )
        else:
            expected = end_baseline_for(repo, cfg, ":", flipped[0])
            if expected is not None and v.get("count") is not None:
                if int(v["count"]) != int(expected):
                    fail(
                        f"queue declares end baseline {expected} for {flipped[0]} "
                        f"but the acceptance run measured {v['count']}.",
                        "The number in the queue is a claim; the number from the "
                        "command is the fact. Fix whichever is wrong.",
                    )

    ok(
        f"task(s) {', '.join(flipped)} → DONE with {len(code_paths)} code path(s) "
        f"and a fresh PASS verdict"
    )


def task_verify_spec(repo: Path, cfg: dict, task: str) -> tuple[str, int, list[str]]:
    """(command, timeout, declared files) for a task: its admitted local check
    when the queue declares one, otherwise the full oracle."""
    qpath = repo / prefixed(cfg, cfg["queue_file"])
    text = qpath.read_text(encoding="utf-8") if qpath.exists() else ""
    files = parse_task_files(text).get(task) or []
    decl = parse_task_verify(text).get(task)
    if decl:
        return decl["cmd"], decl["timeout_s"], files
    return cfg["verify_cmd"], int(cfg["verify_timeout_s"]), files


def cmd_verify(args) -> None:
    """Run the real acceptance command and record what it actually said.

    `--task ID` runs that task's admitted local check (scope "task");
    `--queue` or no flag runs the full oracle (scope "queue")."""
    repo = repo_root(Path(args.repo).resolve())
    cfg = load_config(repo)
    if args.task:
        cmd, timeout, files = task_verify_spec(repo, cfg, args.task)
        payload = run_acceptance(repo, cfg, args.cmd or cmd, task=args.task,
                                 timeout_s=timeout, files=files)
    else:
        payload = run_acceptance(repo, cfg, args.cmd or cfg["verify_cmd"])
    print(
        f"GATE: {payload['result']} exit={payload['exit_code']} "
        f"count={payload['count']} in {payload['elapsed_s']}s"
        + (f" [task {payload['task']}]" if payload.get("task") else "")
    )
    if payload["result"] != "PASS":
        print(payload["tail"], file=sys.stderr)
        raise SystemExit(2)


def run_acceptance(repo: Path, cfg: dict, cmd: str | None = None, *,
                   task: str | None = None, timeout_s: int | None = None,
                   files: list[str] | None = None) -> dict:
    """Run an acceptance command, write the verdict, return it. Shared by
    `verify` and by the runner's own full acceptance at the end of a run.
    A task-scoped verdict records the task and a fingerprint of its declared
    files; a queue-scoped one records the full oracle."""
    cmd = cmd or cfg["verify_cmd"]
    # The oracle runs where the project lives, not at the repo root — a repo
    # can host several projects.
    workdir = repo / (cfg.get("project_prefix") or "")

    print(f"GATE: running acceptance command in {workdir}: {cmd}")
    started = time.time()
    proc = subprocess.run(
        cmd,
        cwd=str(workdir),
        shell=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s or cfg["verify_timeout_s"],
    )
    elapsed = round(time.time() - started, 1)
    output = (proc.stdout or "") + (proc.stderr or "")
    quality = quality_phase(repo, cfg, "task")
    if quality is not None:
        output += "\n" + quality["tail"]
    tail = "\n".join(output.strip().splitlines()[-15:])

    count = None
    m = None
    for m in re.finditer(cfg["count_regex"], output):
        pass
    if m:
        count = int(m.group(1))

    result = "PASS" if proc.returncode == 0 else "FAIL"
    if quality is not None and quality["result"] != "PASS":
        result = "FAIL"

    # Non-vacuity: an oracle that asserts nothing is not an oracle.
    if result == "PASS" and count == 0:
        result = "VACUOUS"

    payload = {
        "result": result,
        "exit_code": proc.returncode,
        "count": count,
        "cmd": cmd,
        "elapsed_s": elapsed,
        "at_epoch": time.time(),
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "head": git(repo, "rev-parse", "HEAD", check=False).strip(),
        "scope": "task" if task else "queue",
        "task": task,
        "files": sorted(files or []),
        "fingerprint": files_fingerprint(repo, cfg, files or []) if task else "",
        "tail": tail,
        "quality": quality,
    }
    write_verdict(repo, payload)
    return payload


def cmd_audit(args) -> None:
    """Re-derive the structural verdict over history. Forgery shows up here."""
    repo = repo_root(Path(args.repo).resolve())
    cfg = load_config(repo)
    n = args.last
    shas = [s for s in git(repo, "log", f"-{n}", "--format=%H").splitlines() if s]

    findings = []
    for sha in shas:
        parent = git(repo, "rev-parse", f"{sha}^", check=False).strip()
        if not parent:
            continue
        flipped = newly_done(repo, cfg, parent, sha)
        if not flipped:
            continue
        names = [
            p
            for p in git(repo, "show", "--name-only", "--format=", sha).splitlines()
            if p.strip()
        ]
        code = [
            p
            for p in names
            if not matches_any(project_rel(cfg, p), cfg["doc_only_globs"])
        ]
        subject = git(repo, "log", "-1", "--format=%s", sha).strip()
        if not code:
            findings.append((sha[:10], ", ".join(flipped), subject, "FAKE_COMPLETION"))
        else:
            findings.append(
                (sha[:10], ", ".join(flipped), subject, f"ok ({len(code)} code paths)")
            )

    if not findings:
        print(f"GATE audit: no DONE transitions in the last {n} commits.")
        return

    print(f"GATE audit — DONE transitions in the last {n} commits:\n")
    bad = 0
    for sha, tasks, subject, verdict in findings:
        flag = "!!" if verdict == "FAKE_COMPLETION" else "  "
        if verdict == "FAKE_COMPLETION":
            bad += 1
        print(f"{flag} {sha}  {tasks:<12}  {verdict}")
        print(f"     {subject}")
    print(f"\n{bad} fake completion(s) out of {len(findings)} DONE transition(s).")
    if bad:
        raise SystemExit(1)


# ------------------------------------------------------------- the run loop


def journal(repo: Path, event: dict) -> None:
    p = repo / GATE_DIR / "journal.ndjson"
    p.parent.mkdir(parents=True, exist_ok=True)
    event = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **event}
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def tool_state(repo: Path) -> dict:
    p = repo / GATE_DIR / "tool-status.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def set_tool_state(repo: Path, state: dict) -> None:
    p = repo / GATE_DIR / "tool-status.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def head_task(cfg: dict, tasks: dict[str, dict]) -> tuple[str | None, str]:
    """Return (task_id, reason). Mirrors the queue's own selection rule."""
    for tid, row in tasks.items():
        st = row["status"]
        if st in cfg["in_progress_markers"]:
            return tid, "in_progress"
        if st in cfg["blocked_markers"]:
            return tid, "blocked"
        if st in cfg["todo_markers"]:
            return tid, "todo"
        # DONE and anything else: keep scanning.
    return None, "empty"


def admit_verdicts(
    cfg: dict,
    repo: Path,
    tasks: dict[str, dict],
    files_map: dict[str, list[str]],
    workers_map: dict[str, str] | None = None,
    after_map: dict[str, list[str]] | None = None,
    approved_map: dict[str, str] | None = None,
) -> dict[str, dict]:
    """Sweet-spot admission for every TODO task: small, isolated, implicitly
    verifiable. Deterministic — a refusal carries its reasons as data, there
    is no judgement call to argue with."""
    out: dict[str, dict] = {}
    todo = [t for t, r in tasks.items() if r["status"] in cfg["todo_markers"]]
    human_re = re.compile(cfg["human_decision_regex"], re.IGNORECASE)
    verdict = read_verdict(repo)
    workers_map = workers_map or {}
    after_map = after_map or {}
    approved_map = approved_map or {}
    known = known_workers(cfg)
    order = list(tasks)

    def ordered_pair(a: str, b: str) -> bool:
        return b in after_map.get(a, []) or a in after_map.get(b, [])

    for tid in todo:
        files = files_map.get(tid)
        if files is None:
            out[tid] = {
                "verdict": "undeclared-scope",
                "reason": f"no '<!-- task:{tid} files: … -->' line in the queue",
            }
            continue
        reasons = []
        if len(files) > cfg["max_task_files"]:
            reasons.append(
                f"too-broad: {len(files)} files > max_task_files={cfg['max_task_files']}"
            )
        overlaps = [
            f"{other} ({', '.join(sorted(set(files) & set(files_map.get(other) or [])))})"
            for other in todo
            if other != tid
            and set(files) & set(files_map.get(other) or [])
            and not ordered_pair(tid, other)
        ]
        if overlaps:
            reasons.append("overlaps: " + "; ".join(overlaps))
        for dep in after_map.get(tid, []):
            if dep not in tasks:
                reasons.append(f"unknown-dependency: {dep} is not in the queue")
            elif tasks[dep]["status"] not in cfg["done_markers"] and order.index(dep) > order.index(tid):
                reasons.append(f"out-of-order: {dep} must come before {tid} in the queue")
        if human_re.search(" ".join(tasks[tid]["cells"])):
            reasons.append("needs-human: row matches human_decision_regex")
        pin = workers_map.get(tid)
        if pin and pin not in known:
            reasons.append(
                f"unknown-worker: '{pin}' is not one of {', '.join(sorted(known))}"
            )
        if not cfg.get("verify_cmd"):
            reasons.append("no-oracle: verify_cmd is empty")
        elif verdict and verdict.get("result") == "VACUOUS":
            reasons.append("no-oracle: last verify was VACUOUS")
        # Reversibility tier: an irreversible task is never a judgement call
        # for the runner. Without an explicit per-task approval it is refused,
        # and cmd_run stops on it regardless of advisory mode.
        needs_approval = False
        irreversible = irreversible_files(cfg, files)
        if irreversible and tid not in approved_map:
            needs_approval = True
            reasons.append(
                "needs-approval: touches irreversible path(s) "
                + ", ".join(irreversible)
                + f"; add `<!-- task:{tid} approved: <who/date> -->`"
            )
        out[tid] = {
            "verdict": "refuse" if reasons else "admit",
            "reason": "; ".join(reasons),
            "needs_approval": needs_approval,
        }
    return out


def cmd_admit(args) -> None:
    repo = repo_root(Path(args.repo).resolve())
    cfg = load_config(repo)
    qpath = repo / prefixed(cfg, cfg["queue_file"])
    if not qpath.exists():
        die(f"queue not found: {qpath}")
    text = qpath.read_text(encoding="utf-8")
    pins = parse_task_workers(text)
    verdicts = admit_verdicts(
        cfg, repo, parse_queue(text), parse_task_files(text), pins,
        parse_task_after(text), parse_task_approved(text),
    )
    if not verdicts:
        print("admit: no TODO tasks in the queue.")
        return
    print(
        f"admission — sweet spot: small (≤{cfg['max_task_files']} files), "
        "isolated, verifiable, reversible-or-approved"
    )
    bad = 0
    for tid, v in verdicts.items():
        mark = {"admit": "ok", "refuse": "REFUSE", "undeclared-scope": "UNDECLARED"}[
            v["verdict"]
        ]
        if v.get("needs_approval"):
            mark = "APPROVAL"
        pin = f" [worker: {pins[tid]}]" if tid in pins else ""
        print(f"  {mark:10} {tid}{pin}" + (f" — {v['reason']}" if v["reason"] else ""))
        bad += v["verdict"] != "admit"
    mode = "strict" if cfg.get("strict_admit") else "advisory"
    print(f"{len(verdicts)} TODO task(s), {bad} not admissible. Mode: {mode}.")


def is_quota(text: str) -> bool:
    low = text.lower()
    return any(pat in low for pat in QUOTA_PATTERNS)


def tool_outage_reason(text: str, returncode: int = 1) -> str | None:
    """Classify worker/tool outages that are not verdicts on task work."""
    if GIT_WRITE_DENIAL_RE.search(text):
        return "git_write_denied"
    if returncode != 0 and is_quota(text):
        return "quota"
    return None


def disable_worker(repo: Path, cfg: dict, worker: str, reason: str) -> None:
    """Bench an unusable worker so retries rotate without burning attempts."""
    until = time.time() + cfg["quota_cooldown_s"]
    st = tool_state(repo)
    st[worker] = {
        "disabled_until": until,
        "reason": reason,
        "seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    set_tool_state(repo, st)


def _task_id_from_row(line: str) -> str | None:
    match = ROW_RE.match(line)
    if not match:
        return None
    task_id = match.group(1).strip().strip("*` ")
    return task_id if task_id and task_id not in ("ID", "---") else None


def restore_task_row_text(before: str, current: str, task_id: str) -> str:
    """Restore only one queue row, preserving concurrent edits to other rows."""
    before_line = next(
        (line for line in before.splitlines(keepends=True) if _task_id_from_row(line) == task_id),
        None,
    )
    if before_line is None:
        raise ValueError(f"task {task_id} was absent before worker dispatch")

    lines = current.splitlines(keepends=True)
    indexes = [i for i, line in enumerate(lines) if _task_id_from_row(line) == task_id]
    if len(indexes) != 1:
        raise ValueError(f"task {task_id} has {len(indexes)} queue rows after worker dispatch")
    lines[indexes[0]] = before_line
    return "".join(lines)


def queue_status_changes(before: str, after: str) -> dict[str, tuple[str, str]]:
    """Return every task whose parsed queue status changed."""
    old = parse_queue(before)
    new = parse_queue(after)
    changes: dict[str, tuple[str, str]] = {}
    for task_id in old.keys() | new.keys():
        prior = old.get(task_id, {}).get("status", "<missing>")
        current = new.get(task_id, {}).get("status", "<missing>")
        if prior != current:
            changes[task_id] = (prior, current)
    return changes


def non_gate_worktree_state(repo: Path) -> tuple[str, ...]:
    """Stable dirty-state snapshot excluding runner-owned .gate artifacts."""
    lines = git(repo, "status", "--porcelain=v1", "-uall").splitlines()
    return tuple(sorted(line for line in lines if line.strip() and GATE_DIR not in line))


INCOMPLETE_WORK_PATTERNS = (
    re.compile(r"\bstill running in (?:the )?background\b", re.IGNORECASE),
    re.compile(
        r"\brunning\s*[;,.:-]\s*waiting on (?:it|the (?:test|verification|build|acceptance))\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bwaiting on (?:it|them) before .*\b(?:commit|verdict)\b", re.IGNORECASE),
)


def worker_incomplete_reason(output: str) -> str | None:
    """Detect a one-shot worker returning while its acceptance work is pending."""
    if any(pattern.search(output) for pattern in INCOMPLETE_WORK_PATTERNS):
        return "background_work_pending"
    return None


def available_workers(repo: Path, cfg: dict, prefer: str | None = None) -> list[str]:
    """Usable workers in dispatch order. A per-task pin goes first even when it
    is not in the run-wide list; benched or uninstalled tools drop out, so a
    pinned task still falls through to the run-wide list on retry."""
    state = tool_state(repo)
    now = time.time()
    out = []
    candidates = ([prefer] if prefer else []) + list(cfg["workers"])
    seen: set[str] = set()
    for w in candidates:
        if w in seen:
            continue
        seen.add(w)
        info = state.get(w, {})
        if info.get("disabled_until", 0) > now:
            continue
        if not resolve_tool(w):
            continue
        out.append(w)
    return out


def resolve_tool(name: str) -> str | None:
    """Find the executable, including Windows shims a POSIX PATH lookup misses."""
    import shutil

    for candidate in (name, f"{name}.cmd", f"{name}.exe"):
        found = shutil.which(candidate)
        if found:
            return found
    for extra in (
        Path.home() / f".{name}" / "bin" / f"{name}.exe",
        Path.home() / f".{name}-code" / "bin" / f"{name}.exe",
        Path.home() / "AppData/Local/pnpm" / f"{name}.cmd",
        Path.home() / ".local/bin" / f"{name}.exe",
    ):
        if extra.exists():
            return str(extra)
    return None


def notify(cfg: dict, repo: Path, title: str, body: str = "") -> None:
    """Milestone notification. It must never be able to fail a run."""
    cmd = cfg.get("notify_cmd")
    if not cmd:
        return
    try:
        subprocess.run(
            cmd.replace("{title}", title).replace("{body}", body[:1500]),
            shell=True,
            cwd=str(repo),
            timeout=60,
            capture_output=True,
        )
    except Exception as exc:
        print(f"GATE: notification failed: {exc}", file=sys.stderr)


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill the worker and everything it started. A surviving grandchild would
    keep writing into the worktree after we declared the attempt over."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            capture_output=True,
        )
    else:
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except Exception:
            proc.kill()


class WorkerResult:
    """Just enough of CompletedProcess for the caller, plus what we streamed."""

    def __init__(self, returncode: int, output: str, timed_out: bool = False):
        self.returncode = returncode
        self.stdout = output
        self.stderr = ""
        self.timed_out = timed_out


def spawn_worker(
    repo: Path,
    cfg: dict,
    worker: str,
    prompt: str,
    prompt_file: Path,
    log_path: Path | None = None,
    on_beat=None,
    template: list[str] | None = None,
    timeout_s: float | None = None,
    extra: dict[str, str] | None = None,
):
    """Spawn one headless CLI process and stream it. `template` overrides the
    worker contract (used by the read-only judge and the usage probe, which
    are not queue workers and skip the worker-model policy); `timeout_s`
    overrides task_timeout_s; `extra` adds placeholder substitutions."""
    projdir = repo / (cfg.get("project_prefix") or "")
    exe = resolve_tool(worker)
    if not exe:
        return None, f"{worker} not found on PATH"
    if template is None:
        # worker_cmds in the config overrides the built-in contracts, so flags
        # can be tuned without editing this file.
        template = (cfg.get("worker_cmds") or {}).get(worker) or WORKER_CMDS.get(worker)
        if not template:
            return None, f"no invocation contract for {worker}"
        template, model_error = ensure_worker_model(worker, template)
        if model_error:
            return None, model_error

    argv = []
    for part in template:
        part = (
            part.replace("{projdir}", str(projdir))
            .replace("{prompt_file}", str(prompt_file))
            .replace("{prompt}", prompt)
        )
        for key, value in (extra or {}).items():
            part = part.replace("{" + key + "}", value)
        argv.append(part)
    argv[0] = exe

    # Codex reads its prompt from stdin: the Windows .cmd shim truncates a
    # multi-line argument at the first newline.
    stdin_text = prompt if worker == "codex" else None
    limit = float(timeout_s) if timeout_s else float(cfg["task_timeout_s"])

    # Stream rather than capture. A 56-minute task that writes nothing until it
    # exits is indistinguishable from a hung one, which is the whole complaint.
    log = open(log_path, "w", encoding="utf-8", buffering=1) if log_path else None
    proc = subprocess.Popen(
        argv,
        cwd=str(projdir),
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        start_new_session=(os.name != "nt"),
    )
    if stdin_text is not None:
        try:
            proc.stdin.write(stdin_text)
            proc.stdin.close()
        except Exception:
            pass

    import threading

    state = {"last_line": "", "lines": 0}
    chunks: list[str] = []

    def pump() -> None:
        for raw in proc.stdout:
            chunks.append(raw)
            state["lines"] += 1
            stripped = raw.strip()
            if stripped:
                state["last_line"] = stripped[:200]
            if log:
                log.write(raw)

    t = threading.Thread(target=pump, daemon=True)
    t.start()

    started = time.time()
    last_beat = started
    timed_out = False
    beat_every = max(30, int(cfg.get("heartbeat_s", 300)))

    while proc.poll() is None:
        time.sleep(2)
        now = time.time()
        if now - started > limit:
            kill_tree(proc)
            timed_out = True
            break
        if on_beat and now - last_beat >= beat_every:
            last_beat = now
            on_beat(int(now - started), state["lines"], state["last_line"])

    t.join(timeout=10)
    if log:
        log.close()
    rc = proc.returncode if proc.returncode is not None else -1
    return WorkerResult(rc, "".join(chunks), timed_out), None


# ------------------------------------------------------------ usage probe

PROBE_PROMPT = "Reply with the single word OK and nothing else. Do not use any tool."


def probe_worker(repo: Path, cfg: dict, worker: str, run_dir: Path | None = None) -> tuple[bool, str]:
    """One tiny real request through the worker's own contract. Returns
    (usable, reason). Only a quota-shaped answer is a quota outage; a probe
    that never reached the provider is reported as such so the caller can
    bench it briefly instead of for the whole quota cooldown."""
    if not resolve_tool(worker):
        return False, "not_installed"
    pcfg = sub_cfg(cfg, "probe")
    pf = (run_dir or repo / GATE_DIR / "runs" / "probe") / f"probe-{worker}.prompt.md"
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(PROBE_PROMPT, encoding="utf-8")
    try:
        proc, err = spawn_worker(
            repo, cfg, worker, PROBE_PROMPT, pf,
            log_path=pf.with_suffix(".log"),
            timeout_s=pcfg.get("timeout_s", 90),
        )
    except Exception as exc:  # a probe must never take the run down
        return False, f"error:{type(exc).__name__}"
    if proc is None:
        return False, f"error:{err}"
    out = proc.stdout or ""
    if is_quota(out):
        return False, "quota"
    if getattr(proc, "timed_out", False):
        return False, "timeout"
    if proc.returncode != 0:
        return False, f"exit:{proc.returncode}"
    return True, "ok"


def bench_from_probe(repo: Path, cfg: dict, worker: str, reason: str, run_id: str) -> None:
    pcfg = sub_cfg(cfg, "probe")
    st = tool_state(repo)
    if reason == "ok":
        st[worker] = {"probed_run": run_id, "reason": "ok",
                      "seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    else:
        # Quota occupies the real cooldown; anything that did not reach the
        # provider gets the short retry window only.
        secs = cfg["quota_cooldown_s"] if reason == "quota" else pcfg.get("retry_s", 600)
        st[worker] = {
            "disabled_until": time.time() + float(secs),
            "reason": f"probe:{reason}",
            "probed_run": run_id,
            "seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    set_tool_state(repo, st)


def probe_candidates(cfg: dict, pins: dict[str, str]) -> list[str]:
    jcfg = sub_cfg(cfg, "judge")
    judge_tools = [JUDGE_TOOL.get(m, m) for m in jcfg.get("chain", [])] if jcfg.get("enabled") else []
    seen: list[str] = []
    for w in list(cfg["workers"]) + list(pins.values()) + judge_tools:
        if w and w not in seen:
            seen.append(w)
    return seen


def ensure_probed(repo: Path, cfg: dict, worker: str, run_id: str, run_dir: Path) -> bool:
    """Probe `worker` once per run (lazily, so a cooldown that expires mid-run
    is re-checked before the first dispatch that would use it). Returns
    whether it is usable now."""
    info = tool_state(repo).get(worker, {})
    if info.get("disabled_until", 0) > time.time():
        return False
    if not sub_cfg(cfg, "probe").get("enabled", True):
        return True
    if info.get("probed_run") == run_id:
        return True
    usable, reason = probe_worker(repo, cfg, worker, run_dir)
    bench_from_probe(repo, cfg, worker, reason, run_id)
    journal(repo, {"event": "probe", "run": run_id, "worker": worker,
                   "usable": usable, "reason": reason})
    print(f"  probe {worker}: {'ok' if usable else reason}")
    return usable


def cmd_usage(args) -> None:
    """Probe every configured worker and judge CLI now and print the table."""
    repo = repo_root(Path(args.repo).resolve())
    cfg = load_config(repo)
    qpath = repo / prefixed(cfg, cfg["queue_file"])
    pins = parse_task_workers(qpath.read_text(encoding="utf-8")) if qpath.exists() else {}
    run_id = "usage-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    print("usage — one tiny request per CLI; quota benches for the cooldown, "
          "a probe that never reached the provider benches briefly")
    for w in probe_candidates(cfg, pins):
        usable, reason = probe_worker(repo, cfg, w)
        bench_from_probe(repo, cfg, w, reason, run_id)
        print(f"  {w:8} {'ok' if usable else 'BENCHED'}  {reason}")
    st = tool_state(repo)
    now = time.time()
    for w, info in st.items():
        if info.get("disabled_until", 0) > now:
            mins = int((info["disabled_until"] - now) // 60)
            print(f"  {w:8} benched {mins}m more ({info.get('reason')})")


# ------------------------------------------------------------------ judge


def _last_json_object(text: str) -> dict | None:
    """Best-effort: the last balanced {...} in a transcript, parsed."""
    end = text.rfind("}")
    while end != -1:
        depth = 0
        for i in range(end, -1, -1):
            ch = text[i]
            if ch == "}":
                depth += 1
            elif ch == "{":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[i:end + 1])
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        pass
                    break
        end = text.rfind("}", 0, end)
    return None


def parse_judge_output(output: str, out_file: Path) -> dict | None:
    """Pull the verdict out of whatever the CLI produced: codex writes the
    schema-shaped answer to out_file; claude wraps it in a result envelope
    under structured_output; anything else must end in a JSON object."""
    candidates: list[dict] = []
    if out_file.exists():
        try:
            obj = json.loads(out_file.read_text(encoding="utf-8"))
            if isinstance(obj, dict):
                candidates.append(obj)
        except (json.JSONDecodeError, OSError):
            pass
    env = _last_json_object(output)
    if env:
        if isinstance(env.get("structured_output"), dict):
            candidates.append(env["structured_output"])
        elif isinstance(env.get("result"), str):
            inner = _last_json_object(env["result"])
            if inner:
                candidates.append(inner)
        candidates.append(env)
    for obj in candidates:
        if validate_judge_verdict(obj):
            return normalize_judge_verdict(obj)
    return None


def validate_judge_verdict(obj: dict) -> bool:
    score = obj.get("score")
    verdict = obj.get("verdict")
    return (
        isinstance(score, int) and 0 <= score <= 100
        and verdict in ("pass", "revise", "escalate")
    )


def normalize_judge_verdict(obj: dict) -> dict:
    findings = []
    for f in obj.get("findings") or []:
        if isinstance(f, dict) and f.get("issue"):
            findings.append({
                "severity": f.get("severity") or "medium",
                "file": f.get("file") or "",
                "issue": str(f["issue"]),
                "fix": f.get("fix") or "",
            })
    return {
        "score": int(obj["score"]),
        "verdict": obj["verdict"],
        "findings": findings,
        "revision_brief": str(obj.get("revision_brief") or ""),
    }


def judge_once(
    repo: Path, cfg: dict, prompt: str, run_dir: Path, tag: str, run_id: str
) -> tuple[dict | None, str]:
    """Walk the judge chain. Returns (verdict, member) or (None, why)."""
    jcfg = sub_cfg(cfg, "judge")
    cmds = dict(JUDGE_CMDS)
    cmds.update(cfg.get("judge_cmds") or {})
    schema_file = run_dir / f"{tag}.schema.json"
    schema_file.write_text(json.dumps(JUDGE_SCHEMA), encoding="utf-8")
    failures: list[str] = []
    for member in jcfg.get("chain", []):
        template = cmds.get(member)
        tool = JUDGE_TOOL.get(member, member)
        if not template:
            failures.append(f"{member}:no_contract")
            continue
        if not ensure_probed(repo, cfg, tool, run_id, run_dir):
            failures.append(f"{member}:benched")
            continue
        out_file = run_dir / f"{tag}.{member}.out.json"
        if out_file.exists():
            out_file.unlink()
        pf = run_dir / f"{tag}.{member}.prompt.md"
        pf.write_text(prompt, encoding="utf-8")
        proc, err = spawn_worker(
            repo, cfg, tool, prompt, pf,
            log_path=pf.with_suffix(".log"),
            template=template,
            timeout_s=jcfg.get("timeout_s", 1200),
            extra={
                "schema": json.dumps(JUDGE_SCHEMA),
                "schema_file": str(schema_file),
                "out_file": str(out_file),
            },
        )
        if proc is None:
            failures.append(f"{member}:{err}")
            continue
        out = proc.stdout or ""
        if getattr(proc, "timed_out", False):
            failures.append(f"{member}:timeout")
            continue
        outage = tool_outage_reason(out, proc.returncode)
        if outage:
            disable_worker(repo, cfg, tool, outage)
            failures.append(f"{member}:{outage}")
            continue
        verdict = parse_judge_output(out, out_file)
        if verdict is None:
            failures.append(f"{member}:unparsable")
            continue
        return verdict, member
    return None, "; ".join(failures) or "empty chain"


def format_findings(findings: list[dict]) -> str:
    if not findings:
        return "(none)"
    return "\n".join(
        f"- [{f['severity']}] {f['file'] + ': ' if f['file'] else ''}{f['issue']}"
        + (f" → fix: {f['fix']}" if f["fix"] else "")
        for f in findings
    )


def judge_task(
    repo: Path,
    cfg: dict,
    run_id: str,
    run_dir: Path,
    tid: str,
    base_head: str,
    files: list[str],
    last_worker: str,
    pin: str | None,
) -> dict:
    """Judge a closed task and iterate at most judge.max_revisions times.

    Returns {"final": pass|escalated|skipped|broke_verify, "reason", "rounds":
    [{"round","score","verdict","member","worker","findings"}]}. The queue row
    is never touched here; a revision is an ordinary code commit that the
    runner re-verifies itself, because check-commit only engages on DONE flips.
    """
    jcfg = sub_cfg(cfg, "judge")
    rounds: list[dict] = []
    revisions = 0
    worker_of_last_commit = last_worker
    scores: list[int] = []
    while True:
        head = git(repo, "rev-parse", "HEAD").strip()
        commits = [
            s[:10] for s in git(repo, "rev-list", f"{base_head}..{head}", check=False).split()
        ]
        v = read_verdict(repo) or {}
        prompt = cfg["judge_prompt"].format(
            task=tid,
            commit=head[:10],
            base=base_head[:10],
            commits=", ".join(commits) or head[:10],
            files=", ".join(files) or "(undeclared)",
            verify_tail=(v.get("tail") or "")[-1500:],
            pass_score=jcfg["pass_score"],
        )
        tag = f"{tid}-judge{len(rounds) + 1}"
        journal(repo, {"event": "judge_start", "task": tid, "round": len(rounds) + 1,
                       "commit": head})
        print(f"  judge round {len(rounds) + 1} for {tid} …")
        verdict, member = judge_once(repo, cfg, prompt, run_dir, tag, run_id)
        if verdict is None:
            journal(repo, {"event": "judge_skipped", "task": tid, "reason": member})
            print(f"  judge unavailable ({member}) — task stays DONE, unreviewed")
            return {"final": "skipped", "reason": member, "rounds": rounds}
        rounds.append({
            "round": len(rounds) + 1,
            "score": verdict["score"],
            "verdict": verdict["verdict"],
            "member": member,
            "worker": worker_of_last_commit,
            "findings": verdict["findings"],
            "brief": verdict["revision_brief"],
        })
        scores.append(verdict["score"])
        journal(repo, {"event": "judge_verdict", "task": tid, "round": len(rounds),
                       "score": verdict["score"], "verdict": verdict["verdict"],
                       "member": member, "findings": len(verdict["findings"])})
        print(f"  judge ({member}): score {verdict['score']}, {verdict['verdict']}")
        notify(cfg, repo, f"{tid} judged {verdict['score']} ({verdict['verdict']}, {member})",
               format_findings(verdict["findings"]))

        if verdict["verdict"] == "pass" or verdict["score"] >= jcfg["pass_score"]:
            return {"final": "pass", "reason": "", "rounds": rounds}
        if verdict["verdict"] == "escalate":
            return _escalate(repo, tid, rounds, "judge asked for a human")
        if revisions >= jcfg["max_revisions"]:
            return _escalate(repo, tid, rounds, f"revision cap {jcfg['max_revisions']} reached")
        if len(scores) >= 2 and scores[-1] - scores[-2] < jcfg["min_gain"]:
            return _escalate(
                repo, tid, rounds,
                f"no progress: {scores[-2]} → {scores[-1]} (< min_gain {jcfg['min_gain']})",
            )

        # Pick a usable worker other than the one that made the last commit.
        pool = [w for w in available_workers(repo, cfg, prefer=pin)
                if ensure_probed(repo, cfg, w, run_id, run_dir)]
        others = [w for w in pool if w != worker_of_last_commit]
        if not pool:
            return _escalate(repo, tid, rounds, "no_workers_for_revision")
        # Rotation: a second pass by the same model repeats its own blind
        # spot. Fall back to the same worker only when it is the only usable
        # one, and say so in the journal and the report.
        rotated = bool(others)
        worker = (others or pool)[0]
        rounds[-1]["rotated"] = rotated
        revisions += 1
        rprompt = cfg["revision_prompt"].format(
            task=tid,
            files=", ".join(files) or "(undeclared)",
            brief=verdict["revision_brief"] or "(none)",
            findings=format_findings(verdict["findings"]),
        )
        pf = run_dir / f"{tid}-rev{revisions}.prompt.md"
        pf.write_text(rprompt, encoding="utf-8")
        queue_path = repo / prefixed(cfg, cfg["queue_file"])
        queue_before = queue_path.read_text(encoding="utf-8")
        tree_before = non_gate_worktree_state(repo)
        head_before = head
        journal(repo, {"event": "revision_start", "task": tid, "revision": revisions,
                       "worker": worker, "rotated": rotated,
                       "log": str(pf.with_suffix(".log"))})
        print(f"  revision {revisions} via {worker}{'' if rotated else ' (same worker: no other usable)'} …")

        def beat(secs: int, lines: int, last: str, _tid=tid, _w=worker) -> None:
            print(f"    … {_tid} revising {secs // 60}m, {lines} lines | {last[:80]}")
            journal(repo, {"event": "heartbeat", "task": _tid, "worker": _w,
                           "seconds": secs, "lines": lines, "last_line": last,
                           "phase": "revision"})

        proc, err = spawn_worker(repo, cfg, worker, rprompt, pf,
                                 log_path=pf.with_suffix(".log"), on_beat=beat)
        head_after = git(repo, "rev-parse", "HEAD").strip()
        queue_after = queue_path.read_text(encoding="utf-8")
        changed_rows = queue_status_changes(queue_before, queue_after)
        if changed_rows:
            return _escalate(repo, tid, rounds, f"revision changed queue rows {changed_rows}",
                             final="broke_verify")
        if proc is None or getattr(proc, "timed_out", False):
            journal(repo, {"event": "revision_end", "task": tid, "revision": revisions,
                           "worker": worker, "outcome": "failed", "err": err or "timeout"})
            worker_of_last_commit = worker
            if non_gate_worktree_state(repo) != tree_before:
                return _escalate(repo, tid, rounds, "revision left uncommitted changes",
                                 final="broke_verify")
            continue
        if head_after == head_before:
            journal(repo, {"event": "revision_end", "task": tid, "revision": revisions,
                           "worker": worker, "outcome": "no_commit"})
            if non_gate_worktree_state(repo) != tree_before:
                return _escalate(repo, tid, rounds, "revision left uncommitted changes",
                                 final="broke_verify")
            worker_of_last_commit = worker
            print(f"  revision {revisions} made no commit")
            continue
        worker_of_last_commit = worker
        # The verdict is ours: re-run the acceptance on the revised tree.
        payload = run_acceptance(repo, cfg)
        journal(repo, {"event": "revision_end", "task": tid, "revision": revisions,
                       "worker": worker, "outcome": payload["result"],
                       "commit": head_after})
        if payload["result"] != "PASS":
            return _escalate(
                repo, tid, rounds,
                f"revision commit {head_after[:10]} fails acceptance ({payload['result']})",
                final="broke_verify",
            )


def _escalate(repo: Path, tid: str, rounds: list[dict], reason: str, final: str = "escalated") -> dict:
    journal(repo, {"event": "judge_escalated", "task": tid, "reason": reason, "final": final})
    print(f"  {tid} needs you: {reason}")
    return {"final": final, "reason": reason, "rounds": rounds}


def cmd_run(args) -> None:
    repo = repo_root(Path(args.repo).resolve())
    cfg = load_config(repo)
    projdir = repo / (cfg.get("project_prefix") or "")
    qpath = repo / prefixed(cfg, cfg["queue_file"])

    run_id = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    started = time.time()
    max_tasks = args.max_tasks or cfg["max_tasks"]

    # ---- preflight: refuse to start blind ------------------------------
    hook_ok, hook_mode = hook_installation(repo)
    if not hook_ok and not args.force:
        die(
            "the pre-commit gate is not installed; an unattended run without it "
            "is exactly the failure this gate exists to prevent. Run install-hook or configure the "
            f"pre-commit chain, then run doctor. Detected: {hook_mode}."
        )
    if not qpath.exists():
        die(f"queue not found: {qpath}")

    workers = available_workers(repo, cfg)
    pins = parse_task_workers(qpath.read_text(encoding="utf-8"))
    pinned_available = sorted(
        {w for w in pins.values() if available_workers(repo, cfg, prefer=w)}
    )
    if not workers and not pinned_available:
        die(
            "no usable worker CLI found (checked: "
            + ", ".join(sorted(set(cfg["workers"]) | set(pins.values())))
            + ")"
        )

    print(f"GATE run {run_id}")
    print(f"  project : {projdir}")
    print(f"  queue   : {qpath}")
    print(f"  workers : {', '.join(workers) or '(none run-wide)'}")
    if pins:
        print("  pinned  : " + ", ".join(f"{t}={w}" for t, w in pins.items()))
    print(f"  budget  : {max_tasks} task(s), {cfg['run_timeout_s']}s")
    jcfg = sub_cfg(cfg, "judge")
    print(f"  judge   : {'on — ' + ' → '.join(jcfg['chain']) if jcfg.get('enabled') else 'off'}")
    journal(
        repo,
        {"event": "run_start", "run": run_id, "workers": workers, "pins": pins,
         "judge": bool(jcfg.get("enabled"))},
    )

    # Probe before spending: every candidate CLI once, so a quota-dead worker
    # is benched before it costs a dispatch and a queue-row restore.
    run_dir = repo / GATE_DIR / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if sub_cfg(cfg, "probe").get("enabled", True):
        print("  probing :")
        for w in probe_candidates(cfg, pins):
            ensure_probed(repo, cfg, w, run_id, run_dir)

    results: list[dict] = []
    stop_reason = "budget"

    for slot in range(max_tasks):
        if time.time() - started > cfg["run_timeout_s"]:
            stop_reason = "run_timeout"
            break

        qtext = qpath.read_text(encoding="utf-8")
        tasks = parse_queue(qtext)
        tid, why = head_task(cfg, tasks)
        if tid is None:
            stop_reason = "queue_empty"
            break
        if why == "blocked":
            stop_reason = f"blocked:{tid}"
            print(f"GATE: head task {tid} is BLOCKED — stopping, this needs you.")
            break
        if why == "in_progress":
            stop_reason = f"in_progress:{tid}"
            print(
                f"GATE: head task {tid} is already IN_PROGRESS — another runner may "
                "be active. Stopping instead of taking it over."
            )
            break

        strict = args.strict_admit or cfg.get("strict_admit", False)
        pins = parse_task_workers(qtext)
        pin = pins.get(tid)
        files_map = parse_task_files(qtext)
        task_files = files_map.get(tid) or []
        adm = admit_verdicts(
            cfg, repo, tasks, files_map, pins, parse_task_after(qtext),
            parse_task_approved(qtext),
        ).get(tid, {"verdict": "admit", "reason": ""})
        if adm.get("needs_approval"):
            # Irreversible tier: never a judgement call for the runner, and
            # never downgraded to a warning by advisory mode.
            journal(repo, {"event": "needs_approval", "task": tid, "reason": adm["reason"]})
            stop_reason = f"needs_approval:{tid}"
            print(f"GATE: task {tid} needs your approval ({adm['reason']}) — stopping.")
            break
        if adm["verdict"] != "admit":
            journal(
                repo,
                {"event": "admit_refused", "task": tid, "strict": strict, **adm},
            )
            detail = f"{adm['verdict']}: {adm['reason']}"
            if strict:
                stop_reason = f"admit_refused:{tid}"
                print(
                    f"GATE: task {tid} refused admission ({detail}) — stopping. "
                    "Re-shape the task, or run without strict admission."
                )
                break
            print(
                f"GATE: WARNING task {tid} is outside the sweet spot ({detail}) "
                "— continuing (advisory mode)."
            )

        print(f"\nGATE: task {tid} ({slot + 1}/{max_tasks})")
        head_before = git(repo, "rev-parse", "HEAD").strip()
        attempts = 0
        dispatches = 0
        outcome = "unknown"
        last_sig = None
        last_worker = ""

        while attempts < cfg["max_attempts_per_task"]:
            attempt_queue_text = qpath.read_text(encoding="utf-8")
            attempt_tasks = parse_queue(attempt_queue_text)
            attempt_status = attempt_tasks.get(tid, {}).get("status", "<missing>")
            if attempt_status in cfg["blocked_markers"]:
                outcome = "blocked"
                print(f"  {tid} became BLOCKED — stopping instead of dispatching another task")
                break
            allowed_statuses = set(cfg["todo_markers"] + cfg["in_progress_markers"])
            if attempt_status not in allowed_statuses:
                outcome = "state_changed"
                print(
                    f"  {tid} changed to {attempt_status} before retry — stopping "
                    "instead of selecting another task"
                )
                break

            pool = [
                w for w in available_workers(repo, cfg, prefer=pin)
                if ensure_probed(repo, cfg, w, run_id, run_dir)
            ]
            if not pool:
                outcome = "no_workers"
                break
            # Rotate on retry. A second attempt through the same CLI repeats
            # whatever was wrong with that CLI: a real run once spent both of
            # its attempts on claude while it was returning 529, with codex,
            # grok and kimi installed and idle. Quota benching already rotates; this covers every other
            # tool-shaped failure, which is most of them.
            worker = pool[attempts % len(pool)]
            last_worker = worker
            dispatches += 1
            attempt_head = git(repo, "rev-parse", "HEAD").strip()
            attempt_worktree = non_gate_worktree_state(repo)

            prompt = cfg["worker_prompt"].format(
                skill=cfg.get("worker_skill") or "the project's next-session",
                task=tid,
            )
            pf = repo / GATE_DIR / "runs" / run_id / f"{tid}-a{dispatches}.prompt.md"
            pf.parent.mkdir(parents=True, exist_ok=True)
            pf.write_text(prompt, encoding="utf-8")

            logp = pf.with_suffix(".log")
            how = (
                "pinned" if pin == worker
                else (f"fallback from {pin}" if pin else "run-wide")
            )
            print(f"  attempt {attempts + 1} via {worker} [{how}] … (live log: {logp})")
            journal(
                repo,
                {
                    "event": "attempt_start",
                    "task": tid,
                    "worker": worker,
                    "pin": pin,
                    "pinned": bool(pin) and worker == pin,
                    "attempt": attempts + 1,
                    "dispatch": dispatches,
                    "log": str(logp),
                },
            )
            notify(cfg, repo, f"{tid} started (attempt {attempts + 1}, {worker})")

            def beat(secs: int, lines: int, last: str, _tid=tid, _w=worker) -> None:
                mins = secs // 60
                print(f"    … {_tid} running {mins}m, {lines} lines | {last[:80]}")
                journal(
                    repo,
                    {
                        "event": "heartbeat",
                        "task": _tid,
                        "worker": _w,
                        "seconds": secs,
                        "lines": lines,
                        "last_line": last,
                    },
                )

            t0 = time.time()
            try:
                proc, err = spawn_worker(repo, cfg, worker, prompt, pf, log_path=logp, on_beat=beat)
            except subprocess.TimeoutExpired:
                proc, err = None, "timeout"
            took = round(time.time() - t0, 1)
            if proc is not None and getattr(proc, "timed_out", False):
                print(f"  timed out after {took}s — worker tree killed")
                journal(repo, {"event": "attempt_timeout", "task": tid, "worker": worker})
                notify(cfg, repo, f"{tid} timed out after {int(took // 60)}m")
                attempts += 1
                outcome = "timeout"
                continue

            if proc is None:
                print(f"  worker unusable: {err}")
                journal(
                    repo,
                    {"event": "worker_error", "task": tid, "worker": worker, "err": err},
                )
                if err == "timeout":
                    attempts += 1
                    outcome = "timeout"
                    continue
                outcome = "worker_error"
                break

            out = (proc.stdout or "") + (proc.stderr or "")  # already streamed to logp

            # Tool outages are not task failures. A model CLI may itself exit
            # zero after a nested Git command fails, so classify its transcript
            # regardless of return code. Restore only the dispatched task row;
            # never overwrite concurrent changes elsewhere in the queue.
            outage = tool_outage_reason(out, proc.returncode)
            if outage:
                outage_head = git(repo, "rev-parse", "HEAD").strip()
                if outage_head != attempt_head:
                    outcome = "tool_state_conflict"
                    print(
                        f"  {worker} reported {outage} after moving HEAD — "
                        "stopping for inspection"
                    )
                    break
                try:
                    restored = restore_task_row_text(
                        attempt_queue_text,
                        qpath.read_text(encoding="utf-8"),
                        tid,
                    )
                except ValueError as exc:
                    outcome = "task_mismatch"
                    print(f"  cannot restore {tid} after {outage}: {exc}")
                    break
                qpath.write_text(restored, encoding="utf-8")

                unexpected = {
                    task_id: change
                    for task_id, change in queue_status_changes(
                        attempt_queue_text, restored
                    ).items()
                    if task_id != tid
                }
                dirty_after = non_gate_worktree_state(repo)
                if unexpected:
                    outcome = "task_mismatch"
                    print(
                        f"  {worker} changed other task statuses during {tid}: "
                        f"{unexpected}"
                    )
                    break
                if dirty_after != attempt_worktree:
                    outcome = "tool_left_changes"
                    print(
                        f"  {worker} left non-queue worktree changes after {outage}; "
                        "stopping instead of handing a dirty attempt to another worker"
                    )
                    break

                disable_worker(repo, cfg, worker, outage)
                print(
                    f"  {worker} unavailable ({outage}) — restored {tid}, "
                    "disabled worker, trying the next tool"
                )
                journal(
                    repo,
                    {
                        "event": "tool_disabled",
                        "task": tid,
                        "worker": worker,
                        "reason": outage,
                        "restored_task": True,
                    },
                )
                continue

            attempts += 1

            # ---- the verdict is ours, not the worker's --------------------
            head_after = git(repo, "rev-parse", "HEAD").strip()
            queue_after_text = qpath.read_text(encoding="utf-8")
            tasks_after = parse_queue(queue_after_text)
            task_status = tasks_after.get(tid, {}).get("status", "<missing>")
            now_done = task_status in cfg["done_markers"]
            advanced = head_after != head_before
            completed = newly_done(repo, cfg, head_before, head_after) if advanced else []
            unexpected_done = [task_id for task_id in completed if task_id != tid]
            unexpected_status = {
                task_id: change
                for task_id, change in queue_status_changes(
                    attempt_queue_text, queue_after_text
                ).items()
                if task_id != tid
            }

            if unexpected_done or unexpected_status:
                outcome = "task_mismatch"
                print(
                    f"  dispatched {tid}, but other tasks changed "
                    f"(DONE={unexpected_done}, statuses={unexpected_status}) — stopping"
                )
                journal(
                    repo,
                    {
                        "event": "task_mismatch",
                        "task": tid,
                        "worker": worker,
                        "unexpected_done": unexpected_done,
                        "unexpected_status": unexpected_status,
                    },
                )
                break

            if now_done and completed == [tid]:
                outcome = "done"
                print(f"  {tid} DONE in {took}s via {worker} ({head_after[:10]})")
                journal(
                    repo,
                    {
                        "event": "task_done",
                        "task": tid,
                        "worker": worker,
                        "commit": head_after,
                        "seconds": took,
                    },
                )
                vv = read_verdict(repo)
                notify(
                    cfg,
                    repo,
                    f"{tid} DONE in {int(took // 60)}m ({worker})",
                    f"commit {head_after[:10]}, acceptance count "
                    f"{vv.get('count') if vv else '?'}",
                )
                break

            if task_status in cfg["blocked_markers"]:
                outcome = "blocked"
                print(f"  {tid} reported a real blocker — stopping without retry")
                break

            if task_status not in allowed_statuses:
                outcome = "state_changed"
                print(f"  {tid} ended attempt in unexpected state {task_status} — stopping")
                break

            incomplete = worker_incomplete_reason(out)
            if incomplete:
                outcome = "worker_incomplete"
                print(
                    f"  {worker} returned while task work was still pending "
                    f"({incomplete}) — stopping without a concurrent retry"
                )
                journal(
                    repo,
                    {
                        "event": "worker_incomplete",
                        "task": tid,
                        "worker": worker,
                        "reason": incomplete,
                    },
                )
                break

            dirty_after = non_gate_worktree_state(repo)
            if dirty_after != attempt_worktree:
                outcome = "worker_left_changes"
                print(
                    f"  {worker} exited without closing {tid} and changed the worktree — "
                    "stopping instead of dispatching another worker into partial work"
                )
                break

            sig = f"{proc.returncode}:{out.strip().splitlines()[-1][:120] if out.strip() else ''}"
            if sig == last_sig:
                outcome = "no_progress"
                print("  same failure twice — stopping this task rather than repeating")
                break
            last_sig = sig
            outcome = "not_done"
            print(
                f"  attempt {attempts} did not close {tid} "
                f"(exit={proc.returncode}, commit {'moved' if advanced else 'unchanged'})"
            )

        judge_info: dict | None = None
        if outcome == "done" and jcfg.get("enabled"):
            judge_info = judge_task(
                repo, cfg, run_id, run_dir, tid, head_before, task_files,
                last_worker, pin,
            )


        results.append(
            {
                "task": tid,
                "outcome": outcome,
                "attempts": attempts,
                "dispatches": dispatches,
                "run": run_id,
                "worker": last_worker,
                "pin": pin or "",
                "judge": judge_info,
            }
        )
        journal(repo, {"event": "task_end", "task": tid, "outcome": outcome,
                       "judge": (judge_info or {}).get("final")})
        if outcome != "done":
            notify(
                cfg,
                repo,
                f"{tid} stopped: {outcome}",
                f"log: .gate/runs/{run_id}/{tid}-a{max(dispatches, 1)}.prompt.log",
            )

        if outcome != "done":
            stop_reason = f"{outcome}:{tid}"
            break
        if judge_info and judge_info["final"] == "escalated":
            stop_reason = f"judge_escalated:{tid}"
            break
        if judge_info and judge_info["final"] == "broke_verify":
            stop_reason = f"revision_broke_verify:{tid}"
            break

    elapsed = round(time.time() - started, 1)
    report = render_report(repo, cfg, run_id, results, stop_reason, elapsed)
    (repo / GATE_DIR / f"RUN-REPORT-{run_id}.md").write_text(report, encoding="utf-8")
    latest = repo / GATE_DIR / "RUN-REPORT.md"
    latest.write_text(report, encoding="utf-8")
    journal(repo, {"event": "run_end", "run": run_id, "stop": stop_reason})

    print("\n" + report)

    done = sum(1 for r in results if r["outcome"] == "done")
    notify(
        cfg,
        repo,
        f"run {run_id} finished: {done}/{len(results)} done ({stop_reason})",
        report,
    )

    needs_human = stop_reason.split(":", 1)[0] in (
        "judge_escalated", "revision_broke_verify", "needs_approval"
    )
    if any(r["outcome"] != "done" for r in results) or not results or needs_human:
        raise SystemExit(3)


def render_report(
    repo: Path, cfg: dict, run_id: str, results: list[dict], stop: str, elapsed: float
) -> str:
    lines = [
        f"# Run report {run_id}",
        "",
        f"- elapsed: {elapsed}s",
        f"- stopped because: **{stop}**",
        "",
        "| task | outcome | task attempts | worker dispatches | worker | pin |",
        "|---|---|---:|---:|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['task']} | {r['outcome']} | {r['attempts']} | "
            f"{r.get('dispatches', r['attempts'])} | {r.get('worker', '')} | "
            f"{r.get('pin', '')} |"
        )
    if not results:
        lines.append("| — | nothing ran | 0 | 0 |")

    # Judge: rounds, scores, and what each round's worker was. A skipped
    # judge is printed loudly — quality silently switched off is the failure.
    judged = [r for r in results if r.get("judge")]
    if judged:
        lines += ["", "## Judge", "",
                  "| task | final | rounds | scores | workers | reason |",
                  "|---|---|---:|---|---|---|"]
        for r in judged:
            j = r["judge"]
            scores = " → ".join(str(x["score"]) for x in j["rounds"]) or "—"
            workers_used = ", ".join(
                x["worker"] + ("" if x.get("rotated", True) else " (not rotated)")
                for x in j["rounds"]
            ) or "—"
            lines.append(
                f"| {r['task']} | {j['final']} | {len(j['rounds'])} | {scores} | "
                f"{workers_used} | {j.get('reason', '')} |"
            )
        for r in judged:
            j = r["judge"]
            if j["final"] in ("escalated", "broke_verify") and j["rounds"]:
                last = j["rounds"][-1]
                lines += ["", f"### {r['task']} — last findings (round {last['round']}, "
                          f"{last['member']})", "", format_findings(last["findings"])]
                if last.get("brief"):
                    lines += ["", f"Brief: {last['brief']}"]

    st = tool_state(repo)
    now = time.time()
    disabled = [
        f"{k} (until {time.strftime('%H:%M UTC', time.gmtime(v['disabled_until']))}, "
        f"{v.get('reason')})"
        for k, v in st.items()
        if v.get("disabled_until", 0) > now
    ]
    lines += ["", "## Tools", ""]
    lines.append(
        "- disabled: " + (", ".join(disabled) if disabled else "none") + ""
    )

    # A failed attempt usually leaves edits behind. Say so — an uncommitted
    # tree is the thing most likely to confuse the next run.
    pre = (cfg.get("project_prefix") or "").strip("/")
    dirty = [
        ln
        for ln in git(repo, "status", "--porcelain", "--", pre or ".").splitlines()
        if ln.strip() and GATE_DIR not in ln
    ]
    if dirty:
        lines += ["", "## Uncommitted changes left in the project", "", "```"]
        lines += dirty[:40]
        if len(dirty) > 40:
            lines.append(f"... {len(dirty) - 40} more")
        lines += ["```", "", "Review these before starting another run."]

    v = read_verdict(repo)
    if v:
        lines += [
            "",
            "## Last acceptance run",
            "",
            f"- `{v['cmd']}` → **{v['result']}** (exit {v['exit_code']}, count {v.get('count')})",
        ]

    # The only section the user has to read. Empty on a clean run.
    waiting: list[str] = []
    if stop.startswith("needs_approval:"):
        tid = stop.split(":", 1)[1]
        waiting.append(
            f"- **{tid}** touches an irreversible path. Review its scope, then add "
            f"`<!-- task:{tid} approved: <who/date> -->` to the queue and start another run."
        )
    if stop.startswith("judge_escalated:"):
        tid = stop.split(":", 1)[1]
        waiting.append(
            f"- **{tid}** is DONE by the acceptance gate but the judge could not get it to "
            "the pass score. Read the Judge section; decide fix / accept / re-shape."
        )
    if stop.startswith("revision_broke_verify:"):
        tid = stop.split(":", 1)[1]
        waiting.append(
            f"- **{tid}**: a judge-driven revision left the tree failing acceptance or "
            "dirty. Inspect `git log`/`git status` before any further run."
        )
    for r in results:
        j = r.get("judge")
        if j and j["final"] == "skipped":
            waiting.append(
                f"- **{r['task']}** closed without a judge review ({j.get('reason')}). "
                "Not blocking; spot-check it."
            )
    lines += ["", "## Waiting on you", ""]
    lines += waiting or ["- nothing"]

    lines += [
        "",
        "## What to do next",
        "",
    ]
    if stop.startswith("blocked:"):
        lines.append(
            f"- The head task {stop.split(':', 1)[1]} is BLOCKED. Answer its blocking "
            "questions in the queue, set it to TODO, and start another run."
        )
    elif stop.startswith("in_progress:"):
        lines.append(
            "- The head task is IN_PROGRESS. Either another runner is active, or a "
            "previous one died mid-task. Check `git status` before restarting."
        )
    elif stop == "queue_empty":
        lines.append("- Nothing left to do. The queue has no eligible task.")
    elif stop in ("budget", "run_timeout"):
        lines.append("- Budget reached. Start another run to continue.")
    else:
        lines.append(
            "- A task did not close. Its prompt and full worker log are under "
            f"`.gate/runs/{run_id}/`. Read the log before re-running."
        )
    lines.append("- Re-derive the structural verdict any time: `gate.py audit --last 30`.")
    return "\n".join(lines) + "\n"


def cmd_doctor(args) -> None:
    repo = repo_root(Path(args.repo).resolve())
    print(f"repo            : {repo}")
    cfgp = Path(os.environ["GATE_CONFIG"]) if os.environ.get("GATE_CONFIG") else repo / GATE_DIR / CONFIG_NAME
    print(f"config          : {cfgp} {'OK' if cfgp.exists() else 'MISSING'}")
    if not cfgp.exists():
        return
    cfg = load_config(repo)
    print(f"project_prefix  : {cfg.get('project_prefix') or '(whole repo)'}")
    qf = repo / prefixed(cfg, cfg["queue_file"])
    print(f"queue           : {qf} {'OK' if qf.exists() else 'MISSING'}")
    if qf.exists():
        tasks = parse_queue(qf.read_text(encoding="utf-8"))
        counts: dict[str, int] = {}
        for t in tasks.values():
            counts[t["status"]] = counts.get(t["status"], 0) + 1
        print(f"tasks parsed    : {len(tasks)}  {counts}")
        pins = parse_task_workers(qf.read_text(encoding="utf-8"))
        todo_pins = {
            t: w
            for t, w in pins.items()
            if tasks.get(t, {}).get("status") in cfg["todo_markers"]
        }
        if todo_pins:
            print(f"worker pins     : {todo_pins}")
    installed, hook_mode = hook_installation(repo)
    print(f"pre-commit gate : {'INSTALLED' if installed else 'not installed'} ({hook_mode})")
    print(f"verify_cmd      : {cfg['verify_cmd']}")
    v = read_verdict(repo)
    if v:
        age = int(time.time() - float(v.get("at_epoch", 0)))
        print(f"last verdict    : {v['result']} count={v.get('count')} age={age}s")
    else:
        print("last verdict    : none")


def main() -> None:
    ap = argparse.ArgumentParser(prog="gate.py", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, fn, extra in [
        ("init", cmd_init, [("--force", "store_true")]),
        ("install-hook", cmd_install_hook, [("--force", "store_true")]),
        ("check-commit", cmd_check_commit, []),
        ("verify", cmd_verify, [("--cmd", "str"), ("--task", "str"), ("--queue", "store_true")]),
        ("audit", cmd_audit, [("--last", "int")]),
        ("admit", cmd_admit, []),
        ("usage", cmd_usage, []),
        ("doctor", cmd_doctor, []),
        ("run", cmd_run, [
            ("--max-tasks", "int"),
            ("--force", "store_true"),
            ("--strict-admit", "store_true"),
        ]),
    ]:
        p = sub.add_parser(name)
        p.add_argument("--repo", default=".")
        for flag, kind in extra:
            if kind == "store_true":
                p.add_argument(flag, action="store_true")
            elif kind == "int":
                # --last defaults to 30; --max-tasks falls back to the config.
                p.add_argument(flag, type=int, default=30 if flag == "--last" else None)
            else:
                p.add_argument(flag, default=None)
        p.set_defaults(func=fn)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
